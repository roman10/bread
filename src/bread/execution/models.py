"""Normalized broker DTOs returned by the Broker abstraction.

Distinct from `bread.core.models.Position`, which is bread's internal
per-strategy position record. `BrokerPosition` is the broker's point-in-time
view of an aggregate symbol holding.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from bread.core.models import OrderSide, OrderStatus


@dataclass(frozen=True)
class Account:
    equity: float
    buying_power: float
    cash: float
    last_equity: float
    # Only the backfill flow needs this; making it Optional keeps every other
    # broker call from blowing up if the field is missing. Use
    # `Broker.get_account_created_at()` when presence is required.
    created_at: datetime | None
    account_number: str | None = None


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    qty: float  # broker reality is fractional; consumers cast to int when needed
    avg_entry_price: float
    current_price: float
    market_value: float
    unrealized_pl: float
    unrealized_plpc: float  # decimal fraction (0.05 == 5%)


@dataclass(frozen=True)
class BracketOrderIds:
    """IDs returned when a bracket order is submitted.

    Parent is the market BUY. Stop-loss / take-profit are the OCO child legs
    that become independent orders once the parent fills. The engine tracks
    these on the Position so a per-strategy SELL can cancel its specific
    children without liquidating another strategy's shares on the same symbol.
    """

    parent_order_id: str
    stop_loss_order_id: str
    take_profit_order_id: str


@dataclass(frozen=True)
class BrokerOrder:
    id: str
    symbol: str
    # side/status are None when the broker returns a value we can't normalize.
    # Engine reconciliation skips updates with status=None to avoid overwriting
    # known status with a sentinel.
    side: OrderSide | None
    status: OrderStatus | None
    qty: float  # broker may report fractional; whole-share consumers must cast
    filled_qty: float
    filled_avg_price: float | None
    submitted_at: datetime | None
    created_at: datetime | None
    filled_at: datetime | None
    order_type: str | None  # "market", "limit", "stop", ... — display only
