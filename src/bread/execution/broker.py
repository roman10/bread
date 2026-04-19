"""Broker abstract base class.

All concrete brokers (AlpacaBroker, future IBKR/Tradier/FakeBroker) implement
this interface and return only normalized DTOs from `bread.execution.models`.
Consumers (ExecutionEngine, CLI commands, dashboard) must depend on Broker —
never on concrete broker types or third-party SDK objects.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from bread.execution.models import Account, BracketOrderIds, BrokerOrder, BrokerPosition


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
    ) -> BracketOrderIds:
        """Submit a bracket order. Returns the parent + OCO leg IDs."""

    @abstractmethod
    def submit_market_sell(self, symbol: str, qty: int) -> str:
        """Submit a plain market SELL for exactly *qty* shares. Returns order ID.

        Used when the engine closes one strategy's position in a symbol
        without liquidating another strategy's shares on the same symbol.
        Caller is responsible for cancelling any open OCO legs first.
        """

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Cancel a single order by broker ID. Returns True on success.

        Tolerates "already cancelled"/"already filled" errors as success=False
        without raising — this is a best-effort call on OCO cleanup paths.
        """

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
        """Close a position by symbol. Returns order ID or None if no position.

        Liquidates ALL broker-side shares for the symbol — do not use from the
        per-strategy SELL path when multiple strategies may hold the symbol.
        Reserved for reset flows, unknown-owner positions, and CLI tools.
        """
