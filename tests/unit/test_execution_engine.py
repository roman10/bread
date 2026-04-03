"""Tests for execution.engine."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from bread.core.config import AppConfig, ExecutionSettings, RiskSettings
from bread.core.models import OrderSide, OrderStatus, Position, Signal, SignalDirection
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
