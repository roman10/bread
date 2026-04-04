"""Dashboard application factory."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import dash
import dash_bootstrap_components as dbc
from dash import ClientsideFunction, Input, Output, dcc, html

from bread.dashboard.data import DashboardData

if TYPE_CHECKING:
    from bread.core.config import AppConfig

# Market-hours refresh (30s) vs off-hours (5min), in milliseconds
_MARKET_INTERVAL_MS = 30_000
_OFF_INTERVAL_MS = 300_000


def _make_navbar(mode: str) -> dbc.Navbar:
    badge_color = "primary" if mode == "paper" else "danger"
    return dbc.Navbar(
        dbc.Container([
            dbc.NavbarBrand("bread", className="fw-bold"),
            dbc.Nav([
                dbc.NavItem(dbc.NavLink("Portfolio", href="/", active="exact")),
                dbc.NavItem(dbc.NavLink("Trades", href="/trades", active="exact")),
            ], navbar=True, className="me-auto"),
            dbc.Badge(mode.upper(), color=badge_color, className="me-2 fs-6"),
            html.Span(id="connection-dot"),
        ], fluid=True),
        color="dark",
        dark=True,
        className="mb-4",
    )


def create_app(config: AppConfig) -> dash.Dash:
    """Create and configure the Dash application."""
    data = DashboardData(config)

    app = dash.Dash(
        __name__,
        use_pages=True,
        pages_folder=str(Path(__file__).parent / "pages"),
        external_stylesheets=[dbc.themes.DARKLY],
        title="bread",
        update_title=None,
        suppress_callback_exceptions=True,
    )

    # Store DashboardData on the Flask server for callback access
    app.server.config["data"] = data

    app.layout = dbc.Container([
        _make_navbar(config.mode),
        # Smart refresh interval — adjusted by clientside callback
        dcc.Interval(
            id="refresh-interval",
            interval=_MARKET_INTERVAL_MS,
            n_intervals=0,
        ),
        # Hidden stores for interval constants
        dcc.Store(id="market-interval", data=_MARKET_INTERVAL_MS),
        dcc.Store(id="off-interval", data=_OFF_INTERVAL_MS),
        dash.page_container,
    ], fluid=True, className="pb-4")

    # Clientside callback for smart interval timing
    # Checks if current time is within ET market hours (Mon-Fri 9:30-16:00)
    app.clientside_callback(
        ClientsideFunction(namespace="bread", function_name="smartInterval"),
        Output("refresh-interval", "interval"),
        Input("refresh-interval", "n_intervals"),
        Input("market-interval", "data"),
        Input("off-interval", "data"),
    )

    # Connection status dot (reflects broker availability at startup)
    @app.callback(
        Output("connection-dot", "children"),
        Input("refresh-interval", "n_intervals"),
    )
    def _update_connection_dot(_n: int) -> html.Span:
        d = app.server.config["data"]
        if d.broker_available:
            return html.Span("\u25cf Connected", style={"color": "#00bc8c"})
        return html.Span("\u25cf API unavailable", style={"color": "#f39c12"})

    # Warm up: trigger Dash's lazy _setup_server so the callback map is fully
    # populated before the threaded server starts accepting real requests.
    # Without this, concurrent GET + POST on first page load race past the
    # one-time init flag, causing "Callback not found" KeyErrors.
    with app.server.test_client() as client:
        client.get("/")

    return app
