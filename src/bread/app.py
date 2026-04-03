"""Application orchestrator — scheduler and tick cycle."""

from __future__ import annotations

import logging
import signal
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from bread.core.config import CONFIG_DIR, AppConfig, load_config
from bread.core.logging import setup_logging
from bread.core.models import Signal
from bread.data.alpaca_data import AlpacaDataProvider
from bread.data.cache import BarCache
from bread.data.indicators import compute_indicators
from bread.db.database import get_engine, get_session_factory, init_db
from bread.execution.alpaca_broker import AlpacaBroker
from bread.execution.engine import ExecutionEngine
from bread.risk.manager import RiskManager
from bread.strategy.base import Strategy

logger = logging.getLogger(__name__)

# Module-level state set during run()
_engine: ExecutionEngine | None = None
_scheduler: BlockingScheduler | None = None
_strategies: list[Strategy] = []
_provider: AlpacaDataProvider | None = None
_config: AppConfig | None = None
_session_factory = None


def tick() -> None:
    """Single tick of the trading loop."""
    if _engine is None or _config is None or _provider is None or _session_factory is None:
        logger.error("tick() called before run() — module not initialized")
        return

    logger.info("Tick started")
    try:
        # 1. Reconcile: sync local positions with broker
        _engine.reconcile()

        # 2. Snapshot: record portfolio state
        _engine.save_snapshot()

        # 3. Refresh data + evaluate strategies
        all_signals: list[Signal] = []
        prices: dict[str, float] = {}

        for strategy in _strategies:
            with _session_factory() as session:
                cache = BarCache(session, _provider, _config)
                universe_data = {}
                for symbol in strategy.universe:
                    try:
                        bars = cache.get_bars(symbol)
                        enriched = compute_indicators(bars, _config.indicators)
                        universe_data[symbol] = enriched
                        prices[symbol] = float(enriched.iloc[-1]["close"])
                    except Exception:
                        logger.exception("Failed to load data for %s", symbol)

            # 4. Evaluate strategy
            try:
                signals = strategy.evaluate(universe_data)
                all_signals.extend(signals)
            except Exception:
                logger.exception("Strategy %s evaluation failed", strategy.name)

        # 5. Execute signals
        _engine.process_signals(all_signals, prices)

        logger.info(
            "Tick complete: signals=%d positions=%d",
            len(all_signals), len(_engine.get_positions()),
        )
    except Exception:
        logger.exception("Tick failed")


def _shutdown(signum: int, _frame: object) -> None:
    """Graceful shutdown handler."""
    logger.info("Shutdown signal received (signal=%d)", signum)
    if _scheduler is not None:
        _scheduler.shutdown(wait=True)


def run(mode: str) -> None:
    """Start the trading bot."""
    global _engine, _scheduler, _strategies, _provider, _config, _session_factory

    # 1. Load config — CLI mode overrides env/yaml
    import os

    os.environ["BREAD_MODE"] = mode
    _config = load_config()
    setup_logging(_config.app.log_level)

    # 2. Live mode confirmation — BEFORE any broker interaction
    if _config.mode == "live":
        confirm = input(
            "WARNING: LIVE TRADING MODE — real money at risk\n"
            'Type "CONFIRM" to proceed: '
        )
        if confirm.strip() != "CONFIRM":
            logger.info("Live mode not confirmed, exiting")
            sys.exit(0)

    # 3. Auto-init DB
    db_engine = get_engine(_config.db.path)
    init_db(db_engine)
    _session_factory = get_session_factory(db_engine)

    # 4. Initialize data provider and broker
    _provider = AlpacaDataProvider(_config)
    broker = AlpacaBroker(_config)

    # 5. Initialize risk manager
    risk = RiskManager(_config.risk)

    # 6. Initialize execution engine
    _engine = ExecutionEngine(broker, risk, _config, _session_factory)

    # 7. Load strategies
    import bread.strategy  # noqa: F401
    from bread.strategy.registry import get_strategy

    _strategies = []
    for s in _config.strategies:
        cls = get_strategy(s.name)
        inst = cls(CONFIG_DIR / s.config_path, _config.indicators)  # type: ignore[call-arg]
        _strategies.append(inst)

    logger.info("Loaded %d strategies: %s", len(_strategies), [s.name for s in _strategies])

    # 8. Initial reconciliation
    _engine.reconcile()

    # 9. Configure scheduler
    _scheduler = BlockingScheduler()
    _scheduler.add_job(
        tick,
        CronTrigger(
            day_of_week="mon-fri",
            hour="9-15",
            minute=f"*/{_config.execution.tick_interval_minutes}",
            timezone="America/New_York",
        ),
        id="trading_tick",
    )

    # 10. Signal handlers
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # 11. Start
    tick_min = _config.execution.tick_interval_minutes
    logger.info("Starting bread in %s mode (tick every %d min)", mode, tick_min)
    try:
        _scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")
    finally:
        db_engine.dispose()
