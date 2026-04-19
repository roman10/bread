"""Operational commands: db init, environment reset."""

from __future__ import annotations

import typer

from bread.cli._app import app, db_app
from bread.cli._helpers import apply_mode
from bread.core.config import load_config
from bread.core.exceptions import BreadError
from bread.core.logging import setup_logging
from bread.db.database import get_engine, init_db, resolve_db_path


@db_app.command("init")
def db_init(
    mode: str = typer.Option(
        None,
        "--mode",
        help="Trading mode: paper or live (defaults to BREAD_MODE env / config)",
    ),
) -> None:
    """Create the database and all tables."""
    apply_mode(mode)
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


@app.command("reset")
def reset_cmd(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
    skip_broker: bool = typer.Option(
        False,
        "--skip-broker",
        help="Skip Alpaca cancel_orders / close_all_positions calls.",
    ),
) -> None:
    """Reset the paper environment: cancel orders, close positions, clear local trade history.

    Preserves the market_data_cache (bars) to avoid re-downloading.
    Paper-only — refuses to run when mode=live. Alpaca's "Reset Account" button
    (which restores starting cash) is NOT API-exposed; the command prints the
    manual steps to complete a full reset.
    """
    try:
        config = load_config()
        setup_logging(config.app.log_level)

        if config.mode != "paper":
            typer.echo(
                "Error: reset is paper-only. Current mode is 'live'.",
                err=True,
            )
            raise SystemExit(1)

        if not yes:
            confirmed = typer.confirm(
                "Reset paper environment? This will cancel orders, close "
                "positions, and delete local trade history."
            )
            if not confirmed:
                typer.echo("Aborted.")
                raise SystemExit(0)

        from bread.execution.alpaca_broker import AlpacaBroker
        from bread.reset import reset_environment

        broker = None
        if not skip_broker:
            try:
                broker = AlpacaBroker(config)
            except BreadError as exc:
                typer.echo(
                    f"Warning: broker unavailable ({exc}); skipping broker-side reset.",
                    err=True,
                )

        engine = get_engine(config.db.path)
        try:
            init_db(engine)
            report = reset_environment(config, broker, engine)
        finally:
            engine.dispose()

        typer.echo("Reset complete.")
        typer.echo("---")
        typer.echo(f"Broker orders cancelled:  {report.broker_orders_cancelled}")
        typer.echo(f"Broker positions closed:  {report.broker_positions_closed}")
        typer.echo(f"Local orders deleted:     {report.orders_deleted}")
        typer.echo(f"Local signals deleted:    {report.signals_deleted}")
        typer.echo(f"Local snapshots deleted:  {report.snapshots_deleted}")
        typer.echo(f"Local alerts deleted:     {report.alerts_deleted}")
        typer.echo(f"Claude usage deleted:     {report.claude_usage_deleted}")
        typer.echo(f"Bars preserved in cache:  {report.bars_preserved}")
        typer.echo("")
        typer.echo(report.manual_instructions)
    except BreadError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc
