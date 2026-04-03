"""Execution engine — order management, position lifecycle, broker reconciliation."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from bread.core.exceptions import OrderError
from bread.core.models import OrderSide, OrderStatus, Position, SignalDirection
from bread.db.models import OrderLog, PortfolioSnapshot

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

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
    ) -> None:
        self._broker = broker
        self._risk = risk_manager
        self._config = config
        self._session_factory = session_factory
        self._positions: dict[str, Position] = {}

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
                    symbol, bp.qty,
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
                        sig.symbol, OrderSide.SELL, self._positions[sig.symbol].qty,
                        OrderStatus.PENDING, order_id, sig.strategy_name, sig.reason,
                    )
                    del self._positions[sig.symbol]
                    logger.info("SELL %s: position closed, reason=%s", sig.symbol, sig.reason)
            except OrderError:
                logger.exception("Failed to close position %s", sig.symbol)

        # Process BUYs
        peak_equity = self._get_peak_equity(equity)
        weekly_pnl = self._get_weekly_pnl(equity)
        day_trade_count = self._get_day_trade_count()

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
                    sig.symbol, OrderSide.BUY, shares, OrderStatus.REJECTED,
                    None, sig.strategy_name, f"rejected: {validation.rejections}",
                )
                continue

            # Compute bracket prices
            stop_loss_price = price * (1 - sig.stop_loss_pct)
            take_profit_pct = sig.stop_loss_pct * self._config.execution.take_profit_ratio
            take_profit_price = price * (1 + take_profit_pct)

            try:
                order_id = self._broker.submit_bracket_order(
                    sig.symbol, shares, stop_loss_price, take_profit_price,
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
                sig.symbol, OrderSide.BUY, shares, OrderStatus.PENDING, order_id,
                sig.strategy_name, sig.reason,
                stop_loss_price=stop_loss_price, take_profit_price=take_profit_price,
            )
            # Reduce buying power for subsequent signals
            buying_power -= shares * price
            logger.info(
                "BUY %s: shares=%d price=%.2f stop=%.2f tp=%.2f order=%s",
                sig.symbol, shares, price, stop_loss_price, take_profit_price, order_id,
            )

    def get_positions(self) -> list[Position]:
        """Return current tracked positions."""
        return list(self._positions.values())

    def get_equity(self) -> float:
        """Return current account equity from broker."""
        account = self._broker.get_account()
        return float(account.equity or 0)

    def save_snapshot(self) -> None:
        """Persist a PortfolioSnapshot to the database."""
        try:
            account = self._broker.get_account()
            equity = float(account.equity or 0)
            cash = float(account.cash or 0)
            daily_pnl = equity - float(account.last_equity or equity)

            snapshot = PortfolioSnapshot(
                timestamp_utc=datetime.now(UTC),
                equity=equity,
                cash=cash,
                positions_value=equity - cash,
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
                1 for sides in by_symbol_date.values()
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
                session.add(OrderLog(
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
                ))
                session.commit()
        except Exception:
            logger.exception("Failed to log order for %s", symbol)
