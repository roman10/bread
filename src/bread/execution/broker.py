"""Broker abstract base class.

All concrete brokers (AlpacaBroker, future IBKR/Tradier/FakeBroker) implement
this interface and return only normalized DTOs from `bread.execution.models`.
Consumers (ExecutionEngine, CLI commands, dashboard) must depend on Broker —
never on concrete broker types or third-party SDK objects.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from bread.execution.models import Account, BrokerOrder, BrokerPosition


class Broker(ABC):
    """Order management and account interface."""

    @abstractmethod
    def get_account(self) -> Account:
        """Fetch account snapshot (equity, buying_power, cash, last_equity)."""

    @abstractmethod
    def get_positions(self) -> list[BrokerPosition]:
        """Fetch all open broker positions."""

    @abstractmethod
    def get_orders(self, status: str = "open") -> list[BrokerOrder]:
        """Fetch orders by status. Status values: 'open', 'closed', 'all'."""

    @abstractmethod
    def list_historical_orders(
        self,
        *,
        after: datetime,
        until: datetime | None = None,
        status: str = "closed",
        page_size: int = 500,
    ) -> list[BrokerOrder]:
        """Return orders in [after, until), deduped, paginated newest-first."""

    @abstractmethod
    def get_order_by_id(self, order_id: str) -> BrokerOrder | None:
        """Fetch a single order by ID. Returns None if not found."""

    @abstractmethod
    def get_account_created_at(self) -> datetime:
        """Return the account creation timestamp (UTC)."""

    @abstractmethod
    def submit_bracket_order(
        self,
        symbol: str,
        qty: int,
        stop_loss_price: float,
        take_profit_price: float,
    ) -> str:
        """Submit a bracket order. Returns the parent order ID."""

    @abstractmethod
    def cancel_orders_for_symbol(self, symbol: str) -> int:
        """Cancel all open orders for a symbol. Returns count cancelled."""

    @abstractmethod
    def cancel_all_orders(self) -> int:
        """Cancel every open order. Returns count attempted."""

    @abstractmethod
    def close_all_positions(self) -> int:
        """Liquidate every open position. Returns count attempted."""

    @abstractmethod
    def close_position(self, symbol: str) -> str | None:
        """Close a position by symbol. Returns order ID or None if no position."""
