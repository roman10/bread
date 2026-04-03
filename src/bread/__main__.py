"""CLI entry point: python -m bread"""

from __future__ import annotations

from datetime import date

import typer

from bread.core.config import CONFIG_DIR, load_config
from bread.core.exceptions import BreadError
from bread.core.logging import setup_logging
from bread.data.alpaca_data import AlpacaDataProvider
from bread.data.cache import BarCache
from bread.data.indicators import compute_indicators, get_indicator_columns
from bread.db.database import get_engine, get_session_factory, init_db, resolve_db_path

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

    except BreadError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    app()
