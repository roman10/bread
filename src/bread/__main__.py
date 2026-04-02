"""CLI entry point: python -m bread"""

from __future__ import annotations

import typer

from bread.core.config import load_config
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
        init_db(engine)
        resolved = resolve_db_path(config.db.path)
        typer.echo(f"Initialized database at {resolved}")
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
    except BreadError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    app()
