"""Tests for execution.engine."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from bread.core.exceptions import OrderError
from bread.core.models import Position, Signal, SignalDirection
from bread.db.database import init_db
from bread.db.models import OrderLog, PortfolioSnapshot
from bread.execution.engine import ExecutionEngine
from bread.risk.validators import ValidationResult


def _make_signal(
    symbol: str = "SPY",
    direction: SignalDirection = SignalDirection.BUY,
    strength: float = 0.5,
    stop_loss_pct: float = 0.05,
) -> Signal:
    return Signal(
        symbol=symbol,
        direction=direction,
        strength=strength,
        stop_loss_pct=stop_loss_pct,
        strategy_name="test",
        reason="test signal",
        timestamp=datetime.now(UTC),
    )


def _make_position(symbol: str = "QQQ") -> Position:
    return Position(
        symbol=symbol, qty=10, entry_price=100.0,
        stop_loss_price=95.0, take_profit_price=110.0,
        broker_order_id="test-123", strategy_name="test",
        entry_date=date.today(),
    )


def _mock_account(
    equity: str = "10000",
    buying_power: str = "8000",
    cash: str = "8000",
    last_equity: str = "9900",
) -> SimpleNamespace:
    return SimpleNamespace(
        equity=equity, buying_power=buying_power, cash=cash, last_equity=last_equity,
    )


def _make_engine(
    monkeypatch,
    broker: MagicMock | None = None,
    risk_manager: MagicMock | None = None,
) -> tuple[ExecutionEngine, MagicMock, MagicMock, sessionmaker]:
    monkeypatch.setenv("ALPACA_PAPER_API_KEY", "fake")
    monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "fake")

    db_engine = create_engine("sqlite:///:memory:")
    init_db(db_engine)
    sf = sessionmaker(bind=db_engine)

    mock_broker = broker or MagicMock()
    mock_broker.get_account.return_value = _mock_account()
    mock_broker.get_positions.return_value = []
    mock_broker.get_orders.return_value = []

    mock_risk = risk_manager or MagicMock()
    mock_risk.evaluate.return_value = (10, ValidationResult(approved=True))

    # Build a minimal config
    from bread.core.config import load_config
    config = load_config()

    engine = ExecutionEngine(mock_broker, mock_risk, config, sf)
    return engine, mock_broker, mock_risk, sf


class TestReconcile:
    def test_adds_untracked_broker_position(self, monkeypatch) -> None:
        engine, mock_broker, _, _ = _make_engine(monkeypatch)
        mock_broker.get_positions.return_value = [
            SimpleNamespace(symbol="SPY", qty="5", avg_entry_price="500.0"),
        ]

        engine.reconcile()

        positions = engine.get_positions()
        assert len(positions) == 1
        assert positions[0].symbol == "SPY"
        assert positions[0].qty == 5

    def test_removes_closed_position(self, monkeypatch) -> None:
        engine, mock_broker, _, _ = _make_engine(monkeypatch)
        # Pre-populate a local position
        engine._positions["SPY"] = _make_position("SPY")
        # Broker says no positions
        mock_broker.get_positions.return_value = []

        engine.reconcile()

        assert len(engine.get_positions()) == 0

    def test_keeps_matching_positions(self, monkeypatch) -> None:
        engine, mock_broker, _, _ = _make_engine(monkeypatch)
        engine._positions["SPY"] = _make_position("SPY")
        mock_broker.get_positions.return_value = [
            SimpleNamespace(symbol="SPY", qty="10", avg_entry_price="100.0"),
        ]

        engine.reconcile()

        assert len(engine.get_positions()) == 1


class TestProcessSignals:
    def test_sell_with_position_closes(self, monkeypatch) -> None:
        engine, mock_broker, _, _ = _make_engine(monkeypatch)
        engine._positions["SPY"] = _make_position("SPY")
        mock_broker.close_position.return_value = "close-123"

        signals = [_make_signal("SPY", SignalDirection.SELL)]
        engine.process_signals(signals, {"SPY": 510.0})

        mock_broker.close_position.assert_called_once_with("SPY")
        assert "SPY" not in engine._positions

    def test_sell_without_position_ignored(self, monkeypatch) -> None:
        engine, mock_broker, _, _ = _make_engine(monkeypatch)

        signals = [_make_signal("SPY", SignalDirection.SELL)]
        engine.process_signals(signals, {"SPY": 510.0})

        mock_broker.close_position.assert_not_called()

    def test_buy_already_held_skipped(self, monkeypatch) -> None:
        engine, mock_broker, _, _ = _make_engine(monkeypatch)
        engine._positions["SPY"] = _make_position("SPY")

        signals = [_make_signal("SPY", SignalDirection.BUY)]
        engine.process_signals(signals, {"SPY": 510.0})

        mock_broker.submit_bracket_order.assert_not_called()

    def test_buy_pending_order_skipped(self, monkeypatch) -> None:
        engine, mock_broker, _, _ = _make_engine(monkeypatch)
        mock_broker.get_orders.return_value = [
            SimpleNamespace(symbol="SPY"),
        ]

        signals = [_make_signal("SPY", SignalDirection.BUY)]
        engine.process_signals(signals, {"SPY": 510.0})

        mock_broker.submit_bracket_order.assert_not_called()

    def test_buy_approved_submits_bracket(self, monkeypatch) -> None:
        engine, mock_broker, mock_risk, _ = _make_engine(monkeypatch)
        mock_broker.submit_bracket_order.return_value = "order-abc"
        mock_risk.evaluate.return_value = (10, ValidationResult(approved=True))

        signals = [_make_signal("SPY", SignalDirection.BUY)]
        engine.process_signals(signals, {"SPY": 500.0})

        mock_broker.submit_bracket_order.assert_called_once()
        args = mock_broker.submit_bracket_order.call_args
        assert args[0][0] == "SPY"  # symbol
        assert args[0][1] == 10  # qty
        assert "SPY" in engine._positions

    def test_buy_rejected_no_order(self, monkeypatch) -> None:
        engine, mock_broker, mock_risk, _ = _make_engine(monkeypatch)
        mock_risk.evaluate.return_value = (
            10, ValidationResult(approved=False, rejections=["max positions exceeded"])
        )

        signals = [_make_signal("SPY", SignalDirection.BUY)]
        engine.process_signals(signals, {"SPY": 500.0})

        mock_broker.submit_bracket_order.assert_not_called()

    def test_buy_no_price_skipped(self, monkeypatch) -> None:
        engine, mock_broker, _, _ = _make_engine(monkeypatch)

        signals = [_make_signal("SPY", SignalDirection.BUY)]
        engine.process_signals(signals, {})  # no prices

        mock_broker.submit_bracket_order.assert_not_called()

    def test_sell_before_buy_ordering(self, monkeypatch) -> None:
        engine, mock_broker, mock_risk, _ = _make_engine(monkeypatch)
        engine._positions["SPY"] = _make_position("SPY")
        mock_broker.close_position.return_value = "close-123"
        mock_broker.submit_bracket_order.return_value = "buy-456"
        mock_risk.evaluate.return_value = (5, ValidationResult(approved=True))

        signals = [
            _make_signal("QQQ", SignalDirection.BUY),
            _make_signal("SPY", SignalDirection.SELL),
        ]
        engine.process_signals(signals, {"SPY": 510.0, "QQQ": 400.0})

        # SELL should happen first
        mock_broker.close_position.assert_called_once_with("SPY")
        mock_broker.submit_bracket_order.assert_called_once()

    def test_order_logged_to_db(self, monkeypatch) -> None:
        engine, mock_broker, mock_risk, sf = _make_engine(monkeypatch)
        mock_broker.submit_bracket_order.return_value = "order-abc"
        mock_risk.evaluate.return_value = (10, ValidationResult(approved=True))

        signals = [_make_signal("SPY", SignalDirection.BUY)]
        engine.process_signals(signals, {"SPY": 500.0})

        with sf() as session:
            orders = session.execute(select(OrderLog)).scalars().all()
            assert len(orders) >= 1
            assert orders[0].symbol == "SPY"
            assert orders[0].side == "BUY"

    def test_rejected_order_logged_to_db(self, monkeypatch) -> None:
        engine, mock_broker, mock_risk, sf = _make_engine(monkeypatch)
        mock_risk.evaluate.return_value = (
            10, ValidationResult(approved=False, rejections=["daily loss limit"])
        )

        signals = [_make_signal("SPY", SignalDirection.BUY)]
        engine.process_signals(signals, {"SPY": 500.0})

        with sf() as session:
            orders = session.execute(select(OrderLog)).scalars().all()
            assert len(orders) == 1
            assert orders[0].status == "REJECTED"


class TestSaveSnapshot:
    def test_saves_snapshot(self, monkeypatch) -> None:
        engine, mock_broker, _, sf = _make_engine(monkeypatch)

        engine.save_snapshot()

        with sf() as session:
            snapshots = session.execute(select(PortfolioSnapshot)).scalars().all()
            assert len(snapshots) == 1
            assert snapshots[0].equity == 10_000.0
            assert snapshots[0].daily_pnl == 100.0  # 10000 - 9900


class TestPeakEquity:
    def test_returns_current_on_empty_db(self, monkeypatch) -> None:
        engine, _, _, _ = _make_engine(monkeypatch)
        assert engine._get_peak_equity(10_000.0) == 10_000.0

    def test_returns_max_from_snapshots(self, monkeypatch) -> None:
        engine, _, _, sf = _make_engine(monkeypatch)
        with sf() as session:
            session.add(PortfolioSnapshot(
                timestamp_utc=datetime.now(UTC), equity=12_000.0,
                cash=10_000.0, positions_value=2_000.0, open_positions=1, daily_pnl=100.0,
            ))
            session.commit()

        assert engine._get_peak_equity(10_000.0) == 12_000.0

    def test_returns_current_when_higher_than_db(self, monkeypatch) -> None:
        engine, _, _, sf = _make_engine(monkeypatch)
        with sf() as session:
            session.add(PortfolioSnapshot(
                timestamp_utc=datetime.now(UTC), equity=8_000.0,
                cash=6_000.0, positions_value=2_000.0, open_positions=1, daily_pnl=0.0,
            ))
            session.commit()

        assert engine._get_peak_equity(10_000.0) == 10_000.0


def _make_snapshot(
    timestamp: datetime,
    equity: float = 10_000.0,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        timestamp_utc=timestamp, equity=equity,
        cash=equity, positions_value=0.0, open_positions=0, daily_pnl=0.0,
    )


class TestWeeklyPnl:
    def test_no_snapshots_returns_zero(self, monkeypatch) -> None:
        engine, _, _, _ = _make_engine(monkeypatch)
        assert engine._get_weekly_pnl(10_000.0) == 0.0

    def test_computes_change_from_week_start(self, monkeypatch) -> None:
        engine, _, _, sf = _make_engine(monkeypatch)
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        week_start = datetime(monday.year, monday.month, monday.day, tzinfo=UTC)

        with sf() as session:
            # Earliest snapshot this week — the baseline
            session.add(_make_snapshot(week_start + timedelta(hours=1), equity=9_500.0))
            # Later snapshot
            session.add(_make_snapshot(week_start + timedelta(hours=10), equity=9_800.0))
            session.commit()

        # weekly P&L = current_equity - earliest_this_week
        assert engine._get_weekly_pnl(10_000.0) == 500.0  # 10000 - 9500

    def test_ignores_last_week_snapshots(self, monkeypatch) -> None:
        engine, _, _, sf = _make_engine(monkeypatch)
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        week_start = datetime(monday.year, monday.month, monday.day, tzinfo=UTC)

        with sf() as session:
            # Last week — should be ignored
            session.add(_make_snapshot(week_start - timedelta(days=1), equity=8_000.0))
            # This week
            session.add(_make_snapshot(week_start + timedelta(hours=1), equity=9_500.0))
            session.commit()

        assert engine._get_weekly_pnl(10_000.0) == 500.0


class TestDayTradeCount:
    def test_no_orders_returns_zero(self, monkeypatch) -> None:
        engine, _, _, _ = _make_engine(monkeypatch)
        assert engine._get_day_trade_count() == 0

    def test_counts_same_day_buy_and_sell(self, monkeypatch) -> None:
        engine, _, _, sf = _make_engine(monkeypatch)
        now = datetime.now(UTC)
        with sf() as session:
            session.add(OrderLog(
                broker_order_id="b1", symbol="SPY", side="BUY", qty=10,
                status="FILLED", strategy_name="test", reason="buy",
                created_at_utc=now, filled_at_utc=now,
            ))
            session.add(OrderLog(
                broker_order_id="s1", symbol="SPY", side="SELL", qty=10,
                status="FILLED", strategy_name="test", reason="sell",
                created_at_utc=now, filled_at_utc=now,
            ))
            session.commit()

        assert engine._get_day_trade_count() == 1

    def test_buy_only_not_counted(self, monkeypatch) -> None:
        engine, _, _, sf = _make_engine(monkeypatch)
        now = datetime.now(UTC)
        with sf() as session:
            session.add(OrderLog(
                broker_order_id="b1", symbol="SPY", side="BUY", qty=10,
                status="FILLED", strategy_name="test", reason="buy",
                created_at_utc=now, filled_at_utc=now,
            ))
            session.commit()

        assert engine._get_day_trade_count() == 0

    def test_old_trades_excluded(self, monkeypatch) -> None:
        engine, _, _, sf = _make_engine(monkeypatch)
        old = datetime.now(UTC) - timedelta(days=10)
        with sf() as session:
            session.add(OrderLog(
                broker_order_id="b1", symbol="SPY", side="BUY", qty=10,
                status="FILLED", strategy_name="test", reason="buy",
                created_at_utc=old, filled_at_utc=old,
            ))
            session.add(OrderLog(
                broker_order_id="s1", symbol="SPY", side="SELL", qty=10,
                status="FILLED", strategy_name="test", reason="sell",
                created_at_utc=old, filled_at_utc=old,
            ))
            session.commit()

        assert engine._get_day_trade_count() == 0

    def test_multiple_symbols_counted_separately(self, monkeypatch) -> None:
        engine, _, _, sf = _make_engine(monkeypatch)
        now = datetime.now(UTC)
        with sf() as session:
            for sym in ("SPY", "QQQ"):
                session.add(OrderLog(
                    broker_order_id=f"b-{sym}", symbol=sym, side="BUY", qty=10,
                    status="FILLED", strategy_name="test", reason="buy",
                    created_at_utc=now, filled_at_utc=now,
                ))
                session.add(OrderLog(
                    broker_order_id=f"s-{sym}", symbol=sym, side="SELL", qty=10,
                    status="FILLED", strategy_name="test", reason="sell",
                    created_at_utc=now, filled_at_utc=now,
                ))
            session.commit()

        assert engine._get_day_trade_count() == 2

    def test_null_filled_at_ignored(self, monkeypatch) -> None:
        engine, _, _, sf = _make_engine(monkeypatch)
        now = datetime.now(UTC)
        with sf() as session:
            session.add(OrderLog(
                broker_order_id="b1", symbol="SPY", side="BUY", qty=10,
                status="FILLED", strategy_name="test", reason="buy",
                created_at_utc=now, filled_at_utc=None,
            ))
            session.add(OrderLog(
                broker_order_id="s1", symbol="SPY", side="SELL", qty=10,
                status="FILLED", strategy_name="test", reason="sell",
                created_at_utc=now, filled_at_utc=now,
            ))
            session.commit()

        # buy has null filled_at so it's skipped — no complete day trade
        assert engine._get_day_trade_count() == 0


class TestReconcileOrders:
    def test_updates_filled_order(self, monkeypatch) -> None:
        engine, mock_broker, _, sf = _make_engine(monkeypatch)
        # Insert a pending order
        now = datetime.now(UTC)
        with sf() as session:
            session.add(OrderLog(
                broker_order_id="order-1", symbol="SPY", side="BUY", qty=10,
                status="PENDING", strategy_name="test", reason="test",
                created_at_utc=now,
            ))
            session.commit()

        # Broker says it's filled
        fill_time = now + timedelta(minutes=5)
        mock_broker.get_orders.return_value = [
            SimpleNamespace(
                id="order-1", status="filled", filled_avg_price="502.50",
                filled_at=fill_time,
            ),
        ]

        engine._reconcile_orders()

        with sf() as session:
            order = session.execute(select(OrderLog)).scalars().first()
            assert order is not None
            assert order.status == "FILLED"
            # Paper cost model adjusts fill: raw * (1 + 0.001) for BUY
            assert order.raw_filled_price == 502.50
            assert order.filled_price == pytest.approx(502.50 * 1.001)
            # SQLite strips tzinfo on round-trip
            assert order.filled_at_utc == fill_time.replace(tzinfo=None)

    def test_updates_cancelled_order(self, monkeypatch) -> None:
        engine, mock_broker, _, sf = _make_engine(monkeypatch)
        now = datetime.now(UTC)
        with sf() as session:
            session.add(OrderLog(
                broker_order_id="order-2", symbol="QQQ", side="BUY", qty=5,
                status="PENDING", strategy_name="test", reason="test",
                created_at_utc=now,
            ))
            session.commit()

        mock_broker.get_orders.return_value = [
            SimpleNamespace(
                id="order-2", status="cancelled", filled_avg_price=None,
                filled_at=None,
            ),
        ]

        engine._reconcile_orders()

        with sf() as session:
            order = session.execute(select(OrderLog)).scalars().first()
            assert order is not None
            assert order.status == "CANCELLED"
            assert order.filled_price is None

    def test_skips_already_filled(self, monkeypatch) -> None:
        engine, mock_broker, _, sf = _make_engine(monkeypatch)
        now = datetime.now(UTC)
        with sf() as session:
            session.add(OrderLog(
                broker_order_id="order-3", symbol="SPY", side="BUY", qty=10,
                status="FILLED", strategy_name="test", reason="test",
                created_at_utc=now, filled_price=500.0, filled_at_utc=now,
            ))
            session.commit()

        # Should not even fetch broker orders since no pending
        engine._reconcile_orders()
        mock_broker.get_orders.assert_not_called()

    def test_broker_failure_handled(self, monkeypatch) -> None:
        engine, mock_broker, _, sf = _make_engine(monkeypatch)
        now = datetime.now(UTC)
        with sf() as session:
            session.add(OrderLog(
                broker_order_id="order-4", symbol="SPY", side="BUY", qty=10,
                status="PENDING", strategy_name="test", reason="test",
                created_at_utc=now,
            ))
            session.commit()

        mock_broker.get_orders.side_effect = Exception("API down")

        # Should not raise
        engine._reconcile_orders()

        # Order should remain unchanged
        with sf() as session:
            order = session.execute(select(OrderLog)).scalars().first()
            assert order is not None
            assert order.status == "PENDING"

    def test_skips_orders_without_broker_id(self, monkeypatch) -> None:
        engine, mock_broker, _, sf = _make_engine(monkeypatch)
        now = datetime.now(UTC)
        with sf() as session:
            # Rejected orders have no broker_order_id
            session.add(OrderLog(
                broker_order_id=None, symbol="SPY", side="BUY", qty=10,
                status="PENDING", strategy_name="test", reason="test",
                created_at_utc=now,
            ))
            session.commit()

        mock_broker.get_orders.return_value = []
        engine._reconcile_orders()

        with sf() as session:
            order = session.execute(select(OrderLog)).scalars().first()
            assert order is not None
            assert order.status == "PENDING"


class TestProcessSignalsExceptionPaths:
    def test_get_orders_failure_proceeds(self, monkeypatch) -> None:
        """When get_orders raises, processing continues with empty pending set."""
        engine, mock_broker, mock_risk, _ = _make_engine(monkeypatch)
        mock_broker.get_orders.side_effect = Exception("API error")
        mock_broker.submit_bracket_order.return_value = "order-abc"
        mock_risk.evaluate.return_value = (10, ValidationResult(approved=True))

        signals = [_make_signal("SPY", SignalDirection.BUY)]
        engine.process_signals(signals, {"SPY": 500.0})

        # Should still attempt the buy since pending_symbols defaults to empty
        mock_broker.submit_bracket_order.assert_called_once()

    def test_get_account_failure_aborts(self, monkeypatch) -> None:
        """When get_account raises, all signal processing is skipped."""
        engine, mock_broker, _, _ = _make_engine(monkeypatch)
        mock_broker.get_account.side_effect = Exception("API error")

        signals = [_make_signal("SPY", SignalDirection.BUY)]
        engine.process_signals(signals, {"SPY": 500.0})

        mock_broker.submit_bracket_order.assert_not_called()

    def test_close_position_failure_continues(self, monkeypatch) -> None:
        """When close_position raises OrderError, other signals still process."""
        engine, mock_broker, mock_risk, _ = _make_engine(monkeypatch)
        engine._positions["SPY"] = _make_position("SPY")
        mock_broker.close_position.side_effect = OrderError("API error")
        mock_broker.submit_bracket_order.return_value = "order-abc"
        mock_risk.evaluate.return_value = (5, ValidationResult(approved=True))

        signals = [
            _make_signal("SPY", SignalDirection.SELL),
            _make_signal("QQQ", SignalDirection.BUY),
        ]
        engine.process_signals(signals, {"SPY": 510.0, "QQQ": 400.0})

        # SELL failed but BUY should still proceed
        mock_broker.submit_bracket_order.assert_called_once()

    def test_submit_bracket_failure_continues(self, monkeypatch) -> None:
        """When submit_bracket_order raises OrderError, next signal still processes."""
        engine, mock_broker, mock_risk, _ = _make_engine(monkeypatch)
        mock_broker.submit_bracket_order.side_effect = [
            OrderError("fail"), "order-abc",
        ]
        mock_risk.evaluate.return_value = (5, ValidationResult(approved=True))

        signals = [
            _make_signal("SPY", SignalDirection.BUY, strength=0.9),
            _make_signal("QQQ", SignalDirection.BUY, strength=0.5),
        ]
        engine.process_signals(signals, {"SPY": 500.0, "QQQ": 400.0})

        # SPY fails, QQQ succeeds
        assert "SPY" not in engine._positions
        assert "QQQ" in engine._positions


class TestPaperCostAdjustment:
    """Tests for the paper trading cost simulation layer.

    Alpaca paper trading fills at quoted prices with no spread/slippage.
    The cost model adjusts fill prices to reflect real-world friction:
    - BUY:  price * (1 + slippage_pct)  — we pay more
    - SELL: price * (1 - slippage_pct)  — we receive less
    """

    def test_adjust_fill_price_buy_paper(self, monkeypatch) -> None:
        engine, _, _, _ = _make_engine(monkeypatch)
        adjusted = engine._adjust_fill_price(500.0, "BUY")
        assert adjusted == pytest.approx(500.0 * 1.001)

    def test_adjust_fill_price_sell_paper(self, monkeypatch) -> None:
        engine, _, _, _ = _make_engine(monkeypatch)
        adjusted = engine._adjust_fill_price(500.0, "SELL")
        assert adjusted == pytest.approx(500.0 * 0.999)

    def test_adjust_fill_price_live_unchanged(self, monkeypatch) -> None:
        engine, _, _, _ = _make_engine(monkeypatch)
        engine._config.mode = "live"
        assert engine._adjust_fill_price(500.0, "BUY") == 500.0
        assert engine._adjust_fill_price(500.0, "SELL") == 500.0

    def test_adjust_fill_price_disabled(self, monkeypatch) -> None:
        engine, _, _, _ = _make_engine(monkeypatch)
        engine._config.execution.paper_cost.enabled = False
        assert engine._adjust_fill_price(500.0, "BUY") == 500.0
        assert engine._adjust_fill_price(500.0, "SELL") == 500.0

    def test_reconcile_stores_raw_and_adjusted(self, monkeypatch) -> None:
        """BUY order should have raw_filled_price < filled_price (slippage)."""
        engine, mock_broker, _, sf = _make_engine(monkeypatch)
        now = datetime.now(UTC)
        with sf() as session:
            session.add(OrderLog(
                broker_order_id="order-buy", symbol="SPY", side="BUY", qty=10,
                status="PENDING", strategy_name="test", reason="test",
                created_at_utc=now,
            ))
            session.commit()

        mock_broker.get_orders.return_value = [
            SimpleNamespace(
                id="order-buy", status="filled", filled_avg_price="500.00",
                filled_at=now,
            ),
        ]
        engine._reconcile_orders()

        with sf() as session:
            order = session.execute(select(OrderLog)).scalars().first()
            assert order is not None
            assert order.raw_filled_price == 500.0
            assert order.filled_price == pytest.approx(500.0 * 1.001)

    def test_reconcile_sell_adjusts_downward(self, monkeypatch) -> None:
        """SELL order should have filled_price < raw_filled_price."""
        engine, mock_broker, _, sf = _make_engine(monkeypatch)
        now = datetime.now(UTC)
        with sf() as session:
            session.add(OrderLog(
                broker_order_id="order-sell", symbol="SPY", side="SELL", qty=10,
                status="PENDING", strategy_name="test", reason="test",
                created_at_utc=now,
            ))
            session.commit()

        mock_broker.get_orders.return_value = [
            SimpleNamespace(
                id="order-sell", status="filled", filled_avg_price="510.00",
                filled_at=now,
            ),
        ]
        engine._reconcile_orders()

        with sf() as session:
            order = session.execute(select(OrderLog)).scalars().first()
            assert order is not None
            assert order.raw_filled_price == 510.0
            assert order.filled_price == pytest.approx(510.0 * 0.999)

    def test_snapshot_applies_cost_adjustment(self, monkeypatch) -> None:
        """In paper mode, snapshot equity should be reduced by cumulative cost drag."""
        engine, mock_broker, _, sf = _make_engine(monkeypatch)
        now = datetime.now(UTC)

        # Insert a filled BUY order with cost adjustment
        raw_buy = 500.0
        adj_buy = 500.0 * 1.001  # paid more
        with sf() as session:
            session.add(OrderLog(
                broker_order_id="b1", symbol="SPY", side="BUY", qty=10,
                status="FILLED", strategy_name="test", reason="test",
                created_at_utc=now, filled_at_utc=now,
                raw_filled_price=raw_buy, filled_price=adj_buy,
            ))
            session.commit()

        engine.save_snapshot()

        # Expected cost drag: (raw_buy - adj_buy) * 10 = -5.0
        expected_drag = (raw_buy - adj_buy) * 10
        with sf() as session:
            snap = session.execute(select(PortfolioSnapshot)).scalars().first()
            assert snap is not None
            assert snap.equity == pytest.approx(10_000.0 + expected_drag)

    def test_snapshot_no_adjustment_live(self, monkeypatch) -> None:
        """Live mode snapshot should equal Alpaca-reported equity."""
        engine, mock_broker, _, sf = _make_engine(monkeypatch)
        engine._config.mode = "live"
        now = datetime.now(UTC)

        # Insert a filled order (live mode should ignore cost adjustment)
        with sf() as session:
            session.add(OrderLog(
                broker_order_id="b1", symbol="SPY", side="BUY", qty=10,
                status="FILLED", strategy_name="test", reason="test",
                created_at_utc=now, filled_at_utc=now,
                raw_filled_price=500.0, filled_price=500.5,
            ))
            session.commit()

        engine.save_snapshot()

        with sf() as session:
            snap = session.execute(select(PortfolioSnapshot)).scalars().first()
            assert snap is not None
            assert snap.equity == 10_000.0  # no adjustment

    def test_cumulative_adjustment_empty_db(self, monkeypatch) -> None:
        engine, _, _, _ = _make_engine(monkeypatch)
        assert engine._get_cumulative_cost_adjustment() == 0.0

    def test_cumulative_adjustment_with_commission(self, monkeypatch) -> None:
        engine, _, _, sf = _make_engine(monkeypatch)
        engine._config.execution.paper_cost.commission_per_trade = 1.0
        now = datetime.now(UTC)

        # One BUY and one SELL, each with $1 commission
        with sf() as session:
            session.add(OrderLog(
                broker_order_id="b1", symbol="SPY", side="BUY", qty=10,
                status="FILLED", strategy_name="test", reason="test",
                created_at_utc=now, filled_at_utc=now,
                raw_filled_price=500.0, filled_price=500.5,
            ))
            session.add(OrderLog(
                broker_order_id="s1", symbol="SPY", side="SELL", qty=10,
                status="FILLED", strategy_name="test", reason="test",
                created_at_utc=now, filled_at_utc=now,
                raw_filled_price=510.0, filled_price=509.49,
            ))
            session.commit()

        adj = engine._get_cumulative_cost_adjustment()
        # BUY drag: (500.0 - 500.5) * 10 = -5.0
        # SELL drag: (509.49 - 510.0) * 10 = -5.1
        # Commission: 2 orders * $1 = -2.0
        expected = (500.0 - 500.5) * 10 + (509.49 - 510.0) * 10 - 2 * 1.0
        assert adj == pytest.approx(expected)
