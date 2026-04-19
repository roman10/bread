"""Read-only data access layer for the dashboard."""

from __future__ import annotations

import logging
import math
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import yaml
from sqlalchemy import func, select

from bread.core.config import CONFIG_DIR
from bread.dashboard.components import format_local_dt
from bread.data.cache import is_market_open
from bread.db.database import get_engine, get_session_factory, init_db
from bread.db.models import EventAlertLog, OrderLog, PortfolioSnapshot, SignalLog
from bread.execution.alpaca_broker import (
    AlpacaBroker,
    normalize_alpaca_side,
    normalize_alpaca_status,
)
from bread.monitoring.journal import (
    get_all_strategies_summary,
    get_journal,
    get_journal_summary,
    get_open_positions,
)
from bread.monitoring.tracker import get_daily_summaries, get_drawdown_series, get_period_pnl

if TYPE_CHECKING:
    from bread.core.config import AppConfig
    from bread.monitoring.journal import JournalEntry, OpenPosition
    from bread.monitoring.tracker import DailySummary
    from bread.reset import ResetReport

logger = logging.getLogger(__name__)

_PRICE_CACHE_TTL = timedelta(seconds=15)


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

        # Short-TTL price cache. Both /strategies and /trades callbacks ask for
        # current prices on every refresh; caching dedups to one broker call
        # per 15s, which is tight enough to stay fresh inside the 30s market-
        # hours refresh window and slack enough to eat a multi-tab scenario.
        self._price_cache: dict[str, float] = {}
        self._price_cache_at: datetime | None = None

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
                "equity": 0.0,
                "cash": 0.0,
                "buying_power": 0.0,
                "daily_pnl": 0.0,
                "daily_pct": 0.0,
                "drawdown_pct": 0.0,
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
                "equity": 0.0,
                "cash": 0.0,
                "buying_power": 0.0,
                "daily_pnl": 0.0,
                "daily_pct": 0.0,
                "drawdown_pct": 0.0,
            }

    def get_current_prices(self) -> dict[str, float]:
        """Return {symbol: current_price} from broker.get_positions, cached briefly.

        Returns {} if broker is unavailable or the API call fails. Other
        symbols (unmatched BUYs without a broker-side position) simply won't
        appear and are treated as reconcile gaps by callers.
        """
        now = datetime.now(UTC)
        if (
            self._price_cache_at is not None
            and now - self._price_cache_at < _PRICE_CACHE_TTL
        ):
            return self._price_cache
        if self._broker is None:
            return {}
        try:
            positions = self._broker.get_positions()
            self._price_cache = {
                p.symbol: float(p.current_price or 0) for p in positions
            }
            self._price_cache_at = now
        except Exception:
            logger.exception("Failed to fetch positions for price cache")
        return self._price_cache

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
                    "side": normalize_alpaca_side(o.side),
                    "qty": str(o.qty),
                    "status": normalize_alpaca_status(o.status),
                    "type": str(getattr(o.type, "value", o.type)).upper() if o.type else "",
                    "submitted_at": format_local_dt(o.submitted_at, fmt="%Y-%m-%d %-I:%M %p %Z"),
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
        limit: int = 10_000,
    ) -> list[JournalEntry]:
        """Return completed trades."""
        with self._sf() as session:
            return get_journal(
                session,
                start=start,
                end=end,
                strategy=strategy,
                symbol=symbol,
                limit=limit,
            )

    def get_journal_summary(self, entries: list[JournalEntry]) -> dict:
        """Compute summary stats from journal entries."""
        return get_journal_summary(entries)

    def get_strategy_leaderboard(self, days: int = 365) -> list[dict]:
        """Return per-strategy P&L rows for the AG Grid leaderboard.

        Profit factor of `inf` (a strategy with zero losses) is sent as `None`
        because JSON cannot represent infinity; the column formatter renders
        it as the unicode infinity glyph.
        """
        prices = self.get_current_prices()
        with self._sf() as session:
            summaries = get_all_strategies_summary(
                session, days=days, current_prices=prices,
            )

        return [
            {
                "strategy_name": s.strategy_name,
                "total_trades": s.total_trades,
                "win_rate_pct": s.win_rate_pct,
                "realized_pnl": s.realized_pnl,
                "unrealized_pnl": s.unrealized_pnl,
                "total_pnl": s.total_pnl,
                "open_positions": s.open_positions,
                "expectancy": s.expectancy,
                "profit_factor": (
                    None if math.isinf(s.profit_factor) else s.profit_factor
                ),
                "best_trade": s.best_trade,
                "worst_trade": s.worst_trade,
                "avg_hold_days": s.avg_hold_days,
            }
            for s in summaries
        ]

    def get_open_positions(
        self,
        *,
        strategy: str | None = None,
        symbol: str | None = None,
    ) -> list[OpenPosition]:
        """Return OpenPosition rows (one per unmatched BUY), optionally filtered."""
        prices = self.get_current_prices()
        with self._sf() as session:
            opens = get_open_positions(session, prices)
        if strategy is not None:
            opens = [p for p in opens if p.strategy_name == strategy]
        if symbol is not None:
            opens = [p for p in opens if p.symbol == symbol]
        return opens

    # ------------------------------------------------------------------
    # Bot activity & strategy status
    # ------------------------------------------------------------------

    def get_bot_activity(self) -> dict[str, object]:
        """Return bot activity metrics for the dashboard."""
        now_utc = datetime.now(UTC)
        et = ZoneInfo("America/New_York")
        now_et = now_utc.astimezone(et)
        today_start_utc = now_et.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        ).astimezone(UTC)

        with self._sf() as session:
            last_tick = session.execute(
                select(func.max(PortfolioSnapshot.timestamp_utc))
            ).scalar_one_or_none()
            if last_tick is not None and last_tick.tzinfo is None:
                last_tick = last_tick.replace(tzinfo=UTC)

            ticks_today: int = (
                session.execute(
                    select(func.count(PortfolioSnapshot.id)).where(
                        PortfolioSnapshot.timestamp_utc >= today_start_utc
                    )
                ).scalar_one()
                or 0
            )

            signals_today: int = (
                session.execute(
                    select(func.count(SignalLog.id)).where(
                        SignalLog.signal_timestamp >= today_start_utc
                    )
                ).scalar_one()
                or 0
            )

            trades_today: int = (
                session.execute(
                    select(func.count(OrderLog.id)).where(
                        OrderLog.created_at_utc >= today_start_utc,
                        OrderLog.status != "REJECTED",
                    )
                ).scalar_one()
                or 0
            )

        # Determine bot status
        is_market_hours = is_market_open(now_et)

        if last_tick is None:
            status, status_color = "No Data", "secondary"
        elif is_market_hours:
            minutes_since = (now_utc - last_tick).total_seconds() / 60
            if minutes_since <= 20:
                status, status_color = "Running", "success"
            else:
                status, status_color = "Stale", "danger"
        else:
            status, status_color = "Idle", "warning"

        # Market status and next transition
        if is_market_hours:
            market_status, market_status_color = "Open", "success"
            close_et = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
            remaining = close_et - now_et
            hours, remainder = divmod(int(remaining.total_seconds()), 3600)
            mins = remainder // 60
            market_next = f"Closes in {hours}h {mins}m"
        else:
            market_status, market_status_color = "Closed", "secondary"
            # Find next market open
            next_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
            if now_et.weekday() < 5 and (
                now_et.hour < 9 or (now_et.hour == 9 and now_et.minute < 30)
            ):
                # Today before open
                day_label = "today"
            else:
                # After close or weekend — advance to next weekday
                next_open += timedelta(days=1)
                while next_open.weekday() >= 5:
                    next_open += timedelta(days=1)
                delta_days = (next_open.date() - now_et.date()).days
                if delta_days == 1:
                    day_label = "tomorrow"
                else:
                    day_label = next_open.strftime("%a")
            open_local = format_local_dt(next_open, fmt="%-I:%M %p %Z")
            market_next = f"Opens {day_label} {open_local}"

        return {
            "last_tick": last_tick,
            "ticks_today": ticks_today,
            "signals_today": signals_today,
            "trades_today": trades_today,
            "status": status,
            "status_color": status_color,
            "market_status": market_status,
            "market_status_color": market_status_color,
            "market_next": market_next,
        }

    def get_strategy_status(self) -> list[dict[str, object]]:
        """Return strategy configuration details for the dashboard."""
        result: list[dict[str, object]] = []
        for s in self._config.strategies:
            if not s.enabled:
                indicator, indicator_color = "disabled", "secondary"
            elif self._config.mode not in s.modes:
                indicator, indicator_color = "wrong-mode", "warning"
            else:
                indicator, indicator_color = "active", "success"

            # Load universe from strategy YAML config
            config_rel = s.config_path or f"strategies/{s.name}.yaml"
            config_path = CONFIG_DIR / config_rel
            universe: list[str] = []
            try:
                with open(config_path) as f:
                    cfg = yaml.safe_load(f)
                if isinstance(cfg, dict):
                    universe = cfg.get("universe", [])
            except Exception:
                logger.debug("Could not load config for %s", s.name)

            result.append(
                {
                    "name": s.name,
                    "enabled": s.enabled,
                    "modes": ", ".join(s.modes),
                    "weight": s.weight,
                    "universe": ", ".join(universe),
                    "status": indicator,
                    "status_color": indicator_color,
                }
            )
        return result

    def get_recent_signals(
        self,
        hours: int = 24,
        strategy: str | None = None,
    ) -> list[dict[str, object]]:
        """Return recent signals from the signal log."""
        cutoff = datetime.now(UTC) - timedelta(hours=hours)

        with self._sf() as session:
            query = (
                select(SignalLog)
                .where(SignalLog.signal_timestamp >= cutoff)
                .order_by(SignalLog.signal_timestamp.desc())
            )
            if strategy:
                query = query.where(SignalLog.strategy_name == strategy)

            rows = session.execute(query).scalars().all()

        return [
            {
                "time": format_local_dt(r.signal_timestamp, fmt="%Y-%m-%d %-I:%M %p %Z"),
                "strategy": r.strategy_name,
                "symbol": r.symbol,
                "direction": r.direction,
                "strength": round(r.strength, 2),
                "stop_loss_pct": round(r.stop_loss_pct * 100, 1),
                "reason": r.reason,
            }
            for r in rows
        ]

    def get_recent_events(self, hours: int = 48) -> list[dict[str, object]]:
        """Return recent event alerts for dashboard display."""
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        try:
            with self._sf() as session:
                rows = (
                    session.execute(
                        select(EventAlertLog)
                        .where(EventAlertLog.scanned_at_utc >= cutoff)
                        .order_by(EventAlertLog.scanned_at_utc.desc())
                    )
                    .scalars()
                    .all()
                )
            return [
                {
                    "time": format_local_dt(r.scanned_at_utc, fmt="%Y-%m-%d %-I:%M %p %Z"),
                    "symbol": r.symbol,
                    "severity": r.severity.upper(),
                    "headline": r.headline,
                    "event_type": r.event_type,
                    "details": r.details,
                }
                for r in rows
            ]
        except Exception:
            logger.exception("Failed to fetch recent events")
            return []

    def run_reset(self) -> ResetReport:
        """Soft-reset the paper env; preserves bar cache. Raises BreadError in live mode."""
        from bread.reset import reset_environment
        return reset_environment(self._config, self._broker, self._db_engine)

    def dispose(self) -> None:
        """Clean up database engine."""
        self._db_engine.dispose()
