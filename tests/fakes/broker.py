"""In-memory Broker double for unit tests.

Replaces patches on `AlpacaBroker`. Holds the broker's view of account,
positions, and orders. Records every submitted order so tests can assert on
the call sequence without inspecting log output or DB rows.

Typical use:

    broker = FakeBroker(account=Account(equity=10_000, ...))
    broker.set_position("SPY", qty=10, avg_entry_price=400.0, current_price=410.0)
    engine = ExecutionEngine(broker, ...)
    engine.process_signals([signal], prices={"SPY": 410.0})
    assert ("SPY", 5, 400.0, 420.0) in broker.submitted_brackets
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from bread.core.models import OrderSide, OrderStatus
from bread.execution.broker import Broker
from bread.execution.models import Account, BrokerOrder, BrokerPosition


def _default_account() -> Account:
    return Account(
        equity=100_000.0,
        buying_power=100_000.0,
        cash=100_000.0,
        last_equity=100_000.0,
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )


@dataclass
class FakeBroker(Broker):
    """In-memory broker. All state is mutable from tests."""

    account: Account = field(default_factory=_default_account)
    positions: dict[str, BrokerPosition] = field(default_factory=dict)
    open_orders: dict[str, BrokerOrder] = field(default_factory=dict)
    closed_orders: dict[str, BrokerOrder] = field(default_factory=dict)

    # Recorded calls — tests assert on these.
    submitted_brackets: list[tuple[str, int, float, float]] = field(default_factory=list)
    closed_symbols: list[str] = field(default_factory=list)
    cancelled_symbols: list[str] = field(default_factory=list)
    cancel_all_calls: int = 0
    close_all_calls: int = 0

    # ------------------------------------------------------------------
    # Test setup helpers
    # ------------------------------------------------------------------

    def set_position(
        self,
        symbol: str,
        *,
        qty: float,
        avg_entry_price: float,
        current_price: float | None = None,
        unrealized_pl: float = 0.0,
        unrealized_plpc: float = 0.0,
        market_value: float | None = None,
    ) -> None:
        cp = current_price if current_price is not None else avg_entry_price
        mv = market_value if market_value is not None else cp * qty
        self.positions[symbol] = BrokerPosition(
            symbol=symbol,
            qty=qty,
            avg_entry_price=avg_entry_price,
            current_price=cp,
            market_value=mv,
            unrealized_pl=unrealized_pl,
            unrealized_plpc=unrealized_plpc,
        )

    def add_order(self, order: BrokerOrder, *, closed: bool = False) -> None:
        bucket = self.closed_orders if closed else self.open_orders
        bucket[order.id] = order

    # ------------------------------------------------------------------
    # Broker interface
    # ------------------------------------------------------------------

    def get_account(self) -> Account:
        return self.account

    def get_positions(self) -> list[BrokerPosition]:
        return list(self.positions.values())

    def get_orders(self, status: str = "open") -> list[BrokerOrder]:
        if status == "open":
            return list(self.open_orders.values())
        if status == "closed":
            return list(self.closed_orders.values())
        return [*self.open_orders.values(), *self.closed_orders.values()]

    def list_historical_orders(
        self,
        *,
        after: datetime,
        until: datetime | None = None,
        status: str = "closed",
        page_size: int = 500,
    ) -> list[BrokerOrder]:
        pool = self.closed_orders if status == "closed" else self.open_orders
        out: list[BrokerOrder] = []
        for o in pool.values():
            ts = o.submitted_at or o.created_at
            if ts is None:
                continue
            if ts < after:
                continue
            if until is not None and ts >= until:
                continue
            out.append(o)
        return out

    def get_order_by_id(self, order_id: str) -> BrokerOrder | None:
        return self.open_orders.get(order_id) or self.closed_orders.get(order_id)

    def get_account_created_at(self) -> datetime:
        return self.account.created_at

    def submit_bracket_order(
        self,
        symbol: str,
        qty: int,
        stop_loss_price: float,
        take_profit_price: float,
    ) -> str:
        order_id = f"fake-{uuid.uuid4().hex[:8]}"
        self.submitted_brackets.append((symbol, qty, stop_loss_price, take_profit_price))
        self.open_orders[order_id] = BrokerOrder(
            id=order_id,
            symbol=symbol,
            side=OrderSide.BUY,
            status=OrderStatus.PENDING,
            qty=float(qty),
            filled_qty=0.0,
            filled_avg_price=None,
            submitted_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
            filled_at=None,
            order_type="market",
        )
        return order_id

    def cancel_orders_for_symbol(self, symbol: str) -> int:
        self.cancelled_symbols.append(symbol)
        ids = [oid for oid, o in self.open_orders.items() if o.symbol == symbol]
        for oid in ids:
            del self.open_orders[oid]
        return len(ids)

    def cancel_all_orders(self) -> int:
        self.cancel_all_calls += 1
        n = len(self.open_orders)
        self.open_orders.clear()
        return n

    def close_all_positions(self) -> int:
        self.close_all_calls += 1
        n = len(self.positions)
        self.positions.clear()
        return n

    def close_position(self, symbol: str) -> str | None:
        if symbol not in self.positions:
            return None
        self.closed_symbols.append(symbol)
        del self.positions[symbol]
        order_id = f"fake-close-{uuid.uuid4().hex[:8]}"
        self.open_orders[order_id] = BrokerOrder(
            id=order_id,
            symbol=symbol,
            side=OrderSide.SELL,
            status=OrderStatus.PENDING,
            qty=0.0,
            filled_qty=0.0,
            filled_avg_price=None,
            submitted_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
            filled_at=None,
            order_type="market",
        )
        return order_id
