"""Application orchestrator — TradingApp class and CLI entry point."""

from __future__ import annotations

import logging
import signal
import sys
from typing import TYPE_CHECKING

import pandas as pd
from apscheduler.events import EVENT_JOB_MISSED, JobExecutionEvent
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from bread.core.config import CONFIG_DIR, AppConfig, load_config
from bread.core.logging import setup_logging
from bread.core.models import Position, Signal, SignalDirection
from bread.data.alpaca_data import AlpacaDataProvider
from bread.data.cache import BarCache
from bread.data.indicators import compute_indicators
from bread.db.database import get_engine, get_session_factory, init_db
from bread.db.models import SignalLog
from bread.execution.alpaca_broker import AlpacaBroker
from bread.execution.engine import ExecutionEngine
from bread.monitoring.alerts import AlertManager
from bread.risk.manager import RiskManager
from bread.strategy.base import Strategy

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session, sessionmaker

    from bread.ai.client import ClaudeClient

logger = logging.getLogger(__name__)


class TradingApp:
    """Owns all trading components and orchestrates the tick cycle.

    Construction is cheap — no I/O occurs until start() is called.
    This makes it straightforward to construct in tests with injected mocks.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        # Components populated by _initialize(); set here so type-checkers see them
        self._db_engine: Engine | None = None
        self._session_factory: sessionmaker[Session] | None = None
        self._provider: AlpacaDataProvider | None = None
        self._claude_client: ClaudeClient | None = None
        self._engine: ExecutionEngine | None = None
        self._strategies: list[Strategy] = []
        self._alert_manager: AlertManager | None = None
        self._scheduler: BlockingScheduler | None = None

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def _initialize(self) -> None:
        """Instantiate all components from config. Called once by start()."""
        cfg = self._config

        # 1. Database
        db_engine = get_engine(cfg.db.path)
        init_db(db_engine)
        self._db_engine = db_engine
        self._session_factory = get_session_factory(db_engine)

        # 2. Data provider + broker
        self._provider = AlpacaDataProvider(cfg)
        broker = AlpacaBroker(cfg)

        # 3. Risk manager
        risk = RiskManager(cfg.risk)

        # 4. Claude AI (optional)
        if cfg.claude.enabled:
            from bread.ai.client import ClaudeClient

            self._claude_client = ClaudeClient(cfg.claude, self._session_factory)
            logger.info("Claude AI signal review enabled (mode=%s)", cfg.claude.review_mode)

        # 5. Execution engine
        self._engine = ExecutionEngine(
            broker, risk, cfg, self._session_factory, claude_client=self._claude_client
        )

        # 6. Universe registry + strategies
        import bread.strategy  # noqa: F401 — registers all strategies via decorators
        from bread.data.universe import (
            UNIVERSE_CACHE_DIR,
            UniverseRegistry,
            resolve_strategy_universe,
        )
        from bread.strategy.base import load_strategy_config
        from bread.strategy.registry import get_strategy

        universe_registry = UniverseRegistry(cfg.universe_providers, UNIVERSE_CACHE_DIR)

        for s in cfg.strategies:
            if not s.enabled:
                logger.info("Strategy %s disabled, skipping", s.name)
                continue
            if cfg.mode not in s.modes:
                logger.info("Strategy %s not enabled for %s mode, skipping", s.name, cfg.mode)
                continue
            cls = get_strategy(s.name)
            cfg_path = s.config_path or f"strategies/{s.name}.yaml"
            strat_cfg = load_strategy_config(CONFIG_DIR / cfg_path)
            resolved_universe = resolve_strategy_universe(strat_cfg, universe_registry, s.name)

            extra_kwargs: dict[str, object] = {}
            if cls.accepts_claude_client:
                if not self._claude_client:
                    logger.warning(
                        "Strategy %s requires claude_client but Claude is disabled — skipping",
                        s.name,
                    )
                    continue
                extra_kwargs["claude_client"] = self._claude_client

            inst = cls(  # type: ignore[call-arg]
                CONFIG_DIR / cfg_path,
                cfg.indicators,
                universe=resolved_universe,
                **extra_kwargs,
            )
            self._strategies.append(inst)

        logger.info(
            "Loaded %d strategies: %s", len(self._strategies), [s.name for s in self._strategies]
        )

        _merge_provider_asset_classes(cfg, universe_registry)

        # 7. Alert manager
        self._alert_manager = AlertManager(cfg.alerts)

    def _configure_scheduler(self) -> None:
        """Register APScheduler jobs and event listeners."""
        cfg = self._config
        self._scheduler = BlockingScheduler()

        self._scheduler.add_job(
            self.tick,
            CronTrigger(
                day_of_week="mon-fri",
                hour="9-15",
                minute=f"*/{cfg.execution.tick_interval_minutes}",
                timezone="America/New_York",
            ),
            id="trading_tick",
            misfire_grace_time=900,
            coalesce=True,
        )
        self._scheduler.add_job(
            self._send_daily_summary,
            CronTrigger(
                day_of_week="mon-fri",
                hour=16,
                minute=5,
                timezone="America/New_York",
            ),
            id="daily_summary",
            misfire_grace_time=900,
            coalesce=True,
        )

        if self._claude_client and cfg.claude.research_enabled:
            from apscheduler.triggers.interval import IntervalTrigger

            self._scheduler.add_job(
                self._research_tick,
                IntervalTrigger(
                    hours=cfg.claude.research_interval_hours,
                    timezone="America/New_York",
                ),
                id="event_research",
                misfire_grace_time=3600,
                coalesce=True,
            )
            logger.info("Event research enabled (every %dh)", cfg.claude.research_interval_hours)

        self._scheduler.add_listener(self._on_job_missed, EVENT_JOB_MISSED)

    # ------------------------------------------------------------------
    # Tick cycle
    # ------------------------------------------------------------------

    def tick(self) -> None:
        """Single tick of the trading loop."""
        if self._engine is None or self._session_factory is None or self._provider is None:
            logger.error("tick() called before initialization")
            return

        logger.info("Tick started")
        try:
            # 1. Reconcile: sync local positions with broker
            self._engine.reconcile()

            # 2. Snapshot: record portfolio state
            self._engine.save_snapshot()

            # 3. Batch-fetch data for all strategy universes
            all_symbols: list[str] = list(
                dict.fromkeys(sym for s in self._strategies for sym in s.universe)
            )
            universe_data: dict[str, pd.DataFrame] = {}
            prices: dict[str, float] = {}

            with self._session_factory() as session:
                cache = BarCache(session, self._provider, self._config)
                try:
                    bars_map = cache.get_bars_batch(all_symbols)
                except Exception:
                    logger.exception("Batch data fetch failed")
                    bars_map = {}

                for symbol, bars in bars_map.items():
                    try:
                        enriched = compute_indicators(bars, self._config.indicators)
                        universe_data[symbol] = enriched
                        prices[symbol] = float(enriched.iloc[-1]["close"])
                    except Exception:
                        logger.exception("Failed to compute indicators for %s", symbol)

            # 4. Evaluate strategies and persist signals
            all_signals: list[Signal] = []
            # Build a per-strategy set of owned symbols once, so each strategy
            # only sees its own positions when deciding whether to emit a SELL.
            owned_by_strategy: dict[str, set[str]] = {}
            for pos in self._engine.get_positions():
                owned_by_strategy.setdefault(pos.strategy_name, set()).add(pos.symbol)

            for strategy in self._strategies:
                signals: list[Signal] = []
                try:
                    signals = strategy.evaluate(universe_data)
                    owned = owned_by_strategy.get(strategy.name, set())
                    signals = [
                        s for s in signals
                        if s.direction != SignalDirection.SELL or s.symbol in owned
                    ]
                    all_signals.extend(signals)
                except Exception:
                    logger.exception("Strategy %s evaluation failed", strategy.name)

                if signals:
                    self._log_signals(signals)

            # 5. Execute signals
            # Keys on (symbol, strategy_name) so two strategies holding the
            # same symbol don't mask each other's open/close events in the
            # notifier.
            positions_before = {
                (p.symbol, p.strategy_name) for p in self._engine.get_positions()
            }
            self._engine.process_signals(all_signals, prices)
            positions_after = {
                (p.symbol, p.strategy_name): p for p in self._engine.get_positions()
            }

            # 6. Send trade alerts
            self._notify_trades(all_signals, positions_before, positions_after, prices)

            logger.info(
                "Tick complete: signals=%d positions=%d",
                len(all_signals),
                len(self._engine.get_positions()),
            )
        except Exception:
            logger.exception("Tick failed")
            if self._alert_manager:
                import traceback

                self._alert_manager.notify_error(traceback.format_exc()[-500:])

    def _log_signals(self, signals: list[Signal]) -> None:
        """Persist signal records to the database."""
        assert self._session_factory is not None
        try:
            with self._session_factory() as session:
                for sig in signals:
                    session.add(
                        SignalLog(
                            strategy_name=sig.strategy_name,
                            symbol=sig.symbol,
                            direction=sig.direction,
                            strength=sig.strength,
                            stop_loss_pct=sig.stop_loss_pct,
                            reason=sig.reason,
                            signal_timestamp=sig.timestamp,
                        )
                    )
                session.commit()
        except Exception:
            logger.exception("Failed to log signals")

    def _notify_trades(
        self,
        signals: list[Signal],
        positions_before: set[tuple[str, str]],
        positions_after: dict[tuple[str, str], Position],
        prices: dict[str, float],
    ) -> None:
        """Send trade alerts for newly entered or closed positions per strategy."""
        if not self._alert_manager or not signals:
            return
        assert self._engine is not None
        for sig in signals:
            key = (sig.symbol, sig.strategy_name)
            is_new_buy = (
                sig.direction == SignalDirection.BUY
                and key in positions_after
                and key not in positions_before
            )
            is_closed_sell = (
                sig.direction == SignalDirection.SELL
                and key in positions_before
                and key not in positions_after
            )
            if is_new_buy:
                pos = positions_after[key]
                reason = sig.reason
                review = self._engine.get_last_review(sig.symbol)
                if review:
                    reason = f"{sig.reason} | AI: {review.reasoning[:150]}"
                self._alert_manager.notify_trade(
                    sig.symbol, "BUY", pos.qty, pos.entry_price, reason
                )
            elif is_closed_sell:
                self._alert_manager.notify_trade(
                    sig.symbol, "SELL", 0, prices.get(sig.symbol, 0.0), sig.reason
                )

    # ------------------------------------------------------------------
    # Scheduled tasks
    # ------------------------------------------------------------------

    def _send_daily_summary(self) -> None:
        """Send end-of-day P&L summary alert."""
        if self._alert_manager is None or self._engine is None or self._session_factory is None:
            return
        try:
            from datetime import date as _date

            from bread.monitoring.journal import get_journal

            account = self._engine.get_account()
            equity = account.equity
            last_equity = account.last_equity or equity
            daily_pnl = equity - last_equity
            daily_pct = (daily_pnl / last_equity * 100) if last_equity > 0 else 0.0

            today = _date.today()
            with self._session_factory() as session:
                entries = get_journal(session, start=today, end=today)
            wins = sum(1 for e in entries if e.pnl > 0)
            losses = len(entries) - wins

            self._alert_manager.notify_daily_summary(
                equity, daily_pnl, daily_pct, len(entries), wins, losses
            )
        except Exception:
            logger.exception("Failed to send daily summary")

    def _research_tick(self) -> None:
        """Scheduled research scan — search for market-moving events."""
        if not self._claude_client or self._engine is None or self._session_factory is None:
            return
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from bread.ai.research import run_research_scan

        now_et = datetime.now(ZoneInfo("America/New_York"))
        if now_et.weekday() >= 5:
            logger.debug("Research scan skipped: weekend")
            return
        if not (7 <= now_et.hour < 18):
            logger.debug("Research scan skipped: outside research hours")
            return

        held = [p.symbol for p in self._engine.get_positions()]
        watchlist = list(dict.fromkeys(sym for s in self._strategies for sym in s.universe))
        run_research_scan(
            self._claude_client,
            self._session_factory,
            held,
            watchlist,
            alert_manager=self._alert_manager,
        )

    # ------------------------------------------------------------------
    # Scheduler event handlers
    # ------------------------------------------------------------------

    def _on_job_missed(self, event: JobExecutionEvent) -> None:
        """Handle missed scheduler jobs — log and optionally run a recovery tick."""
        logger.warning(
            "Missed scheduled job: %s (scheduled=%s)",
            event.job_id,
            event.scheduled_run_time,
        )
        if event.job_id != "trading_tick":
            return

        from bread.data.cache import is_market_open

        if is_market_open() and self._scheduler is not None:
            logger.info("Market open — scheduling recovery tick")
            self._scheduler.add_job(self.tick, id="recovery_tick", replace_existing=True)

    def _shutdown(self, signum: int, _frame: object) -> None:
        """Graceful shutdown handler."""
        logger.info("Shutdown signal received (signal=%d)", signum)
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=True)

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Initialize components and start the scheduler loop."""
        # Live mode safety check — BEFORE any broker interaction
        if self._config.mode == "live":
            confirm = input(
                'WARNING: LIVE TRADING MODE — real money at risk\nType "CONFIRM" to proceed: '
            )
            if confirm.strip() != "CONFIRM":
                logger.info("Live mode not confirmed, exiting")
                sys.exit(0)

        self._initialize()

        assert self._engine is not None
        self._engine.reconcile()

        self._configure_scheduler()

        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

        tick_min = self._config.execution.tick_interval_minutes
        logger.info("Starting bread in %s mode (tick every %d min)", self._config.mode, tick_min)
        try:
            assert self._scheduler is not None
            self._scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("Scheduler stopped")
        finally:
            if self._db_engine is not None:
                self._db_engine.dispose()


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _merge_provider_asset_classes(config: AppConfig, registry: object) -> None:
    """Enrich config.risk.asset_classes with provider-sourced sector data."""
    from bread.data.universe import UniverseRegistry

    if not isinstance(registry, UniverseRegistry):
        return
    classified = {sym for members in config.risk.asset_classes.values() for sym in members}
    added = 0
    for provider in registry.all_providers():
        for sym, gics_sector in provider.get_asset_class_map().items():
            if sym in classified:
                continue
            asset_class = config.asset_class_mapping.get(gics_sector, "unclassified")
            config.risk.asset_classes.setdefault(asset_class, []).append(sym)
            classified.add(sym)
            added += 1
    if added:
        logger.info("Auto-classified %d symbols into asset classes", added)


def run(mode: str) -> None:
    """CLI entry point — create and start the trading app."""
    import os

    os.environ["BREAD_MODE"] = mode
    config = load_config()
    setup_logging(config.app.log_level)
    TradingApp(config).start()
