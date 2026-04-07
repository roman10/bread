"""Execution engine — order management, position lifecycle, broker reconciliation."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from bread.core.exceptions import OrderError
from bread.core.models import OrderSide, OrderStatus, Position, SignalDirection
from bread.db.models import OrderLog, PortfolioSnapshot
from bread.risk.limits import check_bracket_prices

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from bread.ai.client import ClaudeClient
    from bread.ai.models import SignalReview
    from bread.core.config import AppConfig
    from bread.core.models import Signal
    from bread.execution.alpaca_broker import AlpacaBroker
    from bread.risk.manager import RiskManager

logger = logging.getLogger(__name__)


class ExecutionEngine:
    def __init__(
        self,
        broker: AlpacaBroker,
        risk_manager: RiskManager,
        config: AppConfig,
        session_factory: sessionmaker[Session],
        claude_client: ClaudeClient | None = None,
    ) -> None:
        self._broker = broker
        self._risk = risk_manager
        self._config = config
        self._session_factory = session_factory
        self._claude = claude_client
        self._positions: dict[str, Position] = {}
        self._last_reviews: dict[str, SignalReview] = {}

    def _adjust_fill_price(self, raw_price: float, side: str) -> float:
        """Apply paper trading cost model to a fill price.

        Alpaca paper trading fills at quoted prices with no real spread or
        slippage.  This simulates real-world friction by penalizing fills:
        - BUY:  price * (1 + slippage_pct)  — we pay more than quoted
        - SELL: price * (1 - slippage_pct)  — we receive less than quoted

        Only applied when mode='paper' and paper_cost.enabled is True.
        Live-mode fills already include real market friction.
        """
        if self._config.mode != "paper":
            return raw_price
        cost = self._config.execution.paper_cost
        if not cost.enabled:
            return raw_price

        if side == "BUY":
            return raw_price * (1.0 + cost.slippage_pct)
        else:  # SELL
            return raw_price * (1.0 - cost.slippage_pct)

    def reconcile(self) -> None:
        """Sync local state with broker positions.

        - Broker has position we don't track -> add with warning
        - We track position broker doesn't have -> remove (bracket triggered)
        """
        try:
            broker_positions = self._broker.get_positions()
        except Exception:
            logger.exception("Failed to fetch broker positions during reconciliation")
            return

        broker_symbols: set[str] = set()
        for bp in broker_positions:
            symbol = bp.symbol
            broker_symbols.add(symbol)
            if symbol not in self._positions:
                logger.warning(
                    "Reconciliation: found untracked position %s qty=%s on broker",
                    symbol,
                    bp.qty,
                )
                self._positions[symbol] = Position(
                    symbol=symbol,
                    qty=int(float(bp.qty or 0)),
                    entry_price=float(bp.avg_entry_price or 0),
                    stop_loss_price=0.0,
                    take_profit_price=0.0,
                    broker_order_id="",
                    strategy_name="unknown",
                    entry_date=date.today(),
                )

        # Remove positions no longer on broker
        closed = [s for s in self._positions if s not in broker_symbols]
        for symbol in closed:
            logger.info(
                "Reconciliation: position %s no longer on broker (bracket triggered)",
                symbol,
            )
            del self._positions[symbol]

        # Update order fill status
        self._reconcile_orders()

    def _reconcile_orders(self) -> None:
        """Update pending/accepted orders with fill status from broker."""
        try:
            with self._session_factory() as session:
                pending = (
                    session.execute(
                        select(OrderLog).where(OrderLog.status.in_(["PENDING", "ACCEPTED"]))
                    )
                    .scalars()
                    .all()
                )

                if not pending:
                    return

                try:
                    broker_orders = self._broker.get_orders(status="all")
                except Exception:
                    logger.exception("Failed to fetch orders for reconciliation")
                    return

                broker_map = {str(o.id): o for o in broker_orders}

                for order in pending:
                    if not order.broker_order_id:
                        continue
                    broker_order = broker_map.get(order.broker_order_id)
                    if broker_order is None:
                        continue

                    new_status = str(broker_order.status).upper()
                    if new_status == order.status:
                        continue

                    order.status = new_status
                    if new_status == "FILLED":
                        if broker_order.filled_avg_price is not None:
                            raw_price = float(broker_order.filled_avg_price)
                            order.raw_filled_price = raw_price
                            # Apply paper trading cost model (slippage/spread).
                            # In live mode, _adjust_fill_price returns raw_price unchanged.
                            order.filled_price = self._adjust_fill_price(
                                raw_price,
                                order.side,
                            )
                        order.filled_at_utc = broker_order.filled_at
                        if (
                            order.raw_filled_price is not None
                            and order.filled_price is not None
                            and order.raw_filled_price != order.filled_price
                        ):
                            logger.info(
                                "Order %s filled: %s %s @ %.2f (raw: %.2f)",
                                order.broker_order_id,
                                order.side,
                                order.symbol,
                                order.filled_price,
                                order.raw_filled_price,
                            )
                        else:
                            logger.info(
                                "Order %s filled: %s %s @ %s",
                                order.broker_order_id,
                                order.side,
                                order.symbol,
                                f"{order.filled_price:.2f}" if order.filled_price else "N/A",
                            )
                    elif new_status in ("CANCELLED", "REJECTED"):
                        logger.info(
                            "Order %s %s: %s %s",
                            order.broker_order_id,
                            new_status.lower(),
                            order.side,
                            order.symbol,
                        )

                session.commit()

                # Cancel stale orders stuck in PENDING/ACCEPTED too long
                self._cancel_stale_orders(session)
        except Exception:
            logger.exception("Failed to reconcile orders")

    def _cancel_stale_orders(self, session: Session) -> None:
        """Cancel orders stuck in PENDING/ACCEPTED beyond the timeout."""
        timeout = self._config.execution.stale_order_timeout_minutes
        cutoff = datetime.now(UTC) - timedelta(minutes=timeout)
        stale = (
            session.execute(
                select(OrderLog).where(
                    OrderLog.status.in_(["PENDING", "ACCEPTED"]),
                    OrderLog.created_at_utc < cutoff,
                )
            )
            .scalars()
            .all()
        )
        for order in stale:
            logger.warning(
                "Stale order %s for %s (age > %d min) — cancelling",
                order.broker_order_id,
                order.symbol,
                timeout,
            )
            if order.broker_order_id:
                self._broker.cancel_orders_for_symbol(order.symbol)
            order.status = "CANCELLED"
        if stale:
            session.commit()

    def process_signals(
        self,
        signals: list[Signal],
        prices: dict[str, float],
    ) -> None:
        """Process strategy signals. SELL first, then BUY with risk checks."""
        # Fetch open orders once for idempotency checks
        try:
            open_orders = self._broker.get_orders(status="open")
            pending_symbols = {o.symbol for o in open_orders}
        except Exception:
            logger.exception("Failed to fetch open orders, proceeding with empty set")
            pending_symbols = set()

        # Fetch account once
        try:
            account = self._broker.get_account()
        except Exception:
            logger.exception("Failed to fetch account, skipping signal processing")
            return

        equity = float(account.equity or 0)
        buying_power = float(account.buying_power or 0)
        daily_pnl = equity - float(account.last_equity or equity)

        # Split signals
        sell_signals = [s for s in signals if s.direction == SignalDirection.SELL]
        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        buy_signals.sort(key=lambda s: (-s.strength, s.symbol))

        # Process SELLs first
        for sig in sell_signals:
            if sig.symbol not in self._positions:
                logger.debug("SELL signal for %s ignored — no position", sig.symbol)
                continue
            try:
                order_id = self._broker.close_position(sig.symbol)
                if order_id:
                    self._log_order(
                        sig.symbol,
                        OrderSide.SELL,
                        self._positions[sig.symbol].qty,
                        OrderStatus.PENDING,
                        order_id,
                        sig.strategy_name,
                        sig.reason,
                    )
                    del self._positions[sig.symbol]
                    logger.info("SELL %s: position closed, reason=%s", sig.symbol, sig.reason)
            except OrderError:
                logger.exception("Failed to close position %s", sig.symbol)

        # Process BUYs — three phases: risk approval, Claude review, order submission
        peak_equity = self._get_peak_equity(equity)
        weekly_pnl = self._get_weekly_pnl(equity)
        day_trade_count = self._get_day_trade_count()

        # Phase A: Collect risk-approved signals
        approved_buys: list[tuple[Signal, int, float]] = []
        for sig in buy_signals:
            if sig.symbol in self._positions:
                logger.debug("BUY %s skipped — already held", sig.symbol)
                continue
            if sig.symbol in pending_symbols:
                logger.debug("BUY %s skipped — pending order exists", sig.symbol)
                continue

            price = prices.get(sig.symbol)
            if price is None or price <= 0:
                logger.warning("BUY %s skipped — no price available", sig.symbol)
                continue

            shares, validation = self._risk.evaluate(
                signal=sig,
                price=price,
                buying_power=buying_power,
                equity=equity,
                positions=list(self._positions.values()),
                peak_equity=peak_equity,
                daily_pnl=daily_pnl,
                weekly_pnl=weekly_pnl,
                day_trade_count=day_trade_count,
            )

            if not validation.approved:
                logger.info("BUY %s rejected: %s", sig.symbol, validation.rejections)
                self._log_order(
                    sig.symbol,
                    OrderSide.BUY,
                    shares,
                    OrderStatus.REJECTED,
                    None,
                    sig.strategy_name,
                    f"rejected: {validation.rejections}",
                )
                continue

            approved_buys.append((sig, shares, price))
            buying_power -= shares * price  # Reserve capital for subsequent risk evals

        # Phase B: Claude AI review (if enabled)
        self._last_reviews.clear()
        if self._claude and self._config.claude.enabled and approved_buys:
            approved_buys = self._claude_review_batch(
                approved_buys,
                equity,
                buying_power,
                daily_pnl,
                weekly_pnl,
                peak_equity,
            )

        # Phase C: Submit orders
        for sig, shares, price in approved_buys:
            stop_loss_price = price * (1 - sig.stop_loss_pct)
            take_profit_pct = sig.stop_loss_pct * self._config.execution.take_profit_ratio
            take_profit_price = price * (1 + take_profit_pct)

            bracket_ok, bracket_reason = check_bracket_prices(
                price, stop_loss_price, take_profit_price
            )
            if not bracket_ok:
                logger.error(
                    "BUY %s rejected: invalid bracket — %s", sig.symbol, bracket_reason
                )
                self._log_order(
                    sig.symbol,
                    OrderSide.BUY,
                    shares,
                    OrderStatus.REJECTED,
                    None,
                    sig.strategy_name,
                    f"invalid bracket: {bracket_reason}",
                )
                continue

            try:
                order_id = self._broker.submit_bracket_order(
                    sig.symbol,
                    shares,
                    stop_loss_price,
                    take_profit_price,
                )
            except OrderError:
                logger.exception("Failed to submit bracket order for %s", sig.symbol)
                continue

            self._positions[sig.symbol] = Position(
                symbol=sig.symbol,
                qty=shares,
                entry_price=price,
                stop_loss_price=stop_loss_price,
                take_profit_price=take_profit_price,
                broker_order_id=order_id,
                strategy_name=sig.strategy_name,
                entry_date=date.today(),
            )
            self._log_order(
                sig.symbol,
                OrderSide.BUY,
                shares,
                OrderStatus.PENDING,
                order_id,
                sig.strategy_name,
                sig.reason,
                stop_loss_price=stop_loss_price,
                take_profit_price=take_profit_price,
            )
            logger.info(
                "BUY %s: shares=%d price=%.2f stop=%.2f tp=%.2f order=%s",
                sig.symbol,
                shares,
                price,
                stop_loss_price,
                take_profit_price,
                order_id,
            )

    def _claude_review_batch(
        self,
        approved_buys: list[tuple[Signal, int, float]],
        equity: float,
        buying_power: float,
        daily_pnl: float,
        weekly_pnl: float,
        peak_equity: float,
    ) -> list[tuple[Signal, int, float]]:
        """Filter approved buys through Claude AI review.

        In advisory mode, all signals pass through (review is logged only).
        In gating mode, rejected signals are removed.
        On any error, returns the original list unchanged (fail-open).
        """
        from bread.ai.models import TradeContext

        context = TradeContext(
            equity=equity,
            buying_power=buying_power,
            open_positions=list(self._positions.keys()),
            daily_pnl=daily_pnl,
            weekly_pnl=weekly_pnl,
            peak_equity=peak_equity,
        )
        signals = [sig for sig, _, _ in approved_buys]

        # Enrich review with recent event alerts (fail-open)
        event_alerts = None
        try:
            from bread.ai.research import get_active_alerts

            signal_symbols = [sig.symbol for sig in signals]
            event_alerts = get_active_alerts(self._session_factory, signal_symbols) or None
        except Exception:
            logger.debug("Could not fetch event alerts for review enrichment")

        try:
            reviews = self._claude.review_signals_batch(signals, context, event_alerts=event_alerts)  # type: ignore[union-attr]
        except Exception:
            logger.exception("Claude batch review failed, proceeding without review")
            return approved_buys

        is_gating = self._config.claude.review_mode == "gating"
        result: list[tuple[Signal, int, float]] = []

        for (sig, shares, price), review in zip(approved_buys, reviews):
            self._last_reviews[sig.symbol] = review
            logger.info(
                "Claude review %s: approved=%s confidence=%.2f reason=%s flags=%s",
                sig.symbol,
                review.approved,
                review.confidence,
                review.reasoning[:100],
                review.risk_flags,
            )
            if is_gating and not review.approved:
                self._log_order(
                    sig.symbol,
                    OrderSide.BUY,
                    shares,
                    OrderStatus.REJECTED,
                    None,
                    sig.strategy_name,
                    f"claude_rejected: {review.reasoning[:200]}",
                )
                continue
            result.append((sig, shares, price))

        return result

    def get_last_review(self, symbol: str) -> SignalReview | None:
        """Return Claude's most recent review for *symbol*, if any."""
        return self._last_reviews.get(symbol)

    def get_positions(self) -> list[Position]:
        """Return current tracked positions."""
        return list(self._positions.values())

    def get_account(self) -> object:
        """Return the broker account object."""
        return self._broker.get_account()

    def get_equity(self) -> float:
        """Return current account equity from broker."""
        account = self._broker.get_account()
        return float(account.equity or 0)

    def _get_cumulative_cost_adjustment(self) -> float:
        """Total P&L impact of paper trading cost adjustments.

        Sums the per-order cost drag (slippage + commission) across all filled
        orders that have both raw and adjusted prices.  Returns a negative
        number representing the realistic cost friction that Alpaca's paper
        account doesn't reflect.
        """
        if self._config.mode != "paper":
            return 0.0
        cost = self._config.execution.paper_cost
        if not cost.enabled:
            return 0.0

        try:
            with self._session_factory() as session:
                filled_orders = (
                    session.execute(
                        select(OrderLog).where(
                            OrderLog.status == "FILLED",
                            OrderLog.raw_filled_price.isnot(None),
                            OrderLog.filled_price.isnot(None),
                        )
                    )
                    .scalars()
                    .all()
                )

                total = 0.0
                adjusted_count = 0
                for order in filled_orders:
                    raw = order.raw_filled_price
                    adj = order.filled_price
                    if raw is None or adj is None:
                        continue  # already filtered by SQL, but satisfies mypy
                    # BUY: we paid more → (raw - adjusted) * qty is negative
                    # SELL: we received less → (adjusted - raw) * qty is negative
                    if order.side == "BUY":
                        total += (raw - adj) * order.qty
                    else:
                        total += (adj - raw) * order.qty
                    # Only count commission for orders that had cost adjustment
                    # applied (raw != adj).  Historical orders backfilled by
                    # migration have raw == adj and should not be charged.
                    if raw != adj:
                        adjusted_count += 1

                total -= adjusted_count * cost.commission_per_trade

                return total
        except Exception:
            logger.exception("Failed to compute cumulative cost adjustment")
            return 0.0

    def save_snapshot(self) -> None:
        """Persist a PortfolioSnapshot to the database.

        In paper mode, adjusts equity to reflect simulated friction costs.
        Alpaca's paper account doesn't account for our slippage/commission
        model, so we subtract the cumulative cost drag from the reported equity.
        """
        try:
            account = self._broker.get_account()
            equity = float(account.equity or 0)
            cash = float(account.cash or 0)
            daily_pnl = equity - float(account.last_equity or equity)

            # Adjust for paper trading cost model
            cost_adjustment = self._get_cumulative_cost_adjustment()
            adjusted_equity = equity + cost_adjustment  # cost_adjustment is negative
            adjusted_cash = cash + cost_adjustment

            snapshot = PortfolioSnapshot(
                timestamp_utc=datetime.now(UTC),
                equity=adjusted_equity,
                cash=adjusted_cash,
                positions_value=adjusted_equity - adjusted_cash,
                open_positions=len(self._positions),
                daily_pnl=daily_pnl,
            )
            with self._session_factory() as session:
                session.add(snapshot)
                session.commit()
        except Exception:
            logger.exception("Failed to save portfolio snapshot")

    def _get_peak_equity(self, current_equity: float) -> float:
        """Get peak equity from portfolio_snapshots, defaulting to current."""
        try:
            with self._session_factory() as session:
                result = session.execute(
                    select(func.max(PortfolioSnapshot.equity))
                ).scalar_one_or_none()
                return max(result or current_equity, current_equity)
        except Exception:
            logger.exception("Failed to query peak equity")
            return current_equity

    def _get_weekly_pnl(self, current_equity: float) -> float:
        """Compute weekly P&L as change in equity since start of week."""
        try:
            today = date.today()
            monday = today - timedelta(days=today.weekday())
            week_start = datetime(monday.year, monday.month, monday.day, tzinfo=UTC)

            with self._session_factory() as session:
                # Get the earliest snapshot from this week as the baseline
                start_equity = session.execute(
                    select(PortfolioSnapshot.equity)
                    .where(PortfolioSnapshot.timestamp_utc >= week_start)
                    .order_by(PortfolioSnapshot.timestamp_utc)
                    .limit(1)
                ).scalar_one_or_none()

            if start_equity is None:
                return 0.0
            return current_equity - start_equity
        except Exception:
            logger.exception("Failed to compute weekly P&L")
            return 0.0

    def _get_day_trade_count(self) -> int:
        """Count day trades (same-day buy+sell) in last 5 trading days."""
        try:
            cutoff = datetime.now(UTC) - timedelta(days=7)  # 7 calendar days >= 5 trading days
            with self._session_factory() as session:
                rows = session.execute(
                    select(OrderLog.symbol, OrderLog.side, OrderLog.filled_at_utc).where(
                        OrderLog.status == OrderStatus.FILLED,
                        OrderLog.filled_at_utc >= cutoff,
                    )
                ).all()

            # Group by symbol+date, check for both BUY and SELL
            by_symbol_date: dict[tuple[str, date], set[str]] = {}
            for symbol, side, filled_at in rows:
                if filled_at is None:
                    continue
                key = (symbol, filled_at.date())
                by_symbol_date.setdefault(key, set()).add(side)

            return sum(
                1
                for sides in by_symbol_date.values()
                if OrderSide.BUY in sides and OrderSide.SELL in sides
            )
        except Exception:
            logger.exception("Failed to count day trades")
            return 0

    def _log_order(
        self,
        symbol: str,
        side: OrderSide,
        qty: int,
        status: OrderStatus,
        broker_order_id: str | None,
        strategy_name: str,
        reason: str,
        stop_loss_price: float | None = None,
        take_profit_price: float | None = None,
    ) -> None:
        """Persist an order record to the database."""
        try:
            with self._session_factory() as session:
                session.add(
                    OrderLog(
                        broker_order_id=broker_order_id,
                        symbol=symbol,
                        side=side,
                        qty=qty,
                        status=status,
                        stop_loss_price=stop_loss_price,
                        take_profit_price=take_profit_price,
                        strategy_name=strategy_name,
                        reason=reason,
                        created_at_utc=datetime.now(UTC),
                    )
                )
                session.commit()
        except Exception:
            logger.exception("Failed to log order for %s", symbol)
