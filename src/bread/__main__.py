"""CLI entry point: python -m bread"""

from __future__ import annotations

import logging
import math
from datetime import date

import typer

from bread.core.config import load_config
from bread.core.exceptions import BreadError
from bread.core.logging import setup_logging
from bread.data.alpaca_data import AlpacaDataProvider
from bread.data.cache import BarCache
from bread.data.indicators import compute_indicators, get_indicator_columns
from bread.db.database import get_engine, get_session_factory, init_db, resolve_db_path

logger = logging.getLogger(__name__)

app = typer.Typer(name="bread", add_completion=False)
db_app = typer.Typer(name="db", help="Database commands")
app.add_typer(db_app)


@db_app.command("init")
def db_init() -> None:
    """Create the database and all tables."""
    try:
        config = load_config()
        setup_logging(config.app.log_level)
        engine = get_engine(config.db.path)
        try:
            init_db(engine)
            resolved = resolve_db_path(config.db.path)
            typer.echo(f"Initialized database at {resolved}")
        finally:
            engine.dispose()
    except BreadError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc


@app.command("fetch")
def fetch(symbol: str) -> None:
    """Fetch daily bars, cache them, compute indicators, and print a summary."""
    try:
        # 1. Load config
        config = load_config()

        # 2. Initialize logging
        setup_logging(config.app.log_level)

        # 3. Auto-init DB
        engine = get_engine(config.db.path)
        try:
            init_db(engine)
            session_factory = get_session_factory(engine)

            # 4. Fetch and cache raw bars
            provider = AlpacaDataProvider(config)
            with session_factory() as session:
                cache = BarCache(session, provider, config)
                bars_df = cache.get_bars(symbol)

                # 5. Compute indicators
                enriched = compute_indicators(bars_df, config.indicators)

                # 6. Print summary
                symbol_upper = symbol.upper()
                bar_count = len(enriched)
                start_date = enriched.index.min().strftime("%Y-%m-%d")
                end_date = enriched.index.max().strftime("%Y-%m-%d")
                indicator_count = len(get_indicator_columns(config.indicators))
                typer.echo(
                    f"SYMBOL={symbol_upper} bars={bar_count} "
                    f"start={start_date} end={end_date} "
                    f"indicators={indicator_count}"
                )
        finally:
            engine.dispose()
    except BreadError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc


@app.command("backtest")
def backtest_cmd(
    strategy: str = typer.Option(..., "--strategy", help="Strategy name"),
    start: str = typer.Option(..., "--start", help="Start date YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="End date YYYY-MM-DD"),
) -> None:
    """Run a backtest for a strategy over a date range."""
    try:
        config = load_config()
        setup_logging(config.app.log_level)

        engine = get_engine(config.db.path)
        try:
            init_db(engine)
            session_factory = get_session_factory(engine)

            from bread.backtest.runner import run_strategy_backtest
            from bread.data.universe import UNIVERSE_CACHE_DIR, UniverseRegistry

            provider = AlpacaDataProvider(config)
            universe_registry = UniverseRegistry(
                config.universe_providers, UNIVERSE_CACHE_DIR
            )
            start_date = date.fromisoformat(start)
            end_date = date.fromisoformat(end)

            result = run_strategy_backtest(
                strategy,
                start_date,
                end_date,
                config=config,
                session_factory=session_factory,
                provider=provider,
                universe_registry=universe_registry,
            )

            m = result.metrics
            typer.echo(f"Backtest: {strategy} | {start} to {end}")
            typer.echo("---")
            typer.echo(f"Total return:    {m['total_return_pct']:>8.2f}%")
            typer.echo(f"CAGR:            {m['cagr_pct']:>8.2f}%")
            typer.echo(f"Sharpe ratio:    {m['sharpe_ratio']:>8.2f}")
            typer.echo(f"Sortino ratio:   {m['sortino_ratio']:>8.2f}")
            typer.echo(f"Max drawdown:    {m['max_drawdown_pct']:>8.2f}%")
            typer.echo(f"Win rate:        {m['win_rate_pct']:>8.2f}%")
            typer.echo(f"Profit factor:   {m['profit_factor']:>8.2f}")
            typer.echo(f"Total trades:    {m['total_trades']:>8d}")
            typer.echo(f"Avg holding days:{m['avg_holding_days']:>8.2f}")
        finally:
            engine.dispose()

    except BreadError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc


# Promotion thresholds from docs/design/strategy-lifecycle-automation.md:144-156.
# A strategy "passes the gate" when all five hold; gives an instant
# "which of these clear the bar" answer alongside the comparison table.
_GATE_MIN_SHARPE = 0.5
_GATE_MIN_PROFIT_FACTOR = 1.3
_GATE_MAX_DRAWDOWN_PCT = 12.0
_GATE_MIN_WIN_RATE_PCT = 40.0
_GATE_MIN_TRADES = 30


def _passes_promotion_gate(metrics: dict[str, float | int]) -> bool:
    return (
        float(metrics.get("sharpe_ratio", 0.0)) >= _GATE_MIN_SHARPE
        and float(metrics.get("profit_factor", 0.0)) >= _GATE_MIN_PROFIT_FACTOR
        and float(metrics.get("max_drawdown_pct", 0.0)) <= _GATE_MAX_DRAWDOWN_PCT
        and float(metrics.get("win_rate_pct", 0.0)) >= _GATE_MIN_WIN_RATE_PCT
        and int(metrics.get("total_trades", 0)) >= _GATE_MIN_TRADES
    )


def _format_pf(value: float) -> str:
    """Render profit factor with a sensible cap so the column stays narrow."""
    if math.isinf(value):
        return "  inf"
    return f"{value:>5.2f}"


@app.command("compare")
def compare_cmd(
    start: str = typer.Option(..., "--start", help="Start date YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="End date YYYY-MM-DD"),
    strategies: str | None = typer.Option(
        None,
        "--strategies",
        help=(
            "Comma-separated strategy names (e.g. 'etf_momentum,bb_mean_reversion'). "
            "Omit or pass 'all' to compare every enabled strategy from config."
        ),
    ),
) -> None:
    """Backtest multiple strategies and print a side-by-side leaderboard.

    Sorted by Sharpe ratio descending. The 'Gate' column flags strategies
    that clear the promotion thresholds documented in
    docs/design/strategy-lifecycle-automation.md (Sharpe>=0.5, PF>=1.3,
    max DD<=12%, win rate>=40%, trades>=30).
    """
    try:
        config = load_config()
        setup_logging(config.app.log_level)

        # Resolve strategy list — default to all enabled strategies in the
        # current mode, matching what the live tick loop would actually run.
        if strategies is None or strategies.strip().lower() == "all":
            names = [
                s.name
                for s in config.strategies
                if s.enabled and config.mode in s.modes
            ]
        else:
            names = [n.strip() for n in strategies.split(",") if n.strip()]
            available = {s.name for s in config.strategies if s.enabled}
            unknown = [n for n in names if n not in available]
            if unknown:
                typer.echo(
                    f"Error: unknown or disabled strategies: {unknown}. "
                    f"Enabled: {sorted(available)}",
                    err=True,
                )
                raise SystemExit(1)

        if not names:
            typer.echo("Error: no strategies to compare.", err=True)
            raise SystemExit(1)

        engine = get_engine(config.db.path)
        try:
            init_db(engine)
            session_factory = get_session_factory(engine)

            from bread.backtest.runner import run_strategy_backtest
            from bread.data.universe import UNIVERSE_CACHE_DIR, UniverseRegistry

            provider = AlpacaDataProvider(config)
            universe_registry = UniverseRegistry(
                config.universe_providers, UNIVERSE_CACHE_DIR
            )
            start_date = date.fromisoformat(start)
            end_date = date.fromisoformat(end)

            successes: list[tuple[str, dict[str, float | int]]] = []
            failures: list[tuple[str, str]] = []

            for name in names:
                typer.echo(f"  running {name}...", err=True)
                try:
                    result = run_strategy_backtest(
                        name,
                        start_date,
                        end_date,
                        config=config,
                        session_factory=session_factory,
                        provider=provider,
                        universe_registry=universe_registry,
                    )
                    successes.append((name, result.metrics))
                except BreadError as exc:
                    failures.append((name, str(exc)))
                except Exception as exc:  # noqa: BLE001
                    failures.append((name, f"{type(exc).__name__}: {exc}"))

            # Sort by Sharpe descending; ties broken by total return.
            successes.sort(
                key=lambda r: (
                    float(r[1].get("sharpe_ratio", 0.0)),
                    float(r[1].get("total_return_pct", 0.0)),
                ),
                reverse=True,
            )

            # Print header
            typer.echo("")
            typer.echo(f"Strategy comparison | {start} to {end}")
            typer.echo("=" * 100)
            header = (
                f"{'Strategy':<22} "
                f"{'Return%':>8} "
                f"{'CAGR%':>7} "
                f"{'Sharpe':>7} "
                f"{'Sortino':>8} "
                f"{'MaxDD%':>7} "
                f"{'Win%':>6} "
                f"{'PF':>5} "
                f"{'Trades':>7} "
                f"{'AvgHold':>8} "
                f"{'Gate':>5}"
            )
            typer.echo(header)
            typer.echo("-" * 100)

            for name, m in successes:
                gate = "PASS" if _passes_promotion_gate(m) else "FAIL"
                row = (
                    f"{name:<22} "
                    f"{float(m['total_return_pct']):>8.2f} "
                    f"{float(m['cagr_pct']):>7.2f} "
                    f"{float(m['sharpe_ratio']):>7.2f} "
                    f"{float(m['sortino_ratio']):>8.2f} "
                    f"{float(m['max_drawdown_pct']):>7.2f} "
                    f"{float(m['win_rate_pct']):>6.2f} "
                    f"{_format_pf(float(m['profit_factor']))} "
                    f"{int(m['total_trades']):>7d} "
                    f"{float(m['avg_holding_days']):>8.2f} "
                    f"{gate:>5}"
                )
                typer.echo(row)

            typer.echo("=" * 100)
            passing = sum(1 for _, m in successes if _passes_promotion_gate(m))
            typer.echo(
                f"{len(successes)} strategies compared, {passing} pass the promotion gate."
            )

            if failures:
                typer.echo("")
                typer.echo("Failures:")
                for name, err in failures:
                    typer.echo(f"  {name}: {err}", err=True)

            if not successes:
                raise SystemExit(1)
        finally:
            engine.dispose()

    except BreadError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc


def _start_dashboard_thread(port: int) -> None:
    """Start the dashboard in a background daemon thread if deps are available."""
    try:
        from bread.dashboard.app import create_app
    except ImportError:
        return  # dash deps not installed — silently skip

    import threading

    config = load_config()
    dash_app = create_app(config)

    def _serve() -> None:
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
        try:
            dash_app.run(host="0.0.0.0", port=port, debug=False)
        except OSError:
            logger.warning("Dashboard port %d in use — skipping auto-start", port)

    thread = threading.Thread(target=_serve, daemon=True, name="dashboard")
    thread.start()
    # Server binds 0.0.0.0 so Tailscale peers can reach it; the user
    # clicks localhost (or the VM's Tailscale IP remotely).
    typer.echo(f"Dashboard: http://localhost:{port}")


@app.command("run")
def run_cmd(
    mode: str = typer.Option("paper", "--mode", help="Trading mode: paper or live"),
    dashboard: bool = typer.Option(
        True, "--dashboard/--no-dashboard", help="Auto-start dashboard UI"
    ),
    dashboard_port: int = typer.Option(8050, "--dashboard-port", help="Dashboard port"),
) -> None:
    """Start the trading bot."""
    if mode not in ("paper", "live"):
        typer.echo(f"Error: mode must be 'paper' or 'live', got '{mode}'", err=True)
        raise SystemExit(1)
    import os

    os.environ["BREAD_MODE"] = mode
    if dashboard:
        _start_dashboard_thread(dashboard_port)
    try:
        from bread.app import run

        run(mode)
    except BreadError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc


@app.command("status")
def status_cmd() -> None:
    """Show account and position status."""
    try:
        config = load_config()
        setup_logging(config.app.log_level)

        from bread.execution.alpaca_broker import AlpacaBroker

        broker = AlpacaBroker(config)
        account = broker.get_account()
        positions = broker.get_positions()

        equity = float(account.equity or 0)
        cash = float(account.cash or 0)
        buying_power = float(account.buying_power or 0)
        last_equity = float(account.last_equity or equity)
        daily_pnl = equity - last_equity
        daily_pct = (daily_pnl / last_equity * 100) if last_equity > 0 else 0.0

        # Peak equity from DB
        from sqlalchemy import func, select

        from bread.db.models import PortfolioSnapshot

        engine_db = get_engine(config.db.path)
        init_db(engine_db)
        try:
            sf = get_session_factory(engine_db)
            with sf() as session:
                peak = session.execute(
                    select(func.max(PortfolioSnapshot.equity))
                ).scalar_one_or_none()
        finally:
            engine_db.dispose()

        peak = peak or equity
        drawdown_pct = ((peak - equity) / peak * 100) if peak > 0 else 0.0

        sign = "+" if daily_pnl >= 0 else ""
        typer.echo(
            f"Account: equity=${equity:,.2f}  cash=${cash:,.2f}  buying_power=${buying_power:,.2f}"
        )
        typer.echo(
            f"Today: P&L={sign}${daily_pnl:,.2f} ({sign}{daily_pct:.2f}%)  "
            f"Drawdown from peak: {drawdown_pct:.1f}%"
        )

        if positions:
            typer.echo(f"\nOpen Positions ({len(positions)}):")
            for pos in positions:
                sym = pos.symbol
                qty = int(float(pos.qty or 0))
                entry = float(pos.avg_entry_price or 0)
                current = float(pos.current_price or 0)
                unrealized = float(pos.unrealized_pl or 0)
                pct = float(pos.unrealized_plpc or 0) * 100
                sign_p = "+" if unrealized >= 0 else ""
                typer.echo(
                    f"  {sym:<5} qty={qty}  entry=${entry:,.2f}  "
                    f"current=${current:,.2f}  P&L={sign_p}${unrealized:,.2f} "
                    f"({sign_p}{pct:.1f}%)"
                )
        else:
            typer.echo("\nNo open positions")

        # Risk status
        try:
            typer.echo("\nRisk Status:")
            daily_limit = equity * config.risk.max_daily_loss_pct
            loss_amt = max(0.0, -daily_pnl)  # only show loss magnitude
            typer.echo(
                f"  Daily loss: ${loss_amt:,.2f} / "
                f"${daily_limit:,.2f} "
                f"({loss_amt / daily_limit * 100:.1f}% of limit)"
                if daily_limit > 0
                else f"  Daily loss: ${loss_amt:,.2f}"
            )

            dd_limit = config.risk.max_drawdown_pct * 100
            typer.echo(
                f"  Drawdown: {drawdown_pct:.1f}% / {dd_limit:.1f}% "
                f"({drawdown_pct / dd_limit * 100:.1f}% of limit)"
                if dd_limit > 0
                else f"  Drawdown: {drawdown_pct:.1f}%"
            )

            typer.echo(f"  Positions: {len(positions)} / {config.risk.max_positions}")
        except Exception:
            logger.debug("Failed to display risk status", exc_info=True)

        # Open orders
        try:
            open_orders = broker.get_orders(status="open")
            if open_orders:
                from bread.execution.alpaca_broker import (
                    normalize_alpaca_side,
                    normalize_alpaca_status,
                )
                typer.echo(f"\nOpen Orders ({len(open_orders)}):")
                for o in open_orders:
                    sym = str(o.symbol or "")
                    side = normalize_alpaca_side(o.side)
                    qty = int(float(o.qty or 0))
                    status = normalize_alpaca_status(o.status)
                    typer.echo(f"  {sym:<5} {side}  qty={qty}  status={status}")
            else:
                typer.echo("\nNo open orders")
        except Exception:
            logger.debug("Failed to display open orders", exc_info=True)

    except BreadError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc


@app.command("journal")
def journal_cmd(
    strategy: str = typer.Option(None, "--strategy", help="Filter by strategy name"),
    symbol: str = typer.Option(None, "--symbol", help="Filter by symbol"),
    days: int = typer.Option(30, "--days", help="Number of days to look back"),
) -> None:
    """Display trade journal entries."""
    try:
        config = load_config()
        setup_logging(config.app.log_level)

        from datetime import timedelta

        engine_db = get_engine(config.db.path)
        init_db(engine_db)
        try:
            sf = get_session_factory(engine_db)

            from bread.monitoring.journal import get_journal, get_journal_summary

            start_date = date.today() - timedelta(days=days)
            with sf() as session:
                entries = get_journal(
                    session,
                    start=start_date,
                    strategy=strategy,
                    symbol=symbol,
                )
                summary = get_journal_summary(entries)
        finally:
            engine_db.dispose()

        typer.echo(f"Trade Journal (last {days} days)")
        typer.echo("---")

        if entries:
            typer.echo(
                f"{'DATE':<11}{'SYMBOL':<8}{'QTY':>5}  "
                f"{'ENTRY':>9}  {'EXIT':>9}  {'P&L':>10}  "
                f"{'HOLD':>5}  {'STRATEGY':<15}{'REASON'}"
            )
            for e in entries:
                sign = "+" if e.pnl >= 0 else ""
                typer.echo(
                    f"{e.exit_date.isoformat():<11}{e.symbol:<8}{e.qty:>5}  "
                    f"${e.entry_price:>8,.2f}  ${e.exit_price:>8,.2f}  "
                    f"{sign}${e.pnl:>8,.2f}  "
                    f"{e.hold_days:>4}d  {e.strategy_name:<15}{e.exit_reason}"
                )

            total_trades = summary["total_trades"]
            win_rate = summary["win_rate_pct"]
            realized_pnl = summary["realized_pnl"]
            avg_hold = sum(e.hold_days for e in entries) / len(entries)
            sign = "+" if realized_pnl >= 0 else ""
            typer.echo(
                f"\nSummary: {total_trades} trades | "
                f"Win rate: {win_rate:.1f}% | "
                f"Realized P&L: {sign}${realized_pnl:,.2f} | "
                f"Avg hold: {avg_hold:.1f} days"
            )
        else:
            typer.echo("No completed trades in this period.")

    except BreadError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc


@app.command("dashboard")
def dashboard_cmd(
    port: int = typer.Option(8050, "--port", help="Dashboard port"),
    debug: bool = typer.Option(False, "--debug", help="Enable Dash debug mode"),
) -> None:
    """Launch the monitoring dashboard."""
    try:
        from bread.dashboard.app import create_app
    except ImportError:
        typer.echo(
            "Dashboard requires extra dependencies.\nInstall with: pip install bread[dashboard]",
            err=True,
        )
        raise SystemExit(1)

    try:
        config = load_config()
        setup_logging(config.app.log_level)
        dash_app = create_app(config)
        typer.echo(f"Starting dashboard at http://localhost:{port}")
        dash_app.run(host="127.0.0.1", port=port, debug=debug)
    except BreadError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc


@app.command("repair-orders")
def repair_orders_cmd(
    dry_run: bool = typer.Option(
        True, "--dry-run/--no-dry-run",
        help="Preview changes without writing (default: dry-run).",
    ),
    since: str = typer.Option(
        None, "--since",
        help="Only repair orders created on or after this date (YYYY-MM-DD).",
    ),
    batch_size: int = typer.Option(
        100, "--batch-size",
        help="Commit chunk size during the real run.",
    ),
) -> None:
    """Backfill filled_price / filled_at_utc for FILLED orders from Alpaca.

    Historical rows written before the status-normalization fix have
    status='FILLED' but no fill price (the fill-capture branch in
    _reconcile_orders was skipped due to the ORDERSTATUS.FILLED mismatch).
    This command re-fetches each such order from Alpaca by broker_order_id
    and populates raw_filled_price, filled_price (with paper-cost model),
    and filled_at_utc. Idempotent — already-repaired rows are skipped.
    """
    from datetime import UTC, datetime

    from sqlalchemy import select

    from bread.db.models import OrderLog
    from bread.execution.alpaca_broker import AlpacaBroker
    from bread.execution.engine import adjust_fill_price

    try:
        config = load_config()
        setup_logging(config.app.log_level)
        engine_db = get_engine(config.db.path)
        init_db(engine_db)
        sf = get_session_factory(engine_db)

        since_dt: datetime | None = None
        if since:
            try:
                since_dt = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError as exc:
                typer.echo(f"Error: --since must be YYYY-MM-DD, got {since!r}", err=True)
                raise SystemExit(1) from exc

        with sf() as session:
            stmt = select(OrderLog).where(
                OrderLog.status == "FILLED",
                OrderLog.filled_price.is_(None),
                OrderLog.broker_order_id.is_not(None),
            )
            if since_dt is not None:
                stmt = stmt.where(OrderLog.created_at_utc >= since_dt)
            stmt = stmt.order_by(OrderLog.created_at_utc.asc())
            targets = list(session.execute(stmt).scalars().all())

            typer.echo(f"Found {len(targets)} FILLED rows needing backfill.")
            if not targets:
                typer.echo("Nothing to do.")
                return

            broker = AlpacaBroker(config)

            repaired = 0
            missing_on_broker = 0
            no_fill_price = 0
            sample: list[str] = []
            committed_since_last = 0

            for idx, row in enumerate(targets, start=1):
                broker_order = broker.get_order_by_id(row.broker_order_id or "")
                if broker_order is None:
                    missing_on_broker += 1
                    logger.debug(
                        "Broker has no order for %s (id=%s)", row.symbol, row.broker_order_id
                    )
                    continue
                raw_value = getattr(broker_order, "filled_avg_price", None)
                if raw_value is None:
                    no_fill_price += 1
                    continue

                raw = float(raw_value)
                adjusted = adjust_fill_price(config, raw, row.side)
                row.raw_filled_price = raw
                row.filled_price = adjusted
                row.filled_at_utc = broker_order.filled_at
                repaired += 1

                if len(sample) < 10:
                    sample.append(
                        f"  {row.symbol:<5} {row.side}  qty={row.qty}"
                        f"  raw={raw:.2f}  adj={adjusted:.2f}  at={broker_order.filled_at}"
                    )

                if not dry_run:
                    committed_since_last += 1
                    if committed_since_last >= batch_size:
                        session.commit()
                        committed_since_last = 0
                        typer.echo(f"  … committed {idx}/{len(targets)}")

            if not dry_run and committed_since_last:
                session.commit()
            elif dry_run:
                session.rollback()

        typer.echo("\nSummary:")
        typer.echo(f"  repaired:          {repaired}")
        typer.echo(f"  missing_on_broker: {missing_on_broker}")
        typer.echo(f"  no_fill_price:     {no_fill_price}")
        if sample:
            typer.echo("\nSample updates:")
            for line in sample:
                typer.echo(line)
        if dry_run:
            typer.echo("\nDry run — no changes committed. Re-run with --no-dry-run to apply.")
        else:
            typer.echo("\nDone.")
    except BreadError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    app()
