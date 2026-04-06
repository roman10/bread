"""Alpaca broker wrapper for order management."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError, Timeout
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential
from urllib3.exceptions import ProtocolError

from bread.core.exceptions import ExecutionError, OrderError

if TYPE_CHECKING:
    from alpaca.trading.models import Order as AlpacaOrder
    from alpaca.trading.models import Position as AlpacaPosition
    from alpaca.trading.models import TradeAccount

    from bread.core.config import AppConfig

logger = logging.getLogger(__name__)

_read_retrier = Retrying(
    retry=retry_if_exception_type((ConnectionError, Timeout, ProtocolError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)


class AlpacaBroker:
    """Wraps alpaca-py TradingClient. Paper/live controlled by config.mode."""

    def __init__(self, config: AppConfig) -> None:
        if config.mode == "paper":
            api_key = config.alpaca.paper_api_key
            secret_key = config.alpaca.paper_secret_key
        else:
            api_key = config.alpaca.live_api_key
            secret_key = config.alpaca.live_secret_key

        if not api_key or not secret_key:
            raise ExecutionError(f"Missing API credentials for {config.mode} mode")

        self._client = TradingClient(
            api_key=api_key,
            secret_key=secret_key,
            paper=(config.mode == "paper"),
        )
        adapter = HTTPAdapter(pool_maxsize=20)
        self._client._session.mount("https://", adapter)

    def get_account(self) -> TradeAccount:
        """Fetch account info (equity, buying_power, cash, last_equity)."""
        try:
            return _read_retrier(self._client.get_account)  # type: ignore[return-value]
        except Exception as exc:
            raise ExecutionError(f"Failed to get account: {exc}") from exc

    def get_positions(self) -> list[AlpacaPosition]:
        """Fetch all open positions from Alpaca."""
        try:
            return _read_retrier(self._client.get_all_positions)  # type: ignore[arg-type]
        except Exception as exc:
            raise ExecutionError(f"Failed to get positions: {exc}") from exc

    def get_orders(self, status: str = "open") -> list[AlpacaOrder]:
        """Fetch orders by status."""
        try:
            request = GetOrdersRequest(status=QueryOrderStatus(status))
            return _read_retrier(
                self._client.get_orders, filter=request  # type: ignore[arg-type]
            )
        except Exception as exc:
            raise ExecutionError(f"Failed to get orders: {exc}") from exc

    def submit_bracket_order(
        self,
        symbol: str,
        qty: int,
        stop_loss_price: float,
        take_profit_price: float,
    ) -> str:
        """Submit a bracket order (market buy + OCO stop-loss/take-profit).

        Returns the Alpaca parent order ID.
        """
        logger.info(
            "Submitting bracket order: %s qty=%d stop=%.2f tp=%.2f",
            symbol, qty, stop_loss_price, take_profit_price,
        )
        try:
            request = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                order_class=OrderClass.BRACKET,
                take_profit={"limit_price": round(take_profit_price, 2)},
                stop_loss={"stop_price": round(stop_loss_price, 2)},
            )
            order = self._client.submit_order(request)
            order_id = str(order.id)  # type: ignore[union-attr]
            logger.info("Bracket order submitted: %s order_id=%s", symbol, order_id)
            return order_id
        except Exception as exc:
            raise OrderError(f"Failed to submit bracket order for {symbol}: {exc}") from exc

    def cancel_orders_for_symbol(self, symbol: str) -> int:
        """Cancel all open orders for a symbol. Returns count cancelled.

        Never raises — failures are logged so callers can proceed.
        """
        try:
            open_orders = self.get_orders(status="open")
        except Exception:
            logger.warning("Failed to fetch open orders for cancellation of %s", symbol)
            return 0
        cancelled = 0
        for order in open_orders:
            if order.symbol == symbol:
                try:
                    self._client.cancel_order_by_id(order.id)
                    logger.info("Cancelled order %s for %s", order.id, symbol)
                    cancelled += 1
                except Exception:
                    logger.warning("Failed to cancel order %s for %s", order.id, symbol)
        return cancelled

    def close_position(self, symbol: str) -> str | None:
        """Close a position by symbol. Returns order ID or None if no position.

        Cancels any open orders (e.g. bracket OCO legs) for the symbol first,
        since they hold shares and block the close.
        """
        logger.info("Closing position: %s", symbol)
        self.cancel_orders_for_symbol(symbol)
        try:
            order = self._client.close_position(symbol_or_asset_id=symbol)
            order_id = str(order.id)  # type: ignore[union-attr]
            logger.info("Close order submitted: %s order_id=%s", symbol, order_id)
            return order_id
        except Exception as exc:
            exc_str = str(exc).lower()
            if "not found" in exc_str or "no position" in exc_str:
                logger.warning("No position to close for %s", symbol)
                return None
            raise OrderError(f"Failed to close position for {symbol}: {exc}") from exc
