"""Tests for risk.context — RiskContextRepo extracted from ExecutionEngine."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from bread.db.models import OrderLog, PortfolioSnapshot
from bread.risk.context import RiskContext, RiskContextRepo


def _make_snapshot(sf, timestamp: datetime, equity: float = 10_000.0) -> None:
    with sf() as session:
        session.add(
            PortfolioSnapshot(
                timestamp_utc=timestamp,
                equity=equity,
                cash=equity,
                positions_value=0.0,
                open_positions=0,
                daily_pnl=0.0,
            )
        )
        session.commit()


def _add_order(sf, symbol: str, side: str, filled_at: datetime | None) -> None:
    now = datetime.now(UTC)
    with sf() as session:
        session.add(
            OrderLog(
                broker_order_id=f"{side}-{symbol}-{now.timestamp():.0f}",
                symbol=symbol,
                side=side,
                qty=10,
                status="FILLED",
                strategy_name="test",
                reason=side.lower(),
                created_at_utc=now,
                filled_at_utc=filled_at,
            )
        )
        session.commit()


# ---------------------------------------------------------------------------
# RiskContext dataclass
# ---------------------------------------------------------------------------


class TestRiskContext:
    def test_is_frozen(self) -> None:
        ctx = RiskContext(
            equity=10_000.0,
            buying_power=8_000.0,
            daily_pnl=100.0,
            peak_equity=10_000.0,
            weekly_pnl=50.0,
            day_trade_count=0,
        )
        with pytest.raises((AttributeError, TypeError)):
            ctx.equity = 99.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _get_peak_equity
# ---------------------------------------------------------------------------


class TestPeakEquity:
    def test_returns_current_on_empty_db(self, session_factory) -> None:
        repo = RiskContextRepo(session_factory)
        assert repo._get_peak_equity(10_000.0) == 10_000.0

    def test_returns_max_from_snapshots(self, session_factory) -> None:
        repo = RiskContextRepo(session_factory)
        _make_snapshot(session_factory, datetime.now(UTC), equity=12_000.0)
        assert repo._get_peak_equity(10_000.0) == 12_000.0

    def test_returns_current_when_higher_than_db(self, session_factory) -> None:
        repo = RiskContextRepo(session_factory)
        _make_snapshot(session_factory, datetime.now(UTC), equity=8_000.0)
        assert repo._get_peak_equity(10_000.0) == 10_000.0

    def test_zero_equity_in_db_not_lost(self, session_factory) -> None:
        """Peak of 0.0 should not be treated as falsy and replaced with current."""
        repo = RiskContextRepo(session_factory)
        _make_snapshot(session_factory, datetime.now(UTC), equity=0.0)
        # current_equity is the higher value; but 0.0 peak should be found, not silently dropped
        assert repo._get_peak_equity(5_000.0) == 5_000.0  # max(0.0, 5000) = 5000
        assert repo._get_peak_equity(0.0) == 0.0  # max(0.0, 0.0) = 0.0 (not lost)


# ---------------------------------------------------------------------------
# _get_weekly_pnl
# ---------------------------------------------------------------------------


class TestWeeklyPnl:
    def test_no_snapshots_returns_zero(self, session_factory) -> None:
        repo = RiskContextRepo(session_factory)
        assert repo._get_weekly_pnl(10_000.0) == 0.0

    def test_computes_change_from_week_start(self, session_factory) -> None:
        repo = RiskContextRepo(session_factory)
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        week_start = datetime(monday.year, monday.month, monday.day, tzinfo=UTC)

        _make_snapshot(session_factory, week_start + timedelta(hours=1), equity=9_500.0)
        _make_snapshot(session_factory, week_start + timedelta(hours=10), equity=9_800.0)

        assert repo._get_weekly_pnl(10_000.0) == 500.0  # 10_000 - 9_500

    def test_ignores_last_week_snapshots(self, session_factory) -> None:
        repo = RiskContextRepo(session_factory)
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        week_start = datetime(monday.year, monday.month, monday.day, tzinfo=UTC)

        _make_snapshot(session_factory, week_start - timedelta(days=1), equity=8_000.0)
        _make_snapshot(session_factory, week_start + timedelta(hours=1), equity=9_500.0)

        assert repo._get_weekly_pnl(10_000.0) == 500.0


# ---------------------------------------------------------------------------
# _get_day_trade_count
# ---------------------------------------------------------------------------


class TestDayTradeCount:
    def test_no_orders_returns_zero(self, session_factory) -> None:
        repo = RiskContextRepo(session_factory)
        assert repo._get_day_trade_count() == 0

    def test_counts_same_day_buy_and_sell(self, session_factory) -> None:
        repo = RiskContextRepo(session_factory)
        now = datetime.now(UTC)
        _add_order(session_factory, "SPY", "BUY", now)
        _add_order(session_factory, "SPY", "SELL", now)
        assert repo._get_day_trade_count() == 1

    def test_buy_only_not_counted(self, session_factory) -> None:
        repo = RiskContextRepo(session_factory)
        _add_order(session_factory, "SPY", "BUY", datetime.now(UTC))
        assert repo._get_day_trade_count() == 0

    def test_old_trades_excluded(self, session_factory) -> None:
        repo = RiskContextRepo(session_factory)
        old = datetime.now(UTC) - timedelta(days=10)
        _add_order(session_factory, "SPY", "BUY", old)
        _add_order(session_factory, "SPY", "SELL", old)
        assert repo._get_day_trade_count() == 0

    def test_multiple_symbols_counted_separately(self, session_factory) -> None:
        repo = RiskContextRepo(session_factory)
        now = datetime.now(UTC)
        for sym in ("SPY", "QQQ"):
            _add_order(session_factory, sym, "BUY", now)
            _add_order(session_factory, sym, "SELL", now)
        assert repo._get_day_trade_count() == 2

    def test_null_filled_at_ignored(self, session_factory) -> None:
        repo = RiskContextRepo(session_factory)
        now = datetime.now(UTC)
        # BUY with null filled_at is skipped — leaving only SELL, so no complete day trade
        _add_order(session_factory, "SPY", "BUY", None)
        _add_order(session_factory, "SPY", "SELL", now)
        assert repo._get_day_trade_count() == 0


# ---------------------------------------------------------------------------
# fetch (integration of all three)
# ---------------------------------------------------------------------------


class TestFetch:
    def test_returns_risk_context_dataclass(self, session_factory) -> None:
        repo = RiskContextRepo(session_factory)
        ctx = repo.fetch(equity=10_000.0, buying_power=8_000.0, daily_pnl=100.0)
        assert isinstance(ctx, RiskContext)
        assert ctx.equity == 10_000.0
        assert ctx.buying_power == 8_000.0
        assert ctx.daily_pnl == 100.0
        assert ctx.peak_equity == 10_000.0  # no snapshots → defaults to current
        assert ctx.weekly_pnl == 0.0
        assert ctx.day_trade_count == 0
