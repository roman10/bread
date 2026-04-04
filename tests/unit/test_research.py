"""Tests for event monitoring research module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from bread.ai.models import EventAlert, MarketResearch
from bread.ai.research import (
    _deactivate_stale_alerts,
    _send_high_severity_alerts,
    _store_results,
    collect_research_symbols,
    get_active_alerts,
    run_research_scan,
)
from bread.db.database import init_db
from bread.db.models import EventAlertLog


@pytest.fixture()
def db_session_factory() -> sessionmaker:  # type: ignore[type-arg]
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    return sessionmaker(bind=engine)


def _make_event(
    symbol: str = "SPY",
    severity: str = "high",
    headline: str = "Test event",
) -> EventAlert:
    return EventAlert(
        symbol=symbol,
        severity=severity,
        headline=headline,
        details="Some details",
        event_type="macro",
        source="test.com",
    )


def _make_research(events: list[EventAlert] | None = None) -> MarketResearch:
    return MarketResearch(
        events=[_make_event()] if events is None else events,
        scan_summary="Test scan complete",
    )


# ------------------------------------------------------------------
# collect_research_symbols
# ------------------------------------------------------------------


class TestCollectResearchSymbols:
    def test_held_first(self) -> None:
        all_syms, held = collect_research_symbols(["SPY", "QQQ"], ["IWM", "TLT"])
        assert all_syms[:2] == ["SPY", "QQQ"]
        assert held == ["SPY", "QQQ"]

    def test_deduplicates(self) -> None:
        all_syms, held = collect_research_symbols(["SPY", "QQQ"], ["QQQ", "IWM"])
        assert all_syms == ["SPY", "QQQ", "IWM"]

    def test_caps_at_max(self) -> None:
        symbols = [f"SYM{i}" for i in range(50)]
        all_syms, _ = collect_research_symbols([], symbols, max_symbols=10)
        assert len(all_syms) == 10

    def test_empty_inputs(self) -> None:
        all_syms, held = collect_research_symbols([], [])
        assert all_syms == []
        assert held == []

    def test_held_preserved_within_cap(self) -> None:
        held_list = [f"H{i}" for i in range(5)]
        watchlist = [f"W{i}" for i in range(50)]
        all_syms, held = collect_research_symbols(held_list, watchlist, max_symbols=10)
        assert len(all_syms) == 10
        for h in held_list:
            assert h in all_syms

    def test_held_only_subset_of_all_symbols(self) -> None:
        """held_only must not include symbols that were capped out."""
        held_list = [f"H{i}" for i in range(20)]
        all_syms, held = collect_research_symbols(held_list, [], max_symbols=5)
        assert len(all_syms) == 5
        assert len(held) == 5
        assert set(held) == set(all_syms)


# ------------------------------------------------------------------
# get_active_alerts
# ------------------------------------------------------------------


class TestGetActiveAlerts:
    def test_returns_recent_high_medium(self, db_session_factory: sessionmaker) -> None:  # type: ignore[type-arg]
        with db_session_factory() as session:
            session.add(
                EventAlertLog(
                    symbol="SPY",
                    severity="high",
                    headline="Rate hike",
                    details="Details",
                    event_type="macro",
                    source="test",
                    scan_summary="scan",
                    is_active=True,
                    scanned_at_utc=datetime.now(UTC),
                )
            )
            session.add(
                EventAlertLog(
                    symbol="SPY",
                    severity="low",
                    headline="Minor news",
                    details="Details",
                    event_type="other",
                    source="test",
                    scan_summary="scan",
                    is_active=True,
                    scanned_at_utc=datetime.now(UTC),
                )
            )
            session.commit()

        alerts = get_active_alerts(db_session_factory, ["SPY"])
        assert len(alerts) == 1
        assert alerts[0].severity == "high"

    def test_filters_inactive(self, db_session_factory: sessionmaker) -> None:  # type: ignore[type-arg]
        with db_session_factory() as session:
            session.add(
                EventAlertLog(
                    symbol="SPY",
                    severity="high",
                    headline="Old event",
                    details="d",
                    event_type="macro",
                    source="",
                    scan_summary="",
                    is_active=False,
                    scanned_at_utc=datetime.now(UTC),
                )
            )
            session.commit()

        alerts = get_active_alerts(db_session_factory, ["SPY"])
        assert alerts == []

    def test_filters_old(self, db_session_factory: sessionmaker) -> None:  # type: ignore[type-arg]
        with db_session_factory() as session:
            session.add(
                EventAlertLog(
                    symbol="SPY",
                    severity="high",
                    headline="Stale",
                    details="d",
                    event_type="macro",
                    source="",
                    scan_summary="",
                    is_active=True,
                    scanned_at_utc=datetime.now(UTC) - timedelta(hours=48),
                )
            )
            session.commit()

        alerts = get_active_alerts(db_session_factory, ["SPY"], max_age_hours=24)
        assert alerts == []

    def test_empty_symbols(self, db_session_factory: sessionmaker) -> None:  # type: ignore[type-arg]
        assert get_active_alerts(db_session_factory, []) == []

    def test_fail_open_on_error(self) -> None:
        broken_factory = MagicMock(side_effect=Exception("DB broken"))
        assert get_active_alerts(broken_factory, ["SPY"]) == []


# ------------------------------------------------------------------
# _store_results
# ------------------------------------------------------------------


class TestStoreResults:
    def test_stores_non_none_events(self, db_session_factory: sessionmaker) -> None:  # type: ignore[type-arg]
        research = _make_research(
            [
                _make_event("SPY", "high", "Big news"),
                _make_event("QQQ", "none", "Nothing"),
                _make_event("IWM", "medium", "Some news"),
            ]
        )
        _store_results(db_session_factory, research)

        with db_session_factory() as session:
            rows = session.execute(select(EventAlertLog)).scalars().all()
        assert len(rows) == 2
        symbols = {r.symbol for r in rows}
        assert symbols == {"SPY", "IWM"}

    def test_empty_events(self, db_session_factory: sessionmaker) -> None:  # type: ignore[type-arg]
        research = _make_research([])
        _store_results(db_session_factory, research)
        with db_session_factory() as session:
            rows = session.execute(select(EventAlertLog)).scalars().all()
        assert len(rows) == 0


# ------------------------------------------------------------------
# _deactivate_stale_alerts
# ------------------------------------------------------------------


class TestDeactivateStaleAlerts:
    def test_marks_old_inactive(self, db_session_factory: sessionmaker) -> None:  # type: ignore[type-arg]
        with db_session_factory() as session:
            session.add(
                EventAlertLog(
                    symbol="SPY",
                    severity="high",
                    headline="Old",
                    details="d",
                    event_type="macro",
                    source="",
                    scan_summary="",
                    is_active=True,
                    scanned_at_utc=datetime.now(UTC) - timedelta(hours=72),
                )
            )
            session.add(
                EventAlertLog(
                    symbol="QQQ",
                    severity="medium",
                    headline="Recent",
                    details="d",
                    event_type="analyst",
                    source="",
                    scan_summary="",
                    is_active=True,
                    scanned_at_utc=datetime.now(UTC),
                )
            )
            session.commit()

        _deactivate_stale_alerts(db_session_factory, max_age_hours=48)

        with db_session_factory() as session:
            rows = session.execute(select(EventAlertLog)).scalars().all()
        active = [r for r in rows if r.is_active]
        assert len(active) == 1
        assert active[0].symbol == "QQQ"


# ------------------------------------------------------------------
# _send_high_severity_alerts
# ------------------------------------------------------------------


class TestSendHighSeverityAlerts:
    def test_sends_for_high_only(self) -> None:
        alert_mgr = MagicMock()
        research = _make_research(
            [
                _make_event("SPY", "high", "Big"),
                _make_event("QQQ", "medium", "Medium"),
                _make_event("IWM", "low", "Low"),
            ]
        )
        _send_high_severity_alerts(research, alert_mgr)
        alert_mgr.notify_event_alert.assert_called_once_with(
            "SPY", "Big", "Some details", "macro"
        )

    def test_no_alert_manager(self) -> None:
        # Should not raise
        _send_high_severity_alerts(_make_research(), None)


# ------------------------------------------------------------------
# run_research_scan
# ------------------------------------------------------------------


class TestRunResearchScan:
    def test_full_scan(self, db_session_factory: sessionmaker) -> None:  # type: ignore[type-arg]
        claude = MagicMock()
        claude.research_events.return_value = _make_research(
            [_make_event("SPY", "high", "Rate hike")]
        )
        alert_mgr = MagicMock()

        run_research_scan(claude, db_session_factory, ["SPY"], ["QQQ"], alert_mgr)

        claude.research_events.assert_called_once()
        # Verify stored
        with db_session_factory() as session:
            rows = session.execute(select(EventAlertLog)).scalars().all()
        assert len(rows) == 1
        assert rows[0].symbol == "SPY"
        # Verify alert sent
        alert_mgr.notify_event_alert.assert_called_once()

    def test_empty_symbols_noop(self, db_session_factory: sessionmaker) -> None:  # type: ignore[type-arg]
        claude = MagicMock()
        run_research_scan(claude, db_session_factory, [], [])
        claude.research_events.assert_not_called()

    def test_fail_open_on_claude_error(self, db_session_factory: sessionmaker) -> None:  # type: ignore[type-arg]
        claude = MagicMock()
        claude.research_events.side_effect = Exception("Claude broke")
        # Should not raise
        run_research_scan(claude, db_session_factory, ["SPY"], [])
