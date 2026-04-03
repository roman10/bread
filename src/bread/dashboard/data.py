"""Read-only data access layer for the dashboard."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from bread.db.database import get_engine, get_session_factory, init_db
from bread.db.models import PortfolioSnapshot
from bread.execution.alpaca_broker import AlpacaBroker
from bread.monitoring.journal import get_journal, get_journal_summary
from bread.monitoring.tracker import get_daily_summaries, get_drawdown_series, get_period_pnl

if TYPE_CHECKING:
    from bread.core.config import AppConfig
    from bread.monitoring.journal import JournalEntry
    from bread.monitoring.tracker import DailySummary

logger = logging.getLogger(__name__)


class DashboardData:
    """Single data access point for all dashboard queries.

    Wraps the existing monitoring module functions (DB) and AlpacaBroker (live API).
    Broker is optional — if unavailable, live data methods return empty defaults.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._db_engine = get_engine(config.db.path)
        init_db(self._db_engine)
        self._sf = get_session_factory(self._db_engine)

        # Broker is optional — dashboard works without API keys
        self._broker = None
        self._broker_available = False
        try:
            self._broker = AlpacaBroker(config)
            self._broker_available = True
        except Exception:
            logger.warning("Broker unavailable — live data will not be shown")

    @property
    def broker_available(self) -> bool:
        return self._broker_available

    @property
    def mode(self) -> str:
        return self._config.mode

    @property
    def strategy_names(self) -> list[str]:
        return [s.name for s in self._config.strategies]

    # ------------------------------------------------------------------
    # Live data (Alpaca API)
    # ------------------------------------------------------------------

    def get_account_summary(self) -> dict[str, float]:
        """Return account KPIs. Empty defaults if broker unavailable."""
        if self._broker is None:
            return {
                "equity": 0.0, "cash": 0.0, "buying_power": 0.0,
                "daily_pnl": 0.0, "daily_pct": 0.0, "drawdown_pct": 0.0,
            }
        try:
            account = self._broker.get_account()
            equity = float(account.equity or 0)
            cash = float(account.cash or 0)
            buying_power = float(account.buying_power or 0)
            last_equity = float(account.last_equity or equity)
            daily_pnl = equity - last_equity
            daily_pct = (daily_pnl / last_equity * 100) if last_equity > 0 else 0.0

            # Peak equity from DB for drawdown
            with self._sf() as session:
                peak = session.execute(
                    select(func.max(PortfolioSnapshot.equity))
                ).scalar_one_or_none()
            peak = peak or equity
            drawdown_pct = ((peak - equity) / peak * 100) if peak > 0 else 0.0

            return {
                "equity": equity,
                "cash": cash,
                "buying_power": buying_power,
                "daily_pnl": daily_pnl,
                "daily_pct": daily_pct,
                "drawdown_pct": drawdown_pct,
            }
        except Exception:
            logger.exception("Failed to fetch account summary")
            return {
                "equity": 0.0, "cash": 0.0, "buying_power": 0.0,
                "daily_pnl": 0.0, "daily_pct": 0.0, "drawdown_pct": 0.0,
            }

    def get_positions(self) -> list[dict]:
        """Return open positions as dicts (ready for AG Grid)."""
        if self._broker is None:
            return []
        try:
            positions = self._broker.get_positions()
            return [
                {
                    "symbol": p.symbol,
                    "qty": int(float(p.qty or 0)),
                    "entry_price": float(p.avg_entry_price or 0),
                    "current_price": float(p.current_price or 0),
                    "unrealized_pnl": float(p.unrealized_pl or 0),
                    "unrealized_pct": float(p.unrealized_plpc or 0) * 100,
                    "market_value": float(p.market_value or 0),
                }
                for p in positions
            ]
        except Exception:
            logger.exception("Failed to fetch positions")
            return []

    def get_open_orders(self) -> list[dict]:
        """Return open orders as dicts."""
        if self._broker is None:
            return []
        try:
            orders = self._broker.get_orders(status="open")
            return [
                {
                    "symbol": o.symbol,
                    "side": str(o.side).upper(),
                    "qty": str(o.qty),
                    "status": str(o.status).upper(),
                    "type": str(o.type).upper() if o.type else "",
                    "submitted_at": str(o.submitted_at or ""),
                }
                for o in orders
            ]
        except Exception:
            logger.exception("Failed to fetch open orders")
            return []

    # ------------------------------------------------------------------
    # Historical data (SQLite DB)
    # ------------------------------------------------------------------

    def get_equity_curve(self, days: int = 90) -> list[DailySummary]:
        """Return daily summaries for equity curve chart."""
        start = date.today() - timedelta(days=days)
        with self._sf() as session:
            return get_daily_summaries(session, start=start)

    def get_drawdown_series(self) -> list[tuple[date, float]]:
        """Return (date, drawdown_pct) series."""
        with self._sf() as session:
            return get_drawdown_series(session)

    def get_period_pnl(self, period: str) -> list[tuple[str, float, float]]:
        """Return (label, pnl, pnl_pct) for the given period."""
        with self._sf() as session:
            return get_period_pnl(session, period=period)  # type: ignore[arg-type]

    def get_journal(
        self,
        *,
        start: date | None = None,
        end: date | None = None,
        strategy: str | None = None,
        symbol: str | None = None,
        limit: int = 200,
    ) -> list[JournalEntry]:
        """Return completed trades."""
        with self._sf() as session:
            return get_journal(
                session, start=start, end=end,
                strategy=strategy, symbol=symbol, limit=limit,
            )

    def get_journal_summary(self, entries: list[JournalEntry]) -> dict:
        """Compute summary stats from journal entries."""
        return get_journal_summary(entries)

    def dispose(self) -> None:
        """Clean up database engine."""
        self._db_engine.dispose()
