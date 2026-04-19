"""Trade journal command."""

from __future__ import annotations

from datetime import date, timedelta

import typer

from bread.cli._app import app
from bread.core.config import load_config
from bread.core.exceptions import BreadError
from bread.core.logging import setup_logging
from bread.db.database import get_engine, get_session_factory, init_db


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
