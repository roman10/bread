"""Live/paper trading loop, account status, and dashboard launcher."""

from __future__ import annotations

import logging

import typer

from bread.cli._app import app
from bread.cli._helpers import start_dashboard_thread
from bread.core.config import load_config
from bread.core.exceptions import BreadError
from bread.core.logging import setup_logging
from bread.db.database import get_engine, get_session_factory, init_db

logger = logging.getLogger(__name__)


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
        start_dashboard_thread(dashboard_port)
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

        equity = account.equity
        cash = account.cash
        buying_power = account.buying_power
        last_equity = account.last_equity or equity
        daily_pnl = equity - last_equity
        daily_pct = (daily_pnl / last_equity * 100) if last_equity > 0 else 0.0

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
                sign_p = "+" if pos.unrealized_pl >= 0 else ""
                typer.echo(
                    f"  {pos.symbol:<5} qty={int(pos.qty)}  "
                    f"entry=${pos.avg_entry_price:,.2f}  "
                    f"current=${pos.current_price:,.2f}  "
                    f"P&L={sign_p}${pos.unrealized_pl:,.2f} "
                    f"({sign_p}{pos.unrealized_plpc * 100:.1f}%)"
                )
        else:
            typer.echo("\nNo open positions")

        try:
            typer.echo("\nRisk Status:")
            daily_limit = equity * config.risk.max_daily_loss_pct
            loss_amt = max(0.0, -daily_pnl)
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

        try:
            open_orders = broker.get_orders(status="open")
            if open_orders:
                typer.echo(f"\nOpen Orders ({len(open_orders)}):")
                for o in open_orders:
                    side = o.side.value if o.side else ""
                    status = o.status.value if o.status else "UNKNOWN"
                    typer.echo(f"  {o.symbol:<5} {side}  qty={int(o.qty)}  status={status}")
            else:
                typer.echo("\nNo open orders")
        except Exception:
            logger.debug("Failed to display open orders", exc_info=True)

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
