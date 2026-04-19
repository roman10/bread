"""Alpaca broker wrapper for order management."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from alpaca.common.enums import Sort
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, QueryOrderStatus, TimeInForce
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError, Timeout
from tenacity import (
    Retrying,
    before_sleep_log,
    retry_if_exception,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from urllib3.exceptions import ProtocolError

from bread.core.exceptions import ExecutionError, OrderError
from bread.core.models import OrderSide, OrderStatus
from bread.execution.broker import Broker
from bread.execution.models import Account, BrokerOrder, BrokerPosition

if TYPE_CHECKING:
    from datetime import datetime

    from alpaca.trading.models import Order as AlpacaOrder
    from alpaca.trading.models import Position as AlpacaPosition
    from alpaca.trading.models import TradeAccount

    from bread.core.config import AppConfig

logger = logging.getLogger(__name__)


_ALPACA_STATUS_MAP: dict[str, str] = {
    "new": "PENDING",
    "pending_new": "PENDING",
    "accepted": "ACCEPTED",
    "accepted_for_bidding": "ACCEPTED",
    "pending_cancel": "ACCEPTED",
    "pending_replace": "ACCEPTED",
    "replaced": "ACCEPTED",
    "held": "ACCEPTED",
    "suspended": "ACCEPTED",
    "calculated": "ACCEPTED",
    # partial fills stay ACCEPTED: the current pipeline only submits full-qty
    # market brackets, so partials are unreachable today. Revisit if partial
    # fills become a real path.
    "partially_filled": "ACCEPTED",
    "filled": "FILLED",
    "canceled": "CANCELLED",
    "expired": "CANCELLED",
    "done_for_day": "CANCELLED",
    "stopped": "CANCELLED",
    "rejected": "REJECTED",
}


def normalize_alpaca_status(raw: object) -> str:
    """Map an Alpaca order status into our bread.core.models.OrderStatus value.

    Alpaca's `OrderStatus` is a `str, Enum` (not `StrEnum`), so `str(enum)`
    returns the class-prefixed name ("OrderStatus.FILLED") rather than the
    value. Use `.value` (or the raw token) to get the canonical string.
    Accepts enum instance, plain string, or None. Unknown values return
    "UNKNOWN" and log a warning — never raise.
    """
    if raw is None:
        return "UNKNOWN"
    token = str(getattr(raw, "value", raw)).lower()
    mapped = _ALPACA_STATUS_MAP.get(token)
    if mapped is None:
        logger.warning("Unrecognized Alpaca order status: %r", raw)
        return "UNKNOWN"
    return mapped


def normalize_alpaca_side(raw: object) -> str:
    """Map an Alpaca order side to 'BUY' or 'SELL' (or '' for unknown)."""
    if raw is None:
        return ""
    token = str(getattr(raw, "value", raw)).upper()
    if token in ("BUY", "SELL"):
        return token
    return ""


def _to_float(raw: object) -> float:
    if raw is None:
        return 0.0
    return float(raw)  # type: ignore[arg-type]


def _to_optional_float(raw: object) -> float | None:
    if raw is None:
        return None
    return float(raw)  # type: ignore[arg-type]


def _to_account(raw: TradeAccount) -> Account:
    return Account(
        equity=_to_float(getattr(raw, "equity", None)),
        buying_power=_to_float(getattr(raw, "buying_power", None)),
        cash=_to_float(getattr(raw, "cash", None)),
        last_equity=_to_float(getattr(raw, "last_equity", None)),
        created_at=getattr(raw, "created_at", None),
    )


def _to_broker_position(raw: AlpacaPosition) -> BrokerPosition:
    return BrokerPosition(
        symbol=str(raw.symbol),
        qty=_to_float(getattr(raw, "qty", None)),
        avg_entry_price=_to_float(getattr(raw, "avg_entry_price", None)),
        current_price=_to_float(getattr(raw, "current_price", None)),
        market_value=_to_float(getattr(raw, "market_value", None)),
        unrealized_pl=_to_float(getattr(raw, "unrealized_pl", None)),
        unrealized_plpc=_to_float(getattr(raw, "unrealized_plpc", None)),
    )


def _to_broker_order(raw: AlpacaOrder) -> BrokerOrder:
    side_token = normalize_alpaca_side(getattr(raw, "side", None))
    side: OrderSide | None = (
        OrderSide(side_token) if side_token in ("BUY", "SELL") else None
    )
    status_token = normalize_alpaca_status(getattr(raw, "status", None))
    status: OrderStatus | None
    try:
        status = OrderStatus(status_token)
    except ValueError:
        status = None
    qty_raw = getattr(raw, "qty", None)
    filled_qty_raw = getattr(raw, "filled_qty", None)
    qty = _to_float(qty_raw if qty_raw is not None else filled_qty_raw)
    filled_qty = _to_float(filled_qty_raw)
    order_type_raw = getattr(raw, "type", None)
    order_type = (
        str(getattr(order_type_raw, "value", order_type_raw)).lower()
        if order_type_raw is not None
        else None
    )
    return BrokerOrder(
        id=str(raw.id),
        symbol=str(getattr(raw, "symbol", "") or ""),
        side=side,
        status=status,
        qty=qty,
        filled_qty=filled_qty,
        filled_avg_price=_to_optional_float(getattr(raw, "filled_avg_price", None)),
        submitted_at=getattr(raw, "submitted_at", None),
        created_at=getattr(raw, "created_at", None),
        filled_at=getattr(raw, "filled_at", None),
        order_type=order_type,
    )


_read_retrier = Retrying(
    retry=retry_if_exception_type((ConnectionError, Timeout, ProtocolError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)


def _is_held_for_orders_error(exc: BaseException) -> bool:
    """True when Alpaca rejects because shares are still held by a pending cancel."""
    msg = str(exc).lower()
    return "insufficient qty available for order" in msg


_close_retrier = Retrying(
    retry=retry_if_exception(_is_held_for_orders_error),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
    before_sleep=before_sleep_log(logger, logging.WARNING),
)


class AlpacaBroker(Broker):
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

    def get_account(self) -> Account:
        try:
            raw = _read_retrier(self._client.get_account)
        except Exception as exc:
            raise ExecutionError(f"Failed to get account: {exc}") from exc
        return _to_account(raw)  # type: ignore[arg-type]

    def get_positions(self) -> list[BrokerPosition]:
        try:
            raw = _read_retrier(self._client.get_all_positions)
        except Exception as exc:
            raise ExecutionError(f"Failed to get positions: {exc}") from exc
        return [_to_broker_position(p) for p in raw]  # type: ignore[arg-type]

    def get_orders(self, status: str = "open") -> list[BrokerOrder]:
        try:
            request = GetOrdersRequest(status=QueryOrderStatus(status))
            raw = _read_retrier(self._client.get_orders, filter=request)
        except Exception as exc:
            raise ExecutionError(f"Failed to get orders: {exc}") from exc
        return [_to_broker_order(o) for o in raw]  # type: ignore[arg-type]

    def list_historical_orders(
        self,
        *,
        after: datetime,
        until: datetime | None = None,
        status: str = "closed",
        page_size: int = 500,
    ) -> list[BrokerOrder]:
        """Return orders in [after, until), deduped, paginated newest-first.

        Alpaca caps a single /v2/orders response at 500 rows. We page by
        walking the `until` cursor backward: each batch's oldest
        submitted_at becomes the next batch's `until`. Stop on empty or
        short batch. Cross-batch dedup by order id — the until-cursor
        boundary can repeat one row across the seam.
        """
        collected: dict[str, AlpacaOrder] = {}
        cursor: datetime | None = until
        while True:
            request = GetOrdersRequest(
                status=QueryOrderStatus(status),
                after=after,
                until=cursor,
                limit=page_size,
                direction=Sort.DESC,
                nested=False,
            )
            try:
                raw_batch = _read_retrier(
                    self._client.get_orders, filter=request
                )
            except Exception as exc:
                raise ExecutionError(f"Failed to list historical orders: {exc}") from exc

            # alpaca-py annotates get_orders as `list[Order] | RawData`. We
            # don't pass raw_data=True, so it's always a list here, but the
            # guard keeps us honest if alpaca-py ever changes defaults.
            if not isinstance(raw_batch, list):
                raise ExecutionError(
                    f"Unexpected Alpaca response shape: {type(raw_batch).__name__}"
                )
            batch: list[AlpacaOrder] = raw_batch

            if not batch:
                break

            new_rows = 0
            oldest: datetime | None = None
            for o in batch:
                key = str(o.id)
                if key not in collected:
                    collected[key] = o
                    new_rows += 1
                submitted = o.submitted_at
                if submitted is not None and (oldest is None or submitted < oldest):
                    oldest = submitted

            # Short batch => Alpaca has nothing older in-range; we're done.
            # No new rows => the cursor advance didn't yield progress; bail.
            if len(batch) < page_size or new_rows == 0 or oldest is None:
                break
            cursor = oldest

        return [_to_broker_order(o) for o in collected.values()]

    def get_account_created_at(self) -> datetime:
        created = self.get_account().created_at
        if created is None:
            raise ExecutionError("Alpaca account response missing created_at")
        return created

    def get_order_by_id(self, order_id: str) -> BrokerOrder | None:
        """Fetch a single order by its Alpaca order ID. Returns None if not found."""
        try:
            result = _read_retrier(self._client.get_order_by_id, order_id=order_id)
        except Exception as exc:
            msg = str(exc).lower()
            if "not found" in msg or "404" in msg:
                return None
            raise ExecutionError(f"Failed to get order {order_id}: {exc}") from exc
        if result is None:
            return None
        return _to_broker_order(result)  # type: ignore[arg-type]

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
        if qty <= 0:
            raise OrderError(f"Invalid bracket order qty for {symbol}: {qty} (must be > 0)")
        if stop_loss_price <= 0:
            raise OrderError(
                f"Invalid stop_loss_price for {symbol}: {stop_loss_price} (must be > 0)"
            )
        if take_profit_price <= 0:
            raise OrderError(
                f"Invalid take_profit_price for {symbol}: {take_profit_price} (must be > 0)"
            )
        logger.info(
            "Submitting bracket order: %s qty=%d stop=%.2f tp=%.2f",
            symbol, qty, stop_loss_price, take_profit_price,
        )
        try:
            request = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=AlpacaOrderSide.BUY,
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
        if cancelled > 0:
            # Poll until Alpaca confirms cancellations (shares released)
            for _ in range(10):  # up to ~5s
                time.sleep(0.5)
                try:
                    remaining = [
                        o for o in self.get_orders(status="open") if o.symbol == symbol
                    ]
                except Exception:
                    break  # can't check — proceed anyway
                if not remaining:
                    break
            else:
                logger.warning(
                    "Orders for %s still open after cancellation timeout", symbol
                )
        return cancelled

    def cancel_all_orders(self) -> int:
        """Cancel every open order on the account. Returns count attempted.

        Wraps alpaca-py's bulk `DELETE /orders`. Never raises — failures are
        logged so reset flows can proceed to the next step.
        """
        try:
            responses = self._client.cancel_orders()
        except Exception:
            logger.warning("Failed to cancel all orders", exc_info=True)
            return 0
        if isinstance(responses, list):
            return len(responses)
        return 0

    def close_all_positions(self) -> int:
        """Liquidate every open position. Returns count attempted.

        Passes `cancel_orders=True` so lingering OCO legs don't block the
        close. Never raises — failures are logged.
        """
        try:
            responses = self._client.close_all_positions(cancel_orders=True)
        except Exception:
            logger.warning("Failed to close all positions", exc_info=True)
            return 0
        if isinstance(responses, list):
            return len(responses)
        return 0

    def close_position(self, symbol: str) -> str | None:
        """Close a position by symbol. Returns order ID or None if no position.

        Cancels any open orders (e.g. bracket OCO legs) for the symbol first,
        since they hold shares and block the close.
        """
        logger.info("Closing position: %s", symbol)
        self.cancel_orders_for_symbol(symbol)
        try:
            order = _close_retrier(
                self._client.close_position, symbol_or_asset_id=symbol
            )
            order_id = str(order.id)  # type: ignore[union-attr]
            logger.info("Close order submitted: %s order_id=%s", symbol, order_id)
            return order_id
        except Exception as exc:
            exc_str = str(exc).lower()
            if "not found" in exc_str or "no position" in exc_str:
                logger.warning("No position to close for %s", symbol)
                return None
            raise OrderError(f"Failed to close position for {symbol}: {exc}") from exc
