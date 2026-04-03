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


if __name__ == "__main__":
    app()
