"""Risk context — batched DB queries that feed the risk evaluation pipeline."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from bread.core.models import OrderSide, OrderStatus
from bread.db.models import OrderLog, PortfolioSnapshot

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskContext:
    """Snapshot of portfolio state used to evaluate and size risk.

    Collects inputs from both the broker (equity, buying_power, daily_pnl)
    and the local DB (peak_equity, weekly_pnl, day_trade_count) into one
    immutable value object.
    """

    equity: float
    buying_power: float
    daily_pnl: float
    peak_equity: float
    weekly_pnl: float
    day_trade_count: int


class RiskContextRepo:
    """Fetches risk-evaluation inputs from the portfolio snapshot DB.

    Accepts broker-sourced values (equity, buying_power, daily_pnl) and
    queries the local DB for the remaining fields, returning a single
    RiskContext object.  All queries fail-open: on DB error the method
    returns a safe default so trading is not blocked.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def fetch(self, equity: float, buying_power: float, daily_pnl: float) -> RiskContext:
        """Return a fully-populated RiskContext for the current portfolio state."""
        return RiskContext(
            equity=equity,
            buying_power=buying_power,
            daily_pnl=daily_pnl,
            peak_equity=self._get_peak_equity(equity),
            weekly_pnl=self._get_weekly_pnl(equity),
            day_trade_count=self._get_day_trade_count(),
        )

    def _get_peak_equity(self, current_equity: float) -> float:
        """Get peak equity from portfolio_snapshots, defaulting to current."""
        try:
            with self._session_factory() as session:
                result = session.execute(
                    select(func.max(PortfolioSnapshot.equity))
                ).scalar_one_or_none()
                peak = result if result is not None else current_equity
                return max(peak, current_equity)
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
            cutoff = datetime.now(UTC) - timedelta(days=7)  # 7 calendar >= 5 trading days
            with self._session_factory() as session:
                rows = session.execute(
                    select(OrderLog.symbol, OrderLog.side, OrderLog.filled_at_utc).where(
                        OrderLog.status == OrderStatus.FILLED,
                        OrderLog.filled_at_utc >= cutoff,
                    )
                ).all()

            # Group by symbol+date, check for both BUY and SELL on the same day
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
