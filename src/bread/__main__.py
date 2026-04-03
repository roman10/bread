"""CLI entry point: python -m bread"""

from __future__ import annotations

import logging
from datetime import date

import typer

from bread.core.config import CONFIG_DIR, load_config
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
        # 1. Load config
        config = load_config()

        # 2. Initialize logging
        setup_logging(config.app.log_level)

        # 3. Auto-init DB
        engine = get_engine(config.db.path)
        try:
            init_db(engine)
        finally:
            engine.dispose()

        # 4. Match strategy name
        strat_settings = None
        for s in config.strategies:
            if s.name == strategy:
                strat_settings = s
                break
        if strat_settings is None:
            available = [s.name for s in config.strategies]
            typer.echo(f"Error: Unknown strategy '{strategy}'. Available: {available}", err=True)
            raise SystemExit(1)

        # 5. Resolve config path
        strategy_config_path = CONFIG_DIR / strat_settings.config_path

        # 6. Look up strategy class from registry
        import bread.strategy  # noqa: F401
        from bread.strategy.registry import get_strategy

        strategy_cls = get_strategy(strategy)

        # 7. Instantiate strategy
        strat_instance = strategy_cls(strategy_config_path, config.indicators)  # type: ignore[call-arg]

        # 8. Create data feed, load universe
        from bread.backtest.data_feed import HistoricalDataFeed

        provider = AlpacaDataProvider(config)
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
        feed = HistoricalDataFeed(provider, config)
        universe_data = feed.load_universe(strat_instance.universe, start_date, end_date)

        # 9. Run backtest
        from bread.backtest.engine import BacktestEngine

        bt = BacktestEngine(strat_instance, config)
        result = bt.run(universe_data, start_date, end_date)

        # 10. Print metrics summary
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

    except BreadError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc


@app.command("run")
def run_cmd(
    mode: str = typer.Option("paper", "--mode", help="Trading mode: paper or live"),
) -> None:
    """Start the trading bot."""
    if mode not in ("paper", "live"):
        typer.echo(f"Error: mode must be 'paper' or 'live', got '{mode}'", err=True)
        raise SystemExit(1)
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
            f"Account: equity=${equity:,.2f}  cash=${cash:,.2f}  "
            f"buying_power=${buying_power:,.2f}"
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
                if daily_limit > 0 else f"  Daily loss: ${loss_amt:,.2f}"
            )

            dd_limit = config.risk.max_drawdown_pct * 100
            typer.echo(
                f"  Drawdown: {drawdown_pct:.1f}% / {dd_limit:.1f}% "
                f"({drawdown_pct / dd_limit * 100:.1f}% of limit)"
                if dd_limit > 0 else f"  Drawdown: {drawdown_pct:.1f}%"
            )

            typer.echo(
                f"  Positions: {len(positions)} / {config.risk.max_positions}"
            )
        except Exception:
            logger.debug("Failed to display risk status", exc_info=True)

        # Open orders
        try:
            open_orders = broker.get_orders(status="open")
            if open_orders:
                typer.echo(f"\nOpen Orders ({len(open_orders)}):")
                for o in open_orders:
                    sym = o.symbol
                    side = str(o.side).upper()
                    qty = o.qty
                    status = str(o.status).upper()
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
            total_pnl = summary["total_pnl"]
            avg_hold = sum(e.hold_days for e in entries) / len(entries)
            sign = "+" if total_pnl >= 0 else ""
            typer.echo(
                f"\nSummary: {total_trades} trades | "
                f"Win rate: {win_rate:.1f}% | "
                f"Total P&L: {sign}${total_pnl:,.2f} | "
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
            "Dashboard requires extra dependencies.\n"
            "Install with: pip install bread[dashboard]",
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


if __name__ == "__main__":
    app()
