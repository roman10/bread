"""Tests for execution.engine."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from bread.ai.models import SignalReview
from bread.core.exceptions import OrderError
from bread.core.models import OrderSide, OrderStatus, Position, Signal, SignalDirection
from bread.db.database import init_db
from bread.db.models import OrderLog, PortfolioSnapshot
from bread.execution.engine import ExecutionEngine
from bread.execution.models import Account, BracketOrderIds, BrokerOrder, BrokerPosition
from bread.risk.validators import ValidationResult


def _bracket(parent_id: str, stop: str = "", tp: str = "") -> BracketOrderIds:
    return BracketOrderIds(
        parent_order_id=parent_id,
        stop_loss_order_id=stop,
        take_profit_order_id=tp,
    )


def _make_signal(
    symbol: str = "SPY",
    direction: SignalDirection = SignalDirection.BUY,
    strength: float = 0.5,
    stop_loss_pct: float = 0.05,
    strategy_name: str = "test",
) -> Signal:
    return Signal(
        symbol=symbol,
        direction=direction,
        strength=strength,
        stop_loss_pct=stop_loss_pct,
        strategy_name=strategy_name,
        reason="test signal",
        timestamp=datetime.now(UTC),
    )


def _make_position(symbol: str = "QQQ") -> Position:
    return Position(
        symbol=symbol,
        qty=10,
        entry_price=100.0,
        stop_loss_price=95.0,
        take_profit_price=110.0,
        broker_order_id="test-123",
        strategy_name="test",
        entry_date=date.today(),
    )


def _mock_account(
    equity: float = 10000.0,
    buying_power: float = 8000.0,
    cash: float = 8000.0,
    last_equity: float = 9900.0,
) -> Account:
    return Account(
        equity=equity,
        buying_power=buying_power,
        cash=cash,
        last_equity=last_equity,
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )


def _broker_position(
    symbol: str,
    qty: float = 0.0,
    avg_entry_price: float = 0.0,
    current_price: float | None = None,
) -> BrokerPosition:
    cp = current_price if current_price is not None else avg_entry_price
    return BrokerPosition(
        symbol=symbol,
        qty=qty,
        avg_entry_price=avg_entry_price,
        current_price=cp,
        market_value=cp * qty,
        unrealized_pl=0.0,
        unrealized_plpc=0.0,
    )


def _broker_order(
    id: str = "order-1",
    symbol: str = "SPY",
    status: OrderStatus | None = OrderStatus.PENDING,
    side: OrderSide | None = OrderSide.BUY,
    qty: float = 0.0,
    filled_avg_price: float | None = None,
    filled_at: datetime | None = None,
) -> BrokerOrder:
    return BrokerOrder(
        id=id,
        symbol=symbol,
        side=side,
        status=status,
        qty=qty,
        filled_qty=qty if filled_avg_price is not None else 0.0,
        filled_avg_price=filled_avg_price,
        submitted_at=None,
        created_at=None,
        filled_at=filled_at,
        order_type="market",
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
    mock_broker.get_order_by_id.return_value = None
    mock_broker.submit_bracket_order.return_value = _bracket("order-default")
    mock_broker.submit_market_sell.return_value = "sell-default"
    mock_broker.cancel_order.return_value = True

    if risk_manager is None:
        mock_risk = MagicMock()
        mock_risk.evaluate.return_value = (10, ValidationResult(approved=True))
    else:
        mock_risk = risk_manager

    # Build a minimal config
    from bread.core.config import load_config

    config = load_config()

    engine = ExecutionEngine(mock_broker, mock_risk, config, sf)
    return engine, mock_broker, mock_risk, sf


class TestReconcile:
    def test_adds_untracked_broker_position(self, monkeypatch) -> None:
        engine, mock_broker, _, _ = _make_engine(monkeypatch)
        mock_broker.get_positions.return_value = [
            _broker_position("SPY", qty=5.0, avg_entry_price=500.0),
        ]

        engine.reconcile()

        positions = engine.get_positions()
        assert len(positions) == 1
        assert positions[0].symbol == "SPY"
        assert positions[0].qty == 5
        # Broker-found positions must land in the "unknown" bucket so they
        # stay visible without being eligible for any strategy's SELL.
        assert positions[0].strategy_name == "unknown"

    def test_removes_closed_position(self, monkeypatch) -> None:
        engine, mock_broker, _, _ = _make_engine(monkeypatch)
        # Pre-populate a local position
        engine._positions.append(_make_position("SPY"))
        # Broker says no positions
        mock_broker.get_positions.return_value = []

        engine.reconcile()

        assert len(engine.get_positions()) == 0

    def test_keeps_matching_positions(self, monkeypatch) -> None:
        engine, mock_broker, _, _ = _make_engine(monkeypatch)
        engine._positions.append(_make_position("SPY"))
        mock_broker.get_positions.return_value = [
            _broker_position("SPY", qty=10.0, avg_entry_price=100.0),
        ]

        engine.reconcile()

        assert len(engine.get_positions()) == 1


class TestProcessSignals:
    def test_sell_with_position_closes(self, monkeypatch) -> None:
        engine, mock_broker, _, _ = _make_engine(monkeypatch)
        engine._positions.append(_make_position("SPY"))
        mock_broker.submit_market_sell.return_value = "sell-123"

        signals = [_make_signal("SPY", SignalDirection.SELL)]
        engine.process_signals(signals, {"SPY": 510.0})

        mock_broker.submit_market_sell.assert_called_once_with("SPY", 10)
        mock_broker.close_position.assert_not_called()
        assert not any(p.symbol == "SPY" for p in engine._positions)

    def test_cross_strategy_sell_blocked_by_engine_guard(
        self, monkeypatch
    ) -> None:
        """A SELL signal from a strategy that doesn't own the position must be
        ignored. Strategy ownership is enforced at both app.py (primary) and
        engine (defense-in-depth). This test covers the engine guard.
        """
        engine, mock_broker, _, sf = _make_engine(monkeypatch)
        opener_position = Position(
            symbol="SPY", qty=10, entry_price=500.0,
            stop_loss_price=475.0, take_profit_price=525.0,
            broker_order_id="open-1", strategy_name="ema_crossover",
            entry_date=date.today(),
        )
        engine._positions.append(opener_position)

        exit_signal = Signal(
            symbol="SPY", direction=SignalDirection.SELL,
            strength=0.8, stop_loss_pct=0.05,
            strategy_name="bb_mean_reversion",  # different from opener — must be blocked
            reason="rsi>70", timestamp=datetime.now(UTC),
        )
        engine.process_signals([exit_signal], {"SPY": 510.0})

        mock_broker.close_position.assert_not_called()
        mock_broker.submit_market_sell.assert_not_called()
        assert any(p.symbol == "SPY" for p in engine._positions)  # position untouched

        with sf() as session:
            sell_row = (
                session.execute(select(OrderLog).where(OrderLog.side == "SELL"))
                .scalars().first()
            )
            assert sell_row is None  # no order logged

    def test_sell_without_position_ignored(self, monkeypatch) -> None:
        engine, mock_broker, _, _ = _make_engine(monkeypatch)

        signals = [_make_signal("SPY", SignalDirection.SELL)]
        engine.process_signals(signals, {"SPY": 510.0})

        mock_broker.close_position.assert_not_called()
        mock_broker.submit_market_sell.assert_not_called()

    def test_buy_already_held_skipped(self, monkeypatch) -> None:
        engine, mock_broker, _, _ = _make_engine(monkeypatch)
        engine._positions.append(_make_position("SPY"))

        signals = [_make_signal("SPY", SignalDirection.BUY)]
        engine.process_signals(signals, {"SPY": 510.0})

        mock_broker.submit_bracket_order.assert_not_called()

    def test_buy_pending_order_skipped(self, monkeypatch) -> None:
        """Pending OrderLog row for the same (symbol, strategy) blocks a duplicate BUY.

        Pending detection is OrderLog-based (not broker-based) so a pending
        order owned by strategy A does not block strategy B from opening the
        same symbol.
        """
        engine, mock_broker, _, sf = _make_engine(monkeypatch)
        now = datetime.now(UTC)
        with sf() as session:
            session.add(
                OrderLog(
                    broker_order_id="o-pending",
                    symbol="SPY",
                    side="BUY",
                    qty=10,
                    status="PENDING",
                    strategy_name="test",
                    reason="prior tick",
                    created_at_utc=now,
                )
            )
            session.commit()

        signals = [_make_signal("SPY", SignalDirection.BUY, strategy_name="test")]
        engine.process_signals(signals, {"SPY": 510.0})

        mock_broker.submit_bracket_order.assert_not_called()

    def test_pending_buy_does_not_block_other_strategy(self, monkeypatch) -> None:
        """A strategy-A pending BUY must not block strategy B's BUY on the same symbol."""
        engine, mock_broker, mock_risk, sf = _make_engine(monkeypatch)
        mock_broker.submit_bracket_order.return_value = _bracket("order-b")
        mock_risk.evaluate.return_value = (10, ValidationResult(approved=True))
        now = datetime.now(UTC)
        with sf() as session:
            session.add(
                OrderLog(
                    broker_order_id="o-pending-a",
                    symbol="SPY",
                    side="BUY",
                    qty=10,
                    status="PENDING",
                    strategy_name="strategy_a",
                    reason="prior tick",
                    created_at_utc=now,
                )
            )
            session.commit()

        signals = [_make_signal("SPY", SignalDirection.BUY, strategy_name="strategy_b")]
        engine.process_signals(signals, {"SPY": 510.0})

        mock_broker.submit_bracket_order.assert_called_once()

    def test_same_strategy_duplicate_symbol_in_tick_submits_once(
        self, monkeypatch
    ) -> None:
        engine, mock_broker, mock_risk, _ = _make_engine(monkeypatch)
        mock_broker.submit_bracket_order.return_value = _bracket("order-1")
        mock_risk.evaluate.return_value = (10, ValidationResult(approved=True))

        signals = [
            _make_signal("DIA", strength=0.8, strategy_name="sector_rotation"),
            _make_signal("DIA", strength=0.5, strategy_name="sector_rotation"),
        ]
        engine.process_signals(signals, {"DIA": 100.0})

        assert mock_broker.submit_bracket_order.call_count == 1

    def test_different_strategies_same_symbol_both_submit(self, monkeypatch) -> None:
        """Cross-strategy dupes are intentional — guard against re-tightening
        this check to symbol-only dedup."""
        engine, mock_broker, mock_risk, _ = _make_engine(monkeypatch)
        mock_broker.submit_bracket_order.side_effect = [_bracket("order-1"), _bracket("order-2")]
        mock_risk.evaluate.return_value = (10, ValidationResult(approved=True))

        signals = [
            _make_signal("DIA", strategy_name="sector_rotation"),
            _make_signal("DIA", strategy_name="risk_off_rotation"),
        ]
        engine.process_signals(signals, {"DIA": 100.0})

        assert mock_broker.submit_bracket_order.call_count == 2

    def test_buy_approved_submits_bracket(self, monkeypatch) -> None:
        engine, mock_broker, mock_risk, _ = _make_engine(monkeypatch)
        mock_broker.submit_bracket_order.return_value = _bracket("order-abc")
        mock_risk.evaluate.return_value = (10, ValidationResult(approved=True))

        signals = [_make_signal("SPY", SignalDirection.BUY)]
        engine.process_signals(signals, {"SPY": 500.0})

        mock_broker.submit_bracket_order.assert_called_once()
        args = mock_broker.submit_bracket_order.call_args
        assert args[0][0] == "SPY"  # symbol
        assert args[0][1] == 10  # qty
        assert any(p.symbol == "SPY" for p in engine._positions)

    def test_buy_rejected_no_order(self, monkeypatch) -> None:
        engine, mock_broker, mock_risk, _ = _make_engine(monkeypatch)
        mock_risk.evaluate.return_value = (
            10,
            ValidationResult(approved=False, rejections=["max positions exceeded"]),
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
        engine._positions.append(_make_position("SPY"))
        mock_broker.submit_market_sell.return_value = "sell-123"
        mock_broker.submit_bracket_order.return_value = _bracket("buy-456")
        mock_risk.evaluate.return_value = (5, ValidationResult(approved=True))

        signals = [
            _make_signal("QQQ", SignalDirection.BUY),
            _make_signal("SPY", SignalDirection.SELL),
        ]
        engine.process_signals(signals, {"SPY": 510.0, "QQQ": 400.0})

        # SELL should happen first
        mock_broker.submit_market_sell.assert_called_once_with("SPY", 10)
        mock_broker.submit_bracket_order.assert_called_once()

    def test_order_logged_to_db(self, monkeypatch) -> None:
        engine, mock_broker, mock_risk, sf = _make_engine(monkeypatch)
        mock_broker.submit_bracket_order.return_value = _bracket("order-abc")
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
            10,
            ValidationResult(approved=False, rejections=["daily loss limit"]),
        )

        signals = [_make_signal("SPY", SignalDirection.BUY)]
        engine.process_signals(signals, {"SPY": 500.0})

        with sf() as session:
            orders = session.execute(select(OrderLog)).scalars().all()
            assert len(orders) == 1
            assert orders[0].status == "REJECTED"

    def test_buy_with_invalid_bracket_prices_rejected(self, monkeypatch) -> None:
        """A signal with stop_loss_pct >= 1.0 produces invalid bracket prices."""
        engine, mock_broker, mock_risk, sf = _make_engine(monkeypatch)
        mock_risk.evaluate.return_value = (10, ValidationResult(approved=True))

        # stop_loss_pct=1.5 -> stop_loss_price = 500 * (1 - 1.5) = -250
        signals = [_make_signal("SPY", SignalDirection.BUY, stop_loss_pct=1.5)]
        engine.process_signals(signals, {"SPY": 500.0})

        mock_broker.submit_bracket_order.assert_not_called()
        assert not any(p.symbol == "SPY" for p in engine._positions)
        with sf() as session:
            orders = session.execute(select(OrderLog)).scalars().all()
            assert len(orders) == 1
            assert orders[0].status == "REJECTED"
            assert "invalid bracket" in orders[0].reason


class TestSaveSnapshot:
    def test_saves_snapshot(self, monkeypatch) -> None:
        engine, mock_broker, _, sf = _make_engine(monkeypatch)

        engine.save_snapshot()

        with sf() as session:
            snapshots = session.execute(select(PortfolioSnapshot)).scalars().all()
            assert len(snapshots) == 1
            assert snapshots[0].equity == 10_000.0
            assert snapshots[0].daily_pnl == 100.0  # 10000 - 9900


class TestReconcileOrders:
    def test_status_stored_as_clean_enum_value_not_class_prefixed(
        self, monkeypatch
    ) -> None:
        """The engine must persist OrderStatus.value (e.g. 'FILLED') in the DB.

        The original bug — storing 'ORDERSTATUS.FILLED' from Alpaca's
        `str, Enum` — is now structurally impossible because the broker
        layer normalizes status into our OrderStatus enum before the engine
        sees it. See test_alpaca_broker::TestNormalizeAlpacaStatus for the
        normalization regression itself; this test pins the engine half.
        """
        engine, mock_broker, _, sf = _make_engine(monkeypatch)
        now = datetime.now(UTC)
        with sf() as session:
            session.add(
                OrderLog(
                    broker_order_id="order-regression",
                    symbol="SPY",
                    side="BUY",
                    qty=10,
                    status="PENDING",
                    strategy_name="test",
                    reason="test",
                    created_at_utc=now,
                )
            )
            session.commit()

        mock_broker.get_orders.return_value = [
            _broker_order(
                id="order-regression",
                status=OrderStatus.FILLED,
                filled_avg_price=500.0,
                filled_at=now + timedelta(minutes=1),
            ),
        ]

        engine._reconcile_orders()

        with sf() as session:
            order = session.execute(select(OrderLog)).scalars().first()
            assert order is not None
            assert order.status == "FILLED"  # not "ORDERSTATUS.FILLED"
            assert order.filled_price is not None
            assert order.filled_at_utc is not None

    def test_unknown_status_does_not_overwrite(self, monkeypatch) -> None:
        """A vendor status we don't recognize must leave the row alone, not
        stamp 'UNKNOWN' over a valid prior status.
        """
        engine, mock_broker, _, sf = _make_engine(monkeypatch)
        now = datetime.now(UTC)
        with sf() as session:
            session.add(
                OrderLog(
                    broker_order_id="order-xyz",
                    symbol="QQQ",
                    side="BUY",
                    qty=3,
                    status="PENDING",
                    strategy_name="test",
                    reason="test",
                    created_at_utc=now,
                )
            )
            session.commit()

        mock_broker.get_orders.return_value = [
            _broker_order(id="order-xyz", status=None),
        ]

        engine._reconcile_orders()

        with sf() as session:
            order = session.execute(select(OrderLog)).scalars().first()
            assert order is not None
            assert order.status == "PENDING"  # unchanged

    def test_updates_filled_order(self, monkeypatch) -> None:
        engine, mock_broker, _, sf = _make_engine(monkeypatch)
        # Insert a pending order
        now = datetime.now(UTC)
        with sf() as session:
            session.add(
                OrderLog(
                    broker_order_id="order-1",
                    symbol="SPY",
                    side="BUY",
                    qty=10,
                    status="PENDING",
                    strategy_name="test",
                    reason="test",
                    created_at_utc=now,
                )
            )
            session.commit()

        # Broker says it's filled
        fill_time = now + timedelta(minutes=5)
        mock_broker.get_orders.return_value = [
            _broker_order(
                id="order-1",
                status=OrderStatus.FILLED,
                filled_avg_price=502.50,
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
            session.add(
                OrderLog(
                    broker_order_id="order-2",
                    symbol="QQQ",
                    side="BUY",
                    qty=5,
                    status="PENDING",
                    strategy_name="test",
                    reason="test",
                    created_at_utc=now,
                )
            )
            session.commit()

        mock_broker.get_orders.return_value = [
            _broker_order(id="order-2", status=OrderStatus.CANCELLED),
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
            session.add(
                OrderLog(
                    broker_order_id="order-3",
                    symbol="SPY",
                    side="BUY",
                    qty=10,
                    status="FILLED",
                    strategy_name="test",
                    reason="test",
                    created_at_utc=now,
                    filled_price=500.0,
                    filled_at_utc=now,
                )
            )
            session.commit()

        # Should not even fetch broker orders since no pending
        engine._reconcile_orders()
        mock_broker.get_orders.assert_not_called()

    def test_broker_failure_handled(self, monkeypatch) -> None:
        engine, mock_broker, _, sf = _make_engine(monkeypatch)
        now = datetime.now(UTC)
        with sf() as session:
            session.add(
                OrderLog(
                    broker_order_id="order-4",
                    symbol="SPY",
                    side="BUY",
                    qty=10,
                    status="PENDING",
                    strategy_name="test",
                    reason="test",
                    created_at_utc=now,
                )
            )
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
            session.add(
                OrderLog(
                    broker_order_id=None,
                    symbol="SPY",
                    side="BUY",
                    qty=10,
                    status="PENDING",
                    strategy_name="test",
                    reason="test",
                    created_at_utc=now,
                )
            )
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
        mock_broker.submit_bracket_order.return_value = _bracket("order-abc")
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

    def test_sell_submit_failure_continues(self, monkeypatch) -> None:
        """When submit_market_sell raises OrderError, other signals still process."""
        engine, mock_broker, mock_risk, _ = _make_engine(monkeypatch)
        engine._positions.append(_make_position("SPY"))
        mock_broker.submit_market_sell.side_effect = OrderError("API error")
        mock_broker.submit_bracket_order.return_value = _bracket("order-abc")
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
            OrderError("fail"),
            _bracket("order-abc"),
        ]
        mock_risk.evaluate.return_value = (5, ValidationResult(approved=True))

        signals = [
            _make_signal("SPY", SignalDirection.BUY, strength=0.9),
            _make_signal("QQQ", SignalDirection.BUY, strength=0.5),
        ]
        engine.process_signals(signals, {"SPY": 500.0, "QQQ": 400.0})

        # SPY fails, QQQ succeeds
        assert not any(p.symbol == "SPY" for p in engine._positions)
        assert any(p.symbol == "QQQ" for p in engine._positions)


class TestPaperCostAdjustment:
    """Tests for the paper trading cost simulation layer.

    Alpaca paper trading fills at quoted prices with no spread/slippage.
    The cost model adjusts fill prices to reflect real-world friction:
    - BUY:  price * (1 + slippage_pct)  — we pay more
    - SELL: price * (1 - slippage_pct)  — we receive less
    """

    def test_adjust_fill_price_buy_paper(self, monkeypatch) -> None:
        from bread.execution.engine import adjust_fill_price

        engine, _, _, _ = _make_engine(monkeypatch)
        adjusted = adjust_fill_price(engine._config, 500.0, "BUY")
        assert adjusted == pytest.approx(500.0 * 1.001)

    def test_adjust_fill_price_sell_paper(self, monkeypatch) -> None:
        from bread.execution.engine import adjust_fill_price

        engine, _, _, _ = _make_engine(monkeypatch)
        adjusted = adjust_fill_price(engine._config, 500.0, "SELL")
        assert adjusted == pytest.approx(500.0 * 0.999)

    def test_adjust_fill_price_live_unchanged(self, monkeypatch) -> None:
        from bread.execution.engine import adjust_fill_price

        engine, _, _, _ = _make_engine(monkeypatch)
        engine._config.mode = "live"
        assert adjust_fill_price(engine._config, 500.0, "BUY") == 500.0
        assert adjust_fill_price(engine._config, 500.0, "SELL") == 500.0

    def test_adjust_fill_price_disabled(self, monkeypatch) -> None:
        from bread.execution.engine import adjust_fill_price

        engine, _, _, _ = _make_engine(monkeypatch)
        engine._config.execution.paper_cost.enabled = False
        assert adjust_fill_price(engine._config, 500.0, "BUY") == 500.0
        assert adjust_fill_price(engine._config, 500.0, "SELL") == 500.0

    def test_reconcile_stores_raw_and_adjusted(self, monkeypatch) -> None:
        """BUY order should have raw_filled_price < filled_price (slippage)."""
        engine, mock_broker, _, sf = _make_engine(monkeypatch)
        now = datetime.now(UTC)
        with sf() as session:
            session.add(
                OrderLog(
                    broker_order_id="order-buy",
                    symbol="SPY",
                    side="BUY",
                    qty=10,
                    status="PENDING",
                    strategy_name="test",
                    reason="test",
                    created_at_utc=now,
                )
            )
            session.commit()

        mock_broker.get_orders.return_value = [
            _broker_order(
                id="order-buy",
                status=OrderStatus.FILLED,
                filled_avg_price=500.00,
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
            session.add(
                OrderLog(
                    broker_order_id="order-sell",
                    symbol="SPY",
                    side="SELL",
                    qty=10,
                    status="PENDING",
                    strategy_name="test",
                    reason="test",
                    created_at_utc=now,
                )
            )
            session.commit()

        mock_broker.get_orders.return_value = [
            _broker_order(
                id="order-sell",
                status=OrderStatus.FILLED,
                filled_avg_price=510.00,
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
            session.add(
                OrderLog(
                    broker_order_id="b1",
                    symbol="SPY",
                    side="BUY",
                    qty=10,
                    status="FILLED",
                    strategy_name="test",
                    reason="test",
                    created_at_utc=now,
                    filled_at_utc=now,
                    raw_filled_price=raw_buy,
                    filled_price=adj_buy,
                )
            )
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
            session.add(
                OrderLog(
                    broker_order_id="b1",
                    symbol="SPY",
                    side="BUY",
                    qty=10,
                    status="FILLED",
                    strategy_name="test",
                    reason="test",
                    created_at_utc=now,
                    filled_at_utc=now,
                    raw_filled_price=500.0,
                    filled_price=500.5,
                )
            )
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
            session.add(
                OrderLog(
                    broker_order_id="b1",
                    symbol="SPY",
                    side="BUY",
                    qty=10,
                    status="FILLED",
                    strategy_name="test",
                    reason="test",
                    created_at_utc=now,
                    filled_at_utc=now,
                    raw_filled_price=500.0,
                    filled_price=500.5,
                )
            )
            session.add(
                OrderLog(
                    broker_order_id="s1",
                    symbol="SPY",
                    side="SELL",
                    qty=10,
                    status="FILLED",
                    strategy_name="test",
                    reason="test",
                    created_at_utc=now,
                    filled_at_utc=now,
                    raw_filled_price=510.0,
                    filled_price=509.49,
                )
            )
            session.commit()

        adj = engine._get_cumulative_cost_adjustment()
        # BUY drag: (500.0 - 500.5) * 10 = -5.0
        # SELL drag: (509.49 - 510.0) * 10 = -5.1
        # Commission: 2 adjusted orders * $1 = -2.0
        expected = (500.0 - 500.5) * 10 + (509.49 - 510.0) * 10 - 2 * 1.0
        assert adj == pytest.approx(expected)

    def test_cumulative_adjustment_skips_commission_on_backfilled_orders(
        self,
        monkeypatch,
    ) -> None:
        """Historical orders (raw == adj from migration backfill) should not
        be charged commission, only orders with actual cost adjustment."""
        engine, _, _, sf = _make_engine(monkeypatch)
        engine._config.execution.paper_cost.commission_per_trade = 1.0
        now = datetime.now(UTC)

        with sf() as session:
            # Backfilled historical order (raw == adj, no adjustment applied)
            session.add(
                OrderLog(
                    broker_order_id="old",
                    symbol="SPY",
                    side="BUY",
                    qty=10,
                    status="FILLED",
                    strategy_name="test",
                    reason="test",
                    created_at_utc=now,
                    filled_at_utc=now,
                    raw_filled_price=500.0,
                    filled_price=500.0,
                )
            )
            # New order with cost adjustment applied
            session.add(
                OrderLog(
                    broker_order_id="new",
                    symbol="QQQ",
                    side="BUY",
                    qty=10,
                    status="FILLED",
                    strategy_name="test",
                    reason="test",
                    created_at_utc=now,
                    filled_at_utc=now,
                    raw_filled_price=400.0,
                    filled_price=400.4,
                )
            )
            session.commit()

        adj = engine._get_cumulative_cost_adjustment()
        # Historical: slippage drag = 0, commission = 0 (skipped, raw == adj)
        # New: slippage drag = (400.0 - 400.4) * 10 = -4.0, commission = -1.0
        expected = (400.0 - 400.4) * 10 - 1 * 1.0
        assert adj == pytest.approx(expected)


# ------------------------------------------------------------------
# Claude AI review integration tests
# ------------------------------------------------------------------


def _make_engine_with_claude(
    monkeypatch,
    claude_client=None,
    claude_enabled: bool = True,
    review_mode: str = "advisory",
    broker: MagicMock | None = None,
    risk_manager: MagicMock | None = None,
) -> tuple[ExecutionEngine, MagicMock, MagicMock, sessionmaker]:
    """Build an engine with an optional mock Claude client."""
    monkeypatch.setenv("ALPACA_PAPER_API_KEY", "fake")
    monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "fake")

    db_engine = create_engine("sqlite:///:memory:")
    init_db(db_engine)
    sf = sessionmaker(bind=db_engine)

    mock_broker = broker or MagicMock()
    mock_broker.get_account.return_value = _mock_account()
    mock_broker.get_positions.return_value = []
    mock_broker.get_orders.return_value = []

    if risk_manager is None:
        mock_risk = MagicMock()
        mock_risk.evaluate.return_value = (10, ValidationResult(approved=True))
    else:
        mock_risk = risk_manager

    from bread.core.config import load_config

    config = load_config()

    # Override claude settings
    config = config.model_copy(
        update={
            "claude": config.claude.model_copy(
                update={"enabled": claude_enabled, "review_mode": review_mode}
            )
        }
    )

    engine = ExecutionEngine(mock_broker, mock_risk, config, sf, claude_client=claude_client)
    return engine, mock_broker, mock_risk, sf


def _approved_review(**overrides: object) -> SignalReview:
    defaults: dict[str, object] = {
        "approved": True,
        "confidence": 0.85,
        "reasoning": "Looks good",
        "risk_flags": [],
    }
    defaults.update(overrides)
    return SignalReview(**defaults)  # type: ignore[arg-type]


def _rejected_review(**overrides: object) -> SignalReview:
    defaults: dict[str, object] = {
        "approved": False,
        "confidence": 0.3,
        "reasoning": "Too risky given portfolio concentration",
        "risk_flags": ["concentration"],
    }
    defaults.update(overrides)
    return SignalReview(**defaults)  # type: ignore[arg-type]


class TestClaudeReview:
    def test_no_claude_client_submits_normally(self, monkeypatch) -> None:
        """Engine with claude_client=None behaves identically to before."""
        engine, mock_broker, _, _ = _make_engine_with_claude(
            monkeypatch,
            claude_client=None,
        )
        mock_broker.submit_bracket_order.return_value = _bracket("order-1")

        engine.process_signals(
            [_make_signal("SPY")],
            {"SPY": 500.0},
        )
        mock_broker.submit_bracket_order.assert_called_once()

    def test_claude_disabled_skips_review(self, monkeypatch) -> None:
        """When claude.enabled=False, review is skipped even if client exists."""
        mock_claude = MagicMock()
        engine, mock_broker, _, _ = _make_engine_with_claude(
            monkeypatch,
            claude_client=mock_claude,
            claude_enabled=False,
        )
        mock_broker.submit_bracket_order.return_value = _bracket("order-1")

        engine.process_signals(
            [_make_signal("SPY")],
            {"SPY": 500.0},
        )
        mock_claude.review_signals_batch.assert_not_called()
        mock_broker.submit_bracket_order.assert_called_once()

    def test_advisory_mode_submits_all(self, monkeypatch) -> None:
        """Advisory mode logs review but submits all signals regardless."""
        mock_claude = MagicMock()
        mock_claude.review_signals_batch.return_value = [_rejected_review()]

        engine, mock_broker, _, _ = _make_engine_with_claude(
            monkeypatch,
            claude_client=mock_claude,
            review_mode="advisory",
        )
        mock_broker.submit_bracket_order.return_value = _bracket("order-1")

        engine.process_signals(
            [_make_signal("SPY")],
            {"SPY": 500.0},
        )
        # Even though Claude rejected, advisory mode still submits
        mock_broker.submit_bracket_order.assert_called_once()

    def test_gating_mode_rejects_blocked_signals(self, monkeypatch) -> None:
        """Gating mode blocks Claude-rejected signals from submission."""
        mock_claude = MagicMock()
        mock_claude.review_signals_batch.return_value = [_rejected_review()]

        engine, mock_broker, _, sf = _make_engine_with_claude(
            monkeypatch,
            claude_client=mock_claude,
            review_mode="gating",
        )

        engine.process_signals(
            [_make_signal("SPY")],
            {"SPY": 500.0},
        )
        mock_broker.submit_bracket_order.assert_not_called()

        # Should log a rejection order
        with sf() as session:
            orders = session.execute(select(OrderLog)).scalars().all()
            rejected = [o for o in orders if o.status == "REJECTED"]
            assert len(rejected) == 1
            assert "claude_rejected" in rejected[0].reason

    def test_gating_mode_approves_pass_through(self, monkeypatch) -> None:
        """Gating mode allows Claude-approved signals through."""
        mock_claude = MagicMock()
        mock_claude.review_signals_batch.return_value = [_approved_review()]

        engine, mock_broker, _, _ = _make_engine_with_claude(
            monkeypatch,
            claude_client=mock_claude,
            review_mode="gating",
        )
        mock_broker.submit_bracket_order.return_value = _bracket("order-1")

        engine.process_signals(
            [_make_signal("SPY")],
            {"SPY": 500.0},
        )
        mock_broker.submit_bracket_order.assert_called_once()

    def test_claude_error_falls_through(self, monkeypatch) -> None:
        """When Claude batch review raises, all signals proceed (fail-open)."""
        mock_claude = MagicMock()
        mock_claude.review_signals_batch.side_effect = RuntimeError("boom")

        engine, mock_broker, _, _ = _make_engine_with_claude(
            monkeypatch,
            claude_client=mock_claude,
            review_mode="gating",
        )
        mock_broker.submit_bracket_order.return_value = _bracket("order-1")

        engine.process_signals(
            [_make_signal("SPY")],
            {"SPY": 500.0},
        )
        # Fail-open: order submitted despite Claude error
        mock_broker.submit_bracket_order.assert_called_once()

    def test_batch_receives_only_risk_approved(self, monkeypatch) -> None:
        """Claude batch review receives only the signals that passed risk."""
        mock_claude = MagicMock()
        mock_claude.review_signals_batch.return_value = [_approved_review()]

        mock_risk = MagicMock()
        call_count = 0

        def risk_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (10, ValidationResult(approved=True))
            return (0, ValidationResult(approved=False, rejections=["max positions"]))

        mock_risk.evaluate.side_effect = risk_side_effect

        engine, mock_broker, _, _ = _make_engine_with_claude(
            monkeypatch,
            claude_client=mock_claude,
            risk_manager=mock_risk,
        )
        mock_broker.submit_bracket_order.return_value = _bracket("order-1")

        # SPY has higher strength so it sorts first (processed first by risk)
        engine.process_signals(
            [_make_signal("SPY", strength=0.8), _make_signal("QQQ", strength=0.5)],
            {"SPY": 500.0, "QQQ": 400.0},
        )
        # Claude should only receive SPY (QQQ rejected by risk)
        signals_passed = mock_claude.review_signals_batch.call_args[0][0]
        assert len(signals_passed) == 1
        assert signals_passed[0].symbol == "SPY"

    def test_get_last_review_returns_stored(self, monkeypatch) -> None:
        """After processing, get_last_review returns the review for that symbol."""
        mock_claude = MagicMock()
        review = _approved_review(reasoning="strong momentum")
        mock_claude.review_signals_batch.return_value = [review]

        engine, mock_broker, _, _ = _make_engine_with_claude(
            monkeypatch,
            claude_client=mock_claude,
        )
        mock_broker.submit_bracket_order.return_value = _bracket("order-1")

        engine.process_signals(
            [_make_signal("SPY")],
            {"SPY": 500.0},
        )
        result = engine.get_last_review("SPY")
        assert result is not None
        assert result.reasoning == "strong momentum"
        assert engine.get_last_review("QQQ") is None

    def test_reviews_cleared_between_calls(self, monkeypatch) -> None:
        """Reviews from previous process_signals don't leak."""
        mock_claude = MagicMock()
        mock_claude.review_signals_batch.return_value = [_approved_review()]

        engine, mock_broker, _, _ = _make_engine_with_claude(
            monkeypatch,
            claude_client=mock_claude,
        )
        mock_broker.submit_bracket_order.return_value = _bracket("order-1")

        engine.process_signals(
            [_make_signal("SPY")],
            {"SPY": 500.0},
        )
        assert engine.get_last_review("SPY") is not None

        # Second call with no BUY signals — reviews should be cleared
        engine.process_signals([], {})
        assert engine.get_last_review("SPY") is None

    def test_gating_mixed_approvals(self, monkeypatch) -> None:
        """Gating mode: approved signals submitted, rejected skipped."""
        mock_claude = MagicMock()
        # SPY approved, QQQ rejected (order matches strength sort)
        mock_claude.review_signals_batch.return_value = [
            _approved_review(),
            _rejected_review(),
        ]

        engine, mock_broker, _, _ = _make_engine_with_claude(
            monkeypatch,
            claude_client=mock_claude,
            review_mode="gating",
        )
        mock_broker.submit_bracket_order.return_value = _bracket("order-1")

        # SPY higher strength → sorted first
        engine.process_signals(
            [_make_signal("SPY", strength=0.8), _make_signal("QQQ", strength=0.5)],
            {"SPY": 500.0, "QQQ": 400.0},
        )
        # Only SPY should be submitted (QQQ rejected by Claude)
        assert mock_broker.submit_bracket_order.call_count == 1
        call_args = mock_broker.submit_bracket_order.call_args[0]
        assert call_args[0] == "SPY"


class TestStaleOrderTimeout:
    def test_stale_pending_order_cancelled(self, monkeypatch) -> None:
        engine, mock_broker, _, sf = _make_engine(monkeypatch)
        stale_time = datetime.now(UTC) - timedelta(minutes=60)
        with sf() as session:
            session.add(
                OrderLog(
                    broker_order_id="stale-1",
                    symbol="SPY",
                    side="BUY",
                    qty=10,
                    status="PENDING",
                    strategy_name="test",
                    reason="test",
                    created_at_utc=stale_time,
                )
            )
            session.commit()

        # Broker returns no orders (so reconcile_orders status update is a no-op)
        mock_broker.get_orders.return_value = []
        engine._reconcile_orders()

        with sf() as session:
            order = session.execute(select(OrderLog)).scalars().first()
            assert order is not None
            assert order.status == "CANCELLED"
        # Must cancel the specific order, NOT all orders for the symbol —
        # otherwise other strategies' brackets on SPY would get nuked too.
        mock_broker.cancel_order.assert_called_once_with("stale-1")
        mock_broker.cancel_orders_for_symbol.assert_not_called()

    def test_fresh_order_not_cancelled(self, monkeypatch) -> None:
        engine, mock_broker, _, sf = _make_engine(monkeypatch)
        fresh_time = datetime.now(UTC) - timedelta(minutes=5)
        with sf() as session:
            session.add(
                OrderLog(
                    broker_order_id="fresh-1",
                    symbol="SPY",
                    side="BUY",
                    qty=10,
                    status="PENDING",
                    strategy_name="test",
                    reason="test",
                    created_at_utc=fresh_time,
                )
            )
            session.commit()

        mock_broker.get_orders.return_value = []
        engine._reconcile_orders()

        with sf() as session:
            order = session.execute(select(OrderLog)).scalars().first()
            assert order is not None
            assert order.status == "PENDING"
        mock_broker.cancel_order.assert_not_called()

    def test_stale_accepted_also_cancelled(self, monkeypatch) -> None:
        engine, mock_broker, _, sf = _make_engine(monkeypatch)
        stale_time = datetime.now(UTC) - timedelta(minutes=60)
        with sf() as session:
            session.add(
                OrderLog(
                    broker_order_id="stale-2",
                    symbol="QQQ",
                    side="BUY",
                    qty=5,
                    status="ACCEPTED",
                    strategy_name="test",
                    reason="test",
                    created_at_utc=stale_time,
                )
            )
            session.commit()

        mock_broker.get_orders.return_value = []
        engine._reconcile_orders()

        with sf() as session:
            order = session.execute(select(OrderLog)).scalars().first()
            assert order is not None
            assert order.status == "CANCELLED"

    def test_stale_order_without_broker_id(self, monkeypatch) -> None:
        """Order without broker_order_id still marked CANCELLED but no broker cancel."""
        engine, mock_broker, _, sf = _make_engine(monkeypatch)
        stale_time = datetime.now(UTC) - timedelta(minutes=60)
        with sf() as session:
            session.add(
                OrderLog(
                    broker_order_id=None,
                    symbol="SPY",
                    side="BUY",
                    qty=10,
                    status="PENDING",
                    strategy_name="test",
                    reason="test",
                    created_at_utc=stale_time,
                )
            )
            session.commit()

        mock_broker.get_orders.return_value = []
        engine._reconcile_orders()

        with sf() as session:
            order = session.execute(select(OrderLog)).scalars().first()
            assert order is not None
            assert order.status == "CANCELLED"
        mock_broker.cancel_order.assert_not_called()


class TestStrategyIsolation:
    """Tests that defend the per-strategy isolation invariant.

    See memory/project_multi_strategy_positions.md: each strategy is an
    independent portfolio. Two strategies CAN hold the same symbol; a SELL
    from one strategy must not touch another strategy's shares.
    """

    def test_two_strategies_open_same_symbol_in_one_tick(self, monkeypatch) -> None:
        engine, mock_broker, mock_risk, _ = _make_engine(monkeypatch)
        mock_broker.submit_bracket_order.side_effect = [
            _bracket("order-a", stop="sl-a", tp="tp-a"),
            _bracket("order-b", stop="sl-b", tp="tp-b"),
        ]
        mock_risk.evaluate.return_value = (5, ValidationResult(approved=True))

        signals = [
            _make_signal("SPY", strategy_name="strategy_a"),
            _make_signal("SPY", strategy_name="strategy_b"),
        ]
        engine.process_signals(signals, {"SPY": 500.0})

        assert len(engine._positions) == 2
        owners = sorted(p.strategy_name for p in engine._positions if p.symbol == "SPY")
        assert owners == ["strategy_a", "strategy_b"]
        # Each position owns its own bracket legs — critical for SELL isolation
        by_strat = {p.strategy_name: p for p in engine._positions}
        assert by_strat["strategy_a"].stop_loss_order_id == "sl-a"
        assert by_strat["strategy_b"].stop_loss_order_id == "sl-b"

    def test_sell_cancels_only_this_strategys_legs(self, monkeypatch) -> None:
        engine, mock_broker, _, _ = _make_engine(monkeypatch)
        mock_broker.submit_market_sell.return_value = "sell-b"
        a = Position(
            symbol="SPY", qty=10, entry_price=500.0,
            stop_loss_price=475.0, take_profit_price=525.0,
            broker_order_id="order-a", strategy_name="strategy_a",
            entry_date=date.today(),
            stop_loss_order_id="sl-a", take_profit_order_id="tp-a",
        )
        b = Position(
            symbol="SPY", qty=7, entry_price=500.0,
            stop_loss_price=475.0, take_profit_price=525.0,
            broker_order_id="order-b", strategy_name="strategy_b",
            entry_date=date.today(),
            stop_loss_order_id="sl-b", take_profit_order_id="tp-b",
        )
        engine._positions.extend([a, b])

        engine.process_signals(
            [_make_signal("SPY", SignalDirection.SELL, strategy_name="strategy_b")],
            {"SPY": 510.0},
        )

        # Only strategy_b's legs were cancelled; close_position not called (broker-wide)
        cancelled = {c.args[0] for c in mock_broker.cancel_order.call_args_list}
        assert cancelled == {"sl-b", "tp-b"}
        mock_broker.close_position.assert_not_called()
        mock_broker.submit_market_sell.assert_called_once_with("SPY", 7)
        # Strategy_a's position survives intact
        survivors = [p for p in engine._positions if p.symbol == "SPY"]
        assert len(survivors) == 1
        assert survivors[0].strategy_name == "strategy_a"
        assert survivors[0].stop_loss_order_id == "sl-a"

    def test_per_strategy_max_positions_enforced(self, monkeypatch) -> None:
        """A strategy hitting its per-strategy cap is blocked; other strategies unaffected."""
        from bread.risk.manager import RiskManager

        engine, mock_broker, _, _ = _make_engine(
            monkeypatch,
            risk_manager=RiskManager(
                # Tight per-strategy cap, generous global cap
                engine_config := _risk_settings(max_positions_per_strategy=2, max_positions=10),
            ),
        )
        _ = engine_config  # silence lint — set before engine construction
        mock_broker.submit_bracket_order.return_value = _bracket("parent")

        # strategy_a already holds 2 positions — next BUY must be rejected
        for sym in ("AAA", "BBB"):
            engine._positions.append(
                Position(
                    symbol=sym, qty=1, entry_price=100.0,
                    stop_loss_price=95.0, take_profit_price=110.0,
                    broker_order_id=f"o-{sym}", strategy_name="strategy_a",
                    entry_date=date.today(),
                )
            )

        signals = [
            _make_signal("CCC", strategy_name="strategy_a"),   # blocked
            _make_signal("CCC", strategy_name="strategy_b"),   # allowed
        ]
        engine.process_signals(signals, {"CCC": 50.0})

        # Only strategy_b's BUY made it to the broker
        assert mock_broker.submit_bracket_order.call_count == 1
        held = {p.strategy_name for p in engine._positions if p.symbol == "CCC"}
        assert held == {"strategy_b"}

    def test_global_max_positions_enforced_across_strategies(self, monkeypatch) -> None:
        from bread.risk.manager import RiskManager

        engine, mock_broker, _, _ = _make_engine(
            monkeypatch,
            risk_manager=RiskManager(
                _risk_settings(max_positions=3, max_positions_per_strategy=5),
            ),
        )
        mock_broker.submit_bracket_order.return_value = _bracket("parent")

        # 3 positions spread across two strategies fills the global cap
        for i, (sym, strat) in enumerate(
            [("AAA", "s1"), ("BBB", "s1"), ("CCC", "s2")]
        ):
            engine._positions.append(
                Position(
                    symbol=sym, qty=1, entry_price=100.0,
                    stop_loss_price=95.0, take_profit_price=110.0,
                    broker_order_id=f"o-{i}", strategy_name=strat,
                    entry_date=date.today(),
                )
            )

        # Neither strategy can open a fourth — global cap dominates
        signals = [
            _make_signal("DDD", strategy_name="s1"),
            _make_signal("DDD", strategy_name="s2"),
        ]
        engine.process_signals(signals, {"DDD": 50.0})
        mock_broker.submit_bracket_order.assert_not_called()

    def test_reconcile_removes_only_bracket_completed_position(self, monkeypatch) -> None:
        """If strategy_a's TP leg filled, only strategy_a's position is removed."""
        engine, mock_broker, _, _ = _make_engine(monkeypatch)

        a = Position(
            symbol="SPY", qty=10, entry_price=500.0,
            stop_loss_price=475.0, take_profit_price=525.0,
            broker_order_id="order-a", strategy_name="strategy_a",
            entry_date=date.today(),
            stop_loss_order_id="sl-a", take_profit_order_id="tp-a",
        )
        b = Position(
            symbol="SPY", qty=7, entry_price=500.0,
            stop_loss_price=475.0, take_profit_price=525.0,
            broker_order_id="order-b", strategy_name="strategy_b",
            entry_date=date.today(),
            stop_loss_order_id="sl-b", take_profit_order_id="tp-b",
        )
        engine._positions.extend([a, b])

        def _get_order(order_id: str):
            if order_id == "tp-a":
                return _broker_order(id="tp-a", status=OrderStatus.FILLED)
            return _broker_order(id=order_id, status=OrderStatus.ACCEPTED)

        mock_broker.get_order_by_id.side_effect = _get_order
        # Broker still reports qty=7 on SPY (only strategy_b's shares remain)
        mock_broker.get_positions.return_value = [
            _broker_position("SPY", qty=7.0, avg_entry_price=500.0),
        ]

        engine.reconcile()

        # strategy_a removed, strategy_b survives
        survivors = [p for p in engine._positions if p.symbol == "SPY"]
        assert len(survivors) == 1
        assert survivors[0].strategy_name == "strategy_b"


def _risk_settings(**overrides):
    """Build a RiskSettings with defaults + overrides for isolation tests."""
    from bread.core.config import RiskSettings

    return RiskSettings(**overrides)
