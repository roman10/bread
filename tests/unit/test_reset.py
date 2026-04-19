"""Unit tests for bread.reset.reset_environment."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from bread.core.exceptions import BreadError
from bread.db.database import init_db
from bread.db.models import (
    ClaudeUsageLog,
    EventAlertLog,
    MarketDataCache,
    OrderLog,
    PortfolioSnapshot,
    SignalLog,
)
from bread.reset import ALPACA_PAPER_DASHBOARD_URL, reset_environment


def _make_config(mode: str = "paper") -> MagicMock:
    """Minimal AppConfig stand-in — reset_environment only reads .mode."""
    cfg = MagicMock()
    cfg.mode = mode
    return cfg


@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:")
    init_db(eng)
    return eng


@pytest.fixture()
def seeded_engine(engine):
    """Populate one row in each table so we can verify what's wiped vs kept."""
    now = datetime.now(UTC)
    sf = sessionmaker(bind=engine)
    with sf() as session:
        session.add(MarketDataCache(
            symbol="SPY", timeframe="1Day",
            timestamp_utc=datetime(2025, 1, 2, tzinfo=UTC),
            open=100, high=105, low=99, close=103, volume=1_000_000,
            fetched_at_utc=now,
        ))
        session.add(OrderLog(
            broker_order_id="b1", symbol="SPY", side="BUY", qty=10,
            status="FILLED", filled_price=500.0, strategy_name="etf_momentum",
            reason="seed", created_at_utc=now, filled_at_utc=now,
        ))
        session.add(SignalLog(
            strategy_name="etf_momentum", symbol="SPY", direction="BUY",
            strength=0.5, stop_loss_pct=0.05, reason="seed",
            signal_timestamp=now,
        ))
        session.add(PortfolioSnapshot(
            timestamp_utc=now, equity=10_000, cash=8_000,
            positions_value=2_000, open_positions=1, daily_pnl=50,
        ))
        session.add(EventAlertLog(
            symbol="SPY", severity="info", headline="seed",
            details="seed", event_type="news",
        ))
        session.add(ClaudeUsageLog(
            model="sonnet", use_case="review", prompt_length=100,
            duration_ms=250, success=True, cost_usd=0.01,
        ))
        session.commit()
    return engine


class TestResetEnvironment:
    def test_blocks_in_live_mode(self, engine) -> None:
        with pytest.raises(BreadError, match="paper-only"):
            reset_environment(_make_config("live"), None, engine)

    def test_deletes_trade_tables(self, seeded_engine) -> None:
        report = reset_environment(_make_config("paper"), None, seeded_engine)

        assert report.orders_deleted == 1
        assert report.signals_deleted == 1
        assert report.snapshots_deleted == 1
        assert report.alerts_deleted == 1
        assert report.claude_usage_deleted == 1

        sf = sessionmaker(bind=seeded_engine)
        with sf() as session:
            assert session.execute(select(OrderLog)).first() is None
            assert session.execute(select(SignalLog)).first() is None
            assert session.execute(select(PortfolioSnapshot)).first() is None
            assert session.execute(select(EventAlertLog)).first() is None
            assert session.execute(select(ClaudeUsageLog)).first() is None

    def test_preserves_market_data_cache(self, seeded_engine) -> None:
        report = reset_environment(_make_config("paper"), None, seeded_engine)
        assert report.bars_preserved == 1

        sf = sessionmaker(bind=seeded_engine)
        with sf() as session:
            rows = session.execute(select(MarketDataCache)).scalars().all()
            assert len(rows) == 1
            assert rows[0].symbol == "SPY"

    def test_no_broker_yields_zero_counts(self, seeded_engine) -> None:
        report = reset_environment(_make_config("paper"), None, seeded_engine)
        assert report.broker_orders_cancelled == 0
        assert report.broker_positions_closed == 0

    def test_calls_broker_soft_reset(self, seeded_engine) -> None:
        broker = MagicMock()
        broker.cancel_all_orders.return_value = 3
        broker.close_all_positions.return_value = 2

        report = reset_environment(_make_config("paper"), broker, seeded_engine)

        broker.cancel_all_orders.assert_called_once_with()
        broker.close_all_positions.assert_called_once_with()
        assert report.broker_orders_cancelled == 3
        assert report.broker_positions_closed == 2

    def test_report_includes_manual_instructions(self, engine) -> None:
        report = reset_environment(_make_config("paper"), None, engine)
        assert ALPACA_PAPER_DASHBOARD_URL in report.manual_instructions
        assert "Reset Account" in report.manual_instructions

    def test_empty_db_runs_cleanly(self, engine) -> None:
        report = reset_environment(_make_config("paper"), None, engine)
        assert report.orders_deleted == 0
        assert report.signals_deleted == 0
        assert report.snapshots_deleted == 0
        assert report.alerts_deleted == 0
        assert report.claude_usage_deleted == 0
        assert report.bars_preserved == 0

    def test_broker_failure_does_not_abort_local_cleanup(self, seeded_engine) -> None:
        """If broker methods swallow errors and return 0, local cleanup still runs.

        AlpacaBroker.cancel_all_orders / close_all_positions are documented to
        log-and-return-0 on failure rather than raise. The report should show
        zero broker counts but still reflect deleted local rows.
        """
        broker = MagicMock()
        broker.cancel_all_orders.return_value = 0
        broker.close_all_positions.return_value = 0

        report = reset_environment(_make_config("paper"), broker, seeded_engine)

        assert report.broker_orders_cancelled == 0
        assert report.orders_deleted == 1  # local cleanup still ran
