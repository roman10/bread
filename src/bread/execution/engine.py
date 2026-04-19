"""Execution engine — order management, position lifecycle, broker reconciliation."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from bread.core.exceptions import OrderError
from bread.core.models import OrderSide, OrderStatus, Position, SignalDirection
from bread.db.models import OrderLog, PortfolioSnapshot, StrategySnapshot
from bread.risk.context import RiskContext, RiskContextRepo
from bread.risk.limits import check_bracket_prices

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from bread.ai.client import ClaudeClient
    from bread.ai.models import SignalReview
    from bread.core.config import AppConfig
    from bread.core.models import Signal
    from bread.execution.broker import Broker
    from bread.execution.models import Account
    from bread.risk.manager import RiskManager

UNKNOWN_STRATEGY = "unknown"

logger = logging.getLogger(__name__)


def adjust_fill_price(config: AppConfig, raw_price: float, side: str) -> float:
    """Apply paper trading cost model to a fill price.

    Alpaca paper trading fills at quoted prices with no real spread or
    slippage.  This simulates real-world friction by penalizing fills:
    - BUY:  price * (1 + slippage_pct)  — we pay more than quoted
    - SELL: price * (1 - slippage_pct)  — we receive less than quoted

    Only applied when mode='paper' and paper_cost.enabled is True.
    Live-mode fills already include real market friction.
    """
    if config.mode != "paper":
        return raw_price
    cost = config.execution.paper_cost
    if not cost.enabled:
        return raw_price

    if side == "BUY":
        return raw_price * (1.0 + cost.slippage_pct)
    return raw_price * (1.0 - cost.slippage_pct)


class ExecutionEngine:
    def __init__(
        self,
        broker: Broker,
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
        # Positions are stored as a list so two strategies can each hold the
        # same symbol. `Position.strategy_name` carries ownership; helper
        # methods below encapsulate the (symbol, strategy_name) lookups.
        self._positions: list[Position] = []
        self._last_reviews: dict[str, SignalReview] = {}
        self._risk_context = RiskContextRepo(session_factory)

    # ------------------------------------------------------------------
    # Position-list helpers
    # ------------------------------------------------------------------

    def _find(self, symbol: str, strategy_name: str) -> Position | None:
        for p in self._positions:
            if p.symbol == symbol and p.strategy_name == strategy_name:
                return p
        return None

    def _remove(self, position: Position) -> None:
        # Identity-based removal — two Positions can share (symbol, strategy)
        # only transiently during reconciliation, and we always want to drop
        # the exact instance we inspected.
        self._positions = [p for p in self._positions if p is not position]

    def _held_symbol_strategy_keys(self) -> set[tuple[str, str]]:
        return {(p.symbol, p.strategy_name) for p in self._positions}

    def reconcile(self) -> None:
        """Sync local state with broker reality per-position.

        Each tracked Position carries its own bracket parent + OCO legs. A
        closed bracket (either leg FILLED, or the whole group CANCELLED)
        means that specific Position is done, regardless of whether another
        strategy still holds shares of the same symbol. Broker-side shares
        in excess of tracked qty go into an "unknown" bucket so they remain
        visible without being eligible for strategy SELLs.
        """
        try:
            broker_positions = self._broker.get_positions()
        except Exception:
            logger.exception("Failed to fetch broker positions during reconciliation")
            return

        broker_qty_by_symbol: dict[str, float] = {bp.symbol: bp.qty for bp in broker_positions}
        broker_entry_price_by_symbol: dict[str, float] = {
            bp.symbol: bp.avg_entry_price for bp in broker_positions
        }

        # 1) Drop tracked positions whose bracket is done on the broker.
        to_remove: list[Position] = []
        for pos in self._positions:
            if pos.strategy_name == UNKNOWN_STRATEGY:
                continue  # handled in step 3
            if self._bracket_completed(pos):
                logger.info(
                    "Reconciliation: position %s/%s closed (bracket completed)",
                    pos.symbol, pos.strategy_name,
                )
                to_remove.append(pos)
        for pos in to_remove:
            self._remove(pos)

        # 2) Drop tracked positions for symbols the broker no longer holds.
        # This catches the edge case where a bracket leg fill notification
        # hasn't landed yet but the position is gone on the broker.
        survivors: list[Position] = []
        for pos in self._positions:
            broker_qty = broker_qty_by_symbol.get(pos.symbol, 0.0)
            if broker_qty <= 0 and pos.strategy_name != UNKNOWN_STRATEGY:
                logger.info(
                    "Reconciliation: position %s/%s no longer on broker — removing",
                    pos.symbol, pos.strategy_name,
                )
                continue
            survivors.append(pos)
        self._positions = survivors

        # 3) Reconcile "unknown" buckets against any broker-side excess qty
        # (shares on the broker we don't account for, e.g. manually opened
        # positions or reconciliation gaps). Collapse to one unknown Position
        # per symbol holding the delta.
        # Drop stale unknowns first; we'll re-create if still needed.
        self._positions = [p for p in self._positions if p.strategy_name != UNKNOWN_STRATEGY]
        tracked_qty_by_symbol: dict[str, int] = {}
        for pos in self._positions:
            tracked_qty_by_symbol[pos.symbol] = (
                tracked_qty_by_symbol.get(pos.symbol, 0) + pos.qty
            )

        for symbol, broker_qty in broker_qty_by_symbol.items():
            tracked = tracked_qty_by_symbol.get(symbol, 0)
            delta = int(broker_qty) - tracked
            if delta <= 0:
                continue
            entry_price = broker_entry_price_by_symbol.get(symbol, 0.0)
            logger.warning(
                "Reconciliation: %d untracked shares of %s on broker — tagged 'unknown'",
                delta, symbol,
            )
            self._positions.append(
                Position(
                    symbol=symbol,
                    qty=delta,
                    entry_price=entry_price,
                    stop_loss_price=0.0,
                    take_profit_price=0.0,
                    broker_order_id="",
                    strategy_name=UNKNOWN_STRATEGY,
                    entry_date=date.today(),
                )
            )

        # Update order fill status
        self._reconcile_orders()

    def _bracket_completed(self, pos: Position) -> bool:
        """True when either bracket OCO leg has FILLED.

        Checking legs is more reliable than checking the parent, because the
        parent is FILLED the moment the market BUY executes; the position
        itself closes only when one of the OCO legs fills. A best-effort
        check — any error returns False so a transient API issue doesn't
        evict a live position.
        """
        for leg_id in (pos.stop_loss_order_id, pos.take_profit_order_id):
            if not leg_id:
                continue
            try:
                order = self._broker.get_order_by_id(leg_id)
            except Exception:
                logger.debug(
                    "Could not fetch bracket leg %s for %s/%s",
                    leg_id, pos.symbol, pos.strategy_name,
                )
                return False
            if order is not None and order.status == OrderStatus.FILLED:
                return True
        return False

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

                broker_map = {o.id: o for o in broker_orders}

                for order in pending:
                    if not order.broker_order_id:
                        continue
                    broker_order = broker_map.get(order.broker_order_id)
                    if broker_order is None:
                        continue

                    if broker_order.status is None:
                        # Don't overwrite a valid prior status with a value the
                        # broker layer couldn't normalize.
                        continue
                    new_status = broker_order.status.value
                    if new_status == order.status:
                        continue

                    order.status = new_status
                    if new_status == "FILLED":
                        if broker_order.filled_avg_price is not None:
                            raw_price = broker_order.filled_avg_price
                            order.raw_filled_price = raw_price
                            # Apply paper trading cost model (slippage/spread).
                            # In live mode, adjust_fill_price returns raw_price unchanged.
                            order.filled_price = adjust_fill_price(
                                self._config, raw_price, order.side
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
            # Cancel the specific stale order only. Using
            # cancel_orders_for_symbol here would kill every open order for
            # the symbol, including another strategy's live bracket legs.
            if order.broker_order_id:
                self._broker.cancel_order(order.broker_order_id)
            order.status = "CANCELLED"
        if stale:
            session.commit()

    def process_signals(
        self,
        signals: list[Signal],
        prices: dict[str, float],
    ) -> None:
        """Process strategy signals. SELL first, then BUY with risk checks."""
        # Pending-by-(symbol, strategy) comes from OrderLog so a strategy can
        # still buy a symbol that another strategy has pending on the broker.
        pending_keys = self._pending_order_keys()

        # Fetch account once
        try:
            account = self._broker.get_account()
        except Exception:
            logger.exception("Failed to fetch account, skipping signal processing")
            return

        equity = account.equity
        buying_power = account.buying_power
        daily_pnl = equity - (account.last_equity or equity)

        # Split signals
        sell_signals = [s for s in signals if s.direction == SignalDirection.SELL]
        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        buy_signals.sort(key=lambda s: (-s.strength, s.symbol))

        # Process SELLs first — each closes the caller's own position only.
        for sig in sell_signals:
            position = self._find(sig.symbol, sig.strategy_name)
            if position is None:
                logger.debug(
                    "SELL %s from '%s' ignored — no position for this strategy",
                    sig.symbol, sig.strategy_name,
                )
                continue
            self._close_position(position, sig.reason)

        # Process BUYs — three phases: risk approval, Claude review, order submission
        risk_ctx = self._risk_context.fetch(equity, buying_power, daily_pnl)

        # Phase A: Collect risk-approved signals.
        # Dedup is keyed on (symbol, strategy_name) — two strategies buying
        # the same symbol in one tick is allowed; one strategy emitting the
        # same signal twice is not. "Already held" is also per-(symbol,
        # strategy): strategy B may still open SPY when strategy A holds it.
        approved_buys: list[tuple[Signal, int, float]] = []
        approved_keys: set[tuple[str, str]] = set()
        held_keys = self._held_symbol_strategy_keys()
        for sig in buy_signals:
            key = (sig.symbol, sig.strategy_name)
            if key in held_keys:
                logger.debug(
                    "BUY %s/%s skipped — strategy already holds this symbol",
                    sig.symbol, sig.strategy_name,
                )
                continue
            if key in pending_keys:
                logger.debug(
                    "BUY %s/%s skipped — pending order exists for this strategy",
                    sig.symbol, sig.strategy_name,
                )
                continue
            if key in approved_keys:
                logger.debug(
                    "BUY %s/%s skipped — duplicate signal in tick",
                    sig.symbol, sig.strategy_name,
                )
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
                positions=list(self._positions),
                peak_equity=risk_ctx.peak_equity,
                daily_pnl=daily_pnl,
                weekly_pnl=risk_ctx.weekly_pnl,
                day_trade_count=risk_ctx.day_trade_count,
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
            approved_keys.add(key)
            buying_power -= shares * price  # Reserve capital for subsequent risk evals

        # Phase B: Claude AI review (if enabled)
        self._last_reviews.clear()
        if self._claude and self._config.claude.enabled and approved_buys:
            approved_buys = self._claude_review_batch(approved_buys, risk_ctx)

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
                bracket = self._broker.submit_bracket_order(
                    sig.symbol,
                    shares,
                    stop_loss_price,
                    take_profit_price,
                )
            except OrderError:
                logger.exception("Failed to submit bracket order for %s", sig.symbol)
                continue

            self._positions.append(
                Position(
                    symbol=sig.symbol,
                    qty=shares,
                    entry_price=price,
                    stop_loss_price=stop_loss_price,
                    take_profit_price=take_profit_price,
                    broker_order_id=bracket.parent_order_id,
                    strategy_name=sig.strategy_name,
                    entry_date=date.today(),
                    stop_loss_order_id=bracket.stop_loss_order_id,
                    take_profit_order_id=bracket.take_profit_order_id,
                )
            )
            self._log_order(
                sig.symbol,
                OrderSide.BUY,
                shares,
                OrderStatus.PENDING,
                bracket.parent_order_id,
                sig.strategy_name,
                sig.reason,
                stop_loss_price=stop_loss_price,
                take_profit_price=take_profit_price,
            )
            logger.info(
                "BUY %s/%s: shares=%d price=%.2f stop=%.2f tp=%.2f order=%s",
                sig.symbol,
                sig.strategy_name,
                shares,
                price,
                stop_loss_price,
                take_profit_price,
                bracket.parent_order_id,
            )

    def _close_position(self, position: Position, reason: str) -> None:
        """Cancel this position's OCO legs and submit a qty-specific SELL.

        Unlike ``broker.close_position(symbol)`` — which liquidates EVERY share
        of the symbol on the broker — this path only sells this Position's
        qty, leaving other strategies' shares intact. The two OCO legs are
        cancelled first so stop/take-profit can't fire against a different
        strategy's shares after our sell.
        """
        if position.strategy_name == UNKNOWN_STRATEGY:
            # Unknown-owner positions have no bracket we created; fall back to
            # broker.close_position. This path is reset-flow / manual-cleanup
            # territory, not a per-strategy SELL.
            try:
                self._broker.close_position(position.symbol)
            except OrderError:
                logger.exception("Failed to close unknown position %s", position.symbol)
                return
            self._remove(position)
            logger.info("SELL %s (unknown): closed via close_position", position.symbol)
            return

        for leg_id in (position.stop_loss_order_id, position.take_profit_order_id):
            if leg_id:
                try:
                    self._broker.cancel_order(leg_id)
                except Exception:
                    logger.debug("Leg cancel failed for %s (leg=%s)", position.symbol, leg_id)

        try:
            order_id = self._broker.submit_market_sell(position.symbol, position.qty)
        except OrderError:
            logger.exception(
                "Failed to submit market sell for %s/%s",
                position.symbol, position.strategy_name,
            )
            return

        self._log_order(
            position.symbol,
            OrderSide.SELL,
            position.qty,
            OrderStatus.PENDING,
            order_id,
            position.strategy_name,
            reason,
        )
        self._remove(position)
        logger.info(
            "SELL %s/%s: qty=%d order=%s reason=%s",
            position.symbol, position.strategy_name, position.qty, order_id, reason,
        )

    def _pending_order_keys(self) -> set[tuple[str, str]]:
        """Return {(symbol, strategy_name)} of orders currently pending.

        Reads from the local OrderLog rather than broker.get_orders because
        the broker doesn't know which of our strategies owns an order. The
        broker's open-orders view is used separately by other callers that
        need raw broker state.
        """
        try:
            with self._session_factory() as session:
                rows = (
                    session.execute(
                        select(OrderLog.symbol, OrderLog.strategy_name).where(
                            OrderLog.status.in_(["PENDING", "ACCEPTED"]),
                            OrderLog.side == "BUY",
                        )
                    )
                    .all()
                )
                return {(sym, strat) for sym, strat in rows}
        except Exception:
            logger.exception("Failed to fetch pending order keys, proceeding with empty set")
            return set()

    def _claude_review_batch(
        self,
        approved_buys: list[tuple[Signal, int, float]],
        risk_ctx: RiskContext,
    ) -> list[tuple[Signal, int, float]]:
        """Filter approved buys through Claude AI review.

        In advisory mode, all signals pass through (review is logged only).
        In gating mode, rejected signals are removed.
        On any error, returns the original list unchanged (fail-open).
        """
        from bread.ai.models import TradeContext

        context = TradeContext(
            equity=risk_ctx.equity,
            buying_power=risk_ctx.buying_power,
            # Collapse to unique symbols — the review context is per-symbol
            # risk color, not per-strategy attribution.
            open_positions=sorted({p.symbol for p in self._positions}),
            daily_pnl=risk_ctx.daily_pnl,
            weekly_pnl=risk_ctx.weekly_pnl,
            peak_equity=risk_ctx.peak_equity,
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
        return list(self._positions)

    def get_account(self) -> Account:
        """Return the broker account snapshot."""
        return self._broker.get_account()

    def get_equity(self) -> float:
        """Return current account equity from broker."""
        return self._broker.get_account().equity

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
            equity = account.equity
            cash = account.cash
            daily_pnl = equity - (account.last_equity or equity)

            # Adjust for paper trading cost model
            cost_adjustment = self._get_cumulative_cost_adjustment()
            adjusted_equity = equity + cost_adjustment  # cost_adjustment is negative
            adjusted_cash = cash + cost_adjustment

            now_ts = datetime.now(UTC)
            snapshot = PortfolioSnapshot(
                timestamp_utc=now_ts,
                equity=adjusted_equity,
                cash=adjusted_cash,
                positions_value=adjusted_equity - adjusted_cash,
                open_positions=len(self._positions),
                daily_pnl=daily_pnl,
            )
            # Per-strategy snapshots — the dashboard uses these to render
            # separate equity curves per strategy, critical for fair comparison.
            current_prices = self._current_prices_by_symbol()
            strategy_snaps = self._build_strategy_snapshots(now_ts, current_prices)

            with self._session_factory() as session:
                session.add(snapshot)
                for ss in strategy_snaps:
                    session.add(ss)
                session.commit()
        except Exception:
            logger.exception("Failed to save portfolio snapshot")

    def _current_prices_by_symbol(self) -> dict[str, float]:
        """Fetch the broker's current per-symbol prices for unrealized-P&L calc."""
        try:
            broker_positions = self._broker.get_positions()
        except Exception:
            logger.exception("Failed to fetch broker positions for snapshot")
            return {}
        return {bp.symbol: bp.current_price for bp in broker_positions}

    def _build_strategy_snapshots(
        self,
        timestamp: datetime,
        current_prices: dict[str, float],
    ) -> list[StrategySnapshot]:
        """Build one StrategySnapshot per strategy with activity (open OR closed).

        Realized P&L comes from the trade journal (FIFO-paired BUY/SELL rows in
        OrderLog). Unrealized P&L comes from each tracked Position priced at the
        broker's current price. Equity = realized + unrealized — a delta curve
        relative to a notional zero baseline, which is enough for the dashboard
        to compare strategy trajectories over time.
        """
        from bread.monitoring.journal import get_all_strategies_summary

        try:
            with self._session_factory() as session:
                summaries = get_all_strategies_summary(
                    session, days=10_000, current_prices=current_prices,
                )
        except Exception:
            logger.exception("Failed to compute per-strategy summaries for snapshot")
            return []

        # Make sure strategies with only open (unmatched) positions also get a
        # row — the summary filters on closed-round-trip presence, so a pure
        # open-position strategy would otherwise be invisible on the chart.
        strategies_with_open: dict[str, list[Position]] = {}
        for p in self._positions:
            if p.strategy_name == UNKNOWN_STRATEGY:
                continue
            strategies_with_open.setdefault(p.strategy_name, []).append(p)

        snaps: list[StrategySnapshot] = []
        seen: set[str] = set()

        for summary in summaries:
            snaps.append(
                StrategySnapshot(
                    timestamp_utc=timestamp,
                    strategy_name=summary.strategy_name,
                    realized_pnl=summary.realized_pnl,
                    unrealized_pnl=summary.unrealized_pnl,
                    equity=summary.total_pnl,
                    open_positions=summary.open_positions,
                )
            )
            seen.add(summary.strategy_name)

        for strategy_name, positions in strategies_with_open.items():
            if strategy_name in seen:
                continue
            # Compute unrealized directly from our tracked positions.
            unrealized = 0.0
            for pos in positions:
                price = current_prices.get(pos.symbol)
                if price is None:
                    continue
                unrealized += (price - pos.entry_price) * pos.qty
            snaps.append(
                StrategySnapshot(
                    timestamp_utc=timestamp,
                    strategy_name=strategy_name,
                    realized_pnl=0.0,
                    unrealized_pnl=unrealized,
                    equity=unrealized,
                    open_positions=len(positions),
                )
            )

        return snaps

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
