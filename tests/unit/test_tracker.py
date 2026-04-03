"""Tests for monitoring.tracker."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bread.db.database import init_db
from bread.db.models import PortfolioSnapshot
from bread.monitoring.tracker import get_daily_summaries, get_drawdown_series, get_period_pnl


def _make_sf():
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    return sessionmaker(bind=engine)


def _snap(
    sf,
    ts: datetime,
    equity: float,
    cash: float | None = None,
    positions: int = 0,
):
    if cash is None:
        cash = equity
    with sf() as session:
        session.add(PortfolioSnapshot(
            timestamp_utc=ts,
            equity=equity,
            cash=cash,
            positions_value=equity - cash,
            open_positions=positions,
            daily_pnl=0.0,
        ))
        session.commit()


class TestGetDailySummaries:
    def test_empty_db(self) -> None:
        sf = _make_sf()
        with sf() as session:
            assert get_daily_summaries(session) == []

    def test_single_day_single_snapshot(self) -> None:
        sf = _make_sf()
        ts = datetime(2026, 3, 10, 14, 0, tzinfo=UTC)
        _snap(sf, ts, 10_000.0)

        with sf() as session:
            summaries = get_daily_summaries(session)

        assert len(summaries) == 1
        s = summaries[0]
        assert s.date == date(2026, 3, 10)
        assert s.open_equity == 10_000.0
        assert s.close_equity == 10_000.0
        assert s.pnl == 0.0

    def test_multiple_snapshots_per_day(self) -> None:
        sf = _make_sf()
        d = date(2026, 3, 10)
        _snap(sf, datetime(d.year, d.month, d.day, 9, 30, tzinfo=UTC), 10_000.0)
        _snap(sf, datetime(d.year, d.month, d.day, 12, 0, tzinfo=UTC), 10_200.0)
        _snap(sf, datetime(d.year, d.month, d.day, 15, 45, tzinfo=UTC), 10_150.0)

        with sf() as session:
            summaries = get_daily_summaries(session)

        assert len(summaries) == 1
        s = summaries[0]
        assert s.open_equity == 10_000.0
        assert s.close_equity == 10_150.0
        assert s.pnl == 150.0
        assert abs(s.pnl_pct - 1.5) < 0.01
        assert s.high_equity == 10_200.0
        assert s.low_equity == 10_000.0

    def test_filter_by_date_range(self) -> None:
        sf = _make_sf()
        _snap(sf, datetime(2026, 3, 1, 10, 0, tzinfo=UTC), 10_000.0)
        _snap(sf, datetime(2026, 3, 5, 10, 0, tzinfo=UTC), 10_100.0)
        _snap(sf, datetime(2026, 3, 10, 10, 0, tzinfo=UTC), 10_200.0)

        with sf() as session:
            summaries = get_daily_summaries(session, start=date(2026, 3, 5))

        assert len(summaries) == 2
        assert summaries[0].date == date(2026, 3, 5)

    def test_multiple_days(self) -> None:
        sf = _make_sf()
        _snap(sf, datetime(2026, 3, 10, 10, 0, tzinfo=UTC), 10_000.0)
        _snap(sf, datetime(2026, 3, 11, 10, 0, tzinfo=UTC), 10_100.0)
        _snap(sf, datetime(2026, 3, 12, 10, 0, tzinfo=UTC), 9_950.0)

        with sf() as session:
            summaries = get_daily_summaries(session)

        assert len(summaries) == 3
        assert summaries[0].date == date(2026, 3, 10)
        assert summaries[2].date == date(2026, 3, 12)


class TestGetPeriodPnl:
    def test_daily_returns_last_30_days(self) -> None:
        sf = _make_sf()
        today = date.today()
        for i in range(5):
            d = today - timedelta(days=i)
            _snap(sf, datetime(d.year, d.month, d.day, 10, 0, tzinfo=UTC), 10_000.0 + i * 50)

        with sf() as session:
            result = get_period_pnl(session, "daily")

        assert len(result) == 5
        # Each tuple is (label, pnl, pnl_pct)
        for label, pnl, pnl_pct in result:
            assert isinstance(label, str)

    def test_empty_db_returns_empty(self) -> None:
        sf = _make_sf()
        with sf() as session:
            assert get_period_pnl(session, "weekly") == []


class TestGetDrawdownSeries:
    def test_empty_db(self) -> None:
        sf = _make_sf()
        with sf() as session:
            assert get_drawdown_series(session) == []

    def test_no_drawdown_when_always_rising(self) -> None:
        sf = _make_sf()
        _snap(sf, datetime(2026, 3, 1, 10, 0, tzinfo=UTC), 10_000.0)
        _snap(sf, datetime(2026, 3, 2, 10, 0, tzinfo=UTC), 10_500.0)
        _snap(sf, datetime(2026, 3, 3, 10, 0, tzinfo=UTC), 11_000.0)

        with sf() as session:
            series = get_drawdown_series(session)

        assert len(series) == 3
        for _, dd in series:
            assert dd == 0.0

    def test_drawdown_after_peak(self) -> None:
        sf = _make_sf()
        _snap(sf, datetime(2026, 3, 1, 10, 0, tzinfo=UTC), 10_000.0)
        _snap(sf, datetime(2026, 3, 2, 10, 0, tzinfo=UTC), 10_000.0)
        _snap(sf, datetime(2026, 3, 3, 10, 0, tzinfo=UTC), 9_500.0)  # 5% drawdown

        with sf() as session:
            series = get_drawdown_series(session)

        assert len(series) == 3
        assert series[0][1] == 0.0
        assert series[1][1] == 0.0
        assert abs(series[2][1] - 5.0) < 0.01  # 5% drawdown

    def test_recovery_resets_drawdown(self) -> None:
        sf = _make_sf()
        _snap(sf, datetime(2026, 3, 1, 10, 0, tzinfo=UTC), 10_000.0)
        _snap(sf, datetime(2026, 3, 2, 10, 0, tzinfo=UTC), 9_000.0)  # 10% dd
        _snap(sf, datetime(2026, 3, 3, 10, 0, tzinfo=UTC), 10_500.0)  # new peak

        with sf() as session:
            series = get_drawdown_series(session)

        assert abs(series[1][1] - 10.0) < 0.01
        assert series[2][1] == 0.0  # recovered past previous peak
