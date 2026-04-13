"""Test factory functions for bread domain objects.

Each factory accepts keyword overrides so tests only set what they care about:

    from tests.factories import make_signal, make_order_log

    signal = make_signal(symbol="QQQ", strength=0.9)
    order  = make_order_log(symbol="QQQ", status="PENDING")
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from bread.core.models import Position, Signal, SignalDirection
from bread.db.models import OrderLog, PortfolioSnapshot


def make_signal(
    symbol: str = "SPY",
    direction: SignalDirection = SignalDirection.BUY,
    strength: float = 0.7,
    stop_loss_pct: float = 0.05,
    strategy_name: str = "test_strategy",
    reason: str = "test signal",
    timestamp: datetime | None = None,
) -> Signal:
    return Signal(
        symbol=symbol,
        direction=direction,
        strength=strength,
        stop_loss_pct=stop_loss_pct,
        strategy_name=strategy_name,
        reason=reason,
        timestamp=timestamp or datetime.now(UTC),
    )


def make_position(
    symbol: str = "SPY",
    qty: int = 10,
    entry_price: float = 500.0,
    stop_loss_price: float = 475.0,
    take_profit_price: float = 540.0,
    broker_order_id: str = "test-order-001",
    strategy_name: str = "test_strategy",
    entry_date: date | None = None,
) -> Position:
    return Position(
        symbol=symbol,
        qty=qty,
        entry_price=entry_price,
        stop_loss_price=stop_loss_price,
        take_profit_price=take_profit_price,
        broker_order_id=broker_order_id,
        strategy_name=strategy_name,
        entry_date=entry_date or date.today(),
    )


def make_order_log(
    symbol: str = "SPY",
    side: str = "BUY",
    qty: int = 10,
    status: str = "FILLED",
    broker_order_id: str | None = "test-broker-001",
    strategy_name: str = "test_strategy",
    reason: str = "test",
    filled_price: float | None = 500.0,
    raw_filled_price: float | None = 500.0,
    stop_loss_price: float | None = 475.0,
    take_profit_price: float | None = 540.0,
    created_at_utc: datetime | None = None,
    filled_at_utc: datetime | None = None,
) -> OrderLog:
    now = datetime.now(UTC)
    return OrderLog(
        symbol=symbol,
        side=side,
        qty=qty,
        status=status,
        broker_order_id=broker_order_id,
        strategy_name=strategy_name,
        reason=reason,
        filled_price=filled_price,
        raw_filled_price=raw_filled_price,
        stop_loss_price=stop_loss_price,
        take_profit_price=take_profit_price,
        created_at_utc=created_at_utc or now,
        filled_at_utc=filled_at_utc or now,
    )


def make_snapshot(
    equity: float = 10_000.0,
    cash: float | None = None,
    open_positions: int = 0,
    daily_pnl: float = 0.0,
    timestamp_utc: datetime | None = None,
) -> PortfolioSnapshot:
    if cash is None:
        cash = equity
    return PortfolioSnapshot(
        timestamp_utc=timestamp_utc or datetime.now(UTC),
        equity=equity,
        cash=cash,
        positions_value=equity - cash,
        open_positions=open_positions,
        daily_pnl=daily_pnl,
    )
