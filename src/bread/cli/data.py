"""Market data commands."""

from __future__ import annotations

import typer

from bread.cli._app import app
from bread.core.config import load_config
from bread.core.exceptions import BreadError
from bread.core.logging import setup_logging
from bread.data.alpaca_data import AlpacaDataProvider
from bread.data.cache import BarCache
from bread.data.indicators import compute_indicators, get_indicator_columns
from bread.db.database import get_engine, get_session_factory, init_db


@app.command("fetch")
def fetch(symbol: str) -> None:
    """Fetch daily bars, cache them, compute indicators, and print a summary."""
    try:
        config = load_config()
        setup_logging(config.app.log_level)

        engine = get_engine(config.db.path)
        try:
            init_db(engine)
            session_factory = get_session_factory(engine)

            provider = AlpacaDataProvider(config)
            with session_factory() as session:
                cache = BarCache(session, provider, config)
                bars_df = cache.get_bars(symbol)

                enriched = compute_indicators(bars_df, config.indicators)

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
