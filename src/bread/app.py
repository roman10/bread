"""Application orchestrator — scheduler and tick cycle."""

from __future__ import annotations

import logging
import signal
import sys

import pandas as pd
from apscheduler.events import EVENT_JOB_MISSED, JobExecutionEvent
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from bread.core.config import CONFIG_DIR, AppConfig, load_config
from bread.core.logging import setup_logging
from bread.core.models import Signal
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

logger = logging.getLogger(__name__)

# Module-level state set during run()
_engine: ExecutionEngine | None = None
_scheduler: BlockingScheduler | None = None
_strategies: list[Strategy] = []
_provider: AlpacaDataProvider | None = None
_config: AppConfig | None = None
_session_factory = None
_alert_manager: AlertManager | None = None


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

        # Collect all unique symbols across strategies and batch-fetch once
        all_symbols: list[str] = list(
            dict.fromkeys(sym for s in _strategies for sym in s.universe)
        )
        universe_data: dict[str, pd.DataFrame] = {}

        with _session_factory() as session:
            cache = BarCache(session, _provider, _config)
            try:
                bars_map = cache.get_bars_batch(all_symbols)
            except Exception:
                logger.exception("Batch data fetch failed")
                bars_map = {}

            for symbol, bars in bars_map.items():
                try:
                    enriched = compute_indicators(bars, _config.indicators)
                    universe_data[symbol] = enriched
                    prices[symbol] = float(enriched.iloc[-1]["close"])
                except Exception:
                    logger.exception("Failed to compute indicators for %s", symbol)

        for strategy in _strategies:
            # 4. Evaluate strategy
            signals: list[Signal] = []
            try:
                signals = strategy.evaluate(universe_data)
                all_signals.extend(signals)
            except Exception:
                logger.exception("Strategy %s evaluation failed", strategy.name)

            # 4b. Log signals to DB
            if signals:
                try:
                    with _session_factory() as session:
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

        # 5. Execute signals
        positions_before = {p.symbol for p in _engine.get_positions()}
        _engine.process_signals(all_signals, prices)
        positions_after = {p.symbol: p for p in _engine.get_positions()}

        # 6. Alert on executed trades (after execution, with actual qty/price)
        if _alert_manager and all_signals:
            for sig in all_signals:
                is_new_buy = (
                    sig.direction == "BUY"
                    and sig.symbol in positions_after
                    and sig.symbol not in positions_before
                )
                is_closed_sell = (
                    sig.direction == "SELL"
                    and sig.symbol in positions_before
                    and sig.symbol not in positions_after
                )
                if is_new_buy:
                    pos = positions_after[sig.symbol]
                    _alert_manager.notify_trade(
                        sig.symbol,
                        "BUY",
                        pos.qty,
                        pos.entry_price,
                        sig.reason,
                    )
                elif is_closed_sell:
                    _alert_manager.notify_trade(
                        sig.symbol,
                        "SELL",
                        0,
                        prices.get(sig.symbol, 0.0),
                        sig.reason,
                    )

        logger.info(
            "Tick complete: signals=%d positions=%d",
            len(all_signals),
            len(_engine.get_positions()),
        )
    except Exception:
        logger.exception("Tick failed")
        if _alert_manager:
            import traceback

            _alert_manager.notify_error(traceback.format_exc()[-500:])


def _on_job_missed(event: JobExecutionEvent) -> None:
    """Handle missed scheduler jobs — log and optionally run a recovery tick."""
    logger.warning(
        "Missed scheduled job: %s (scheduled=%s)",
        event.job_id,
        event.scheduled_run_time,
    )
    if event.job_id != "trading_tick":
        return

    from bread.data.cache import is_market_open

    if is_market_open() and _scheduler is not None:
        logger.info("Market open — scheduling recovery tick")
        _scheduler.add_job(tick, id="recovery_tick", replace_existing=True)


def _shutdown(signum: int, _frame: object) -> None:
    """Graceful shutdown handler."""
    logger.info("Shutdown signal received (signal=%d)", signum)
    if _scheduler is not None:
        _scheduler.shutdown(wait=True)


def _send_daily_summary() -> None:
    """Send end-of-day P&L summary alert."""
    if _alert_manager is None or _engine is None or _session_factory is None:
        return
    try:
        from datetime import date as _date

        from bread.monitoring.journal import get_journal

        account = _engine.get_account()
        equity = float(account.equity or 0)
        last_equity = float(account.last_equity or equity)
        daily_pnl = equity - last_equity
        daily_pct = (daily_pnl / last_equity * 100) if last_equity > 0 else 0.0

        today = _date.today()
        with _session_factory() as session:
            entries = get_journal(session, start=today, end=today)
        wins = sum(1 for e in entries if e.pnl > 0)
        losses = len(entries) - wins

        _alert_manager.notify_daily_summary(
            equity,
            daily_pnl,
            daily_pct,
            len(entries),
            wins,
            losses,
        )
    except Exception:
        logger.exception("Failed to send daily summary")


def run(mode: str) -> None:
    """Start the trading bot."""
    global _engine, _scheduler, _strategies, _provider, _config, _session_factory
    global _alert_manager

    # 1. Load config — CLI mode overrides env/yaml
    import os

    os.environ["BREAD_MODE"] = mode
    _config = load_config()
    setup_logging(_config.app.log_level)

    # 2. Live mode confirmation — BEFORE any broker interaction
    if _config.mode == "live":
        confirm = input(
            'WARNING: LIVE TRADING MODE — real money at risk\nType "CONFIRM" to proceed: '
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
        if not s.enabled:
            logger.info("Strategy %s disabled, skipping", s.name)
            continue
        if _config.mode not in s.modes:
            logger.info("Strategy %s not enabled for %s mode, skipping", s.name, _config.mode)
            continue
        cls = get_strategy(s.name)
        cfg_path = s.config_path or f"strategies/{s.name}.yaml"
        inst = cls(CONFIG_DIR / cfg_path, _config.indicators)  # type: ignore[call-arg]
        _strategies.append(inst)

    logger.info("Loaded %d strategies: %s", len(_strategies), [s.name for s in _strategies])

    # 8. Initialize alert manager
    _alert_manager = AlertManager(_config.alerts)

    # 9. Initial reconciliation
    _engine.reconcile()

    # 10. Configure scheduler
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
        misfire_grace_time=900,
        coalesce=True,
    )
    _scheduler.add_job(
        _send_daily_summary,
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
    _scheduler.add_listener(_on_job_missed, EVENT_JOB_MISSED)

    # 11. Signal handlers
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # 12. Start
    tick_min = _config.execution.tick_interval_minutes
    logger.info("Starting bread in %s mode (tick every %d min)", mode, tick_min)
    try:
        _scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")
    finally:
        db_engine.dispose()
