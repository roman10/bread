"""Dashboard application factory."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import dash
import dash_bootstrap_components as dbc
from dash import ClientsideFunction, Input, Output, State, dcc, html

from bread.dashboard.data import DashboardData
from bread.reset import ALPACA_PAPER_DASHBOARD_URL, ResetReport

if TYPE_CHECKING:
    from bread.core.config import AppConfig

# Market-hours refresh (30s) vs off-hours (5min), in milliseconds
_MARKET_INTERVAL_MS = 30_000
_OFF_INTERVAL_MS = 300_000


def _make_navbar(mode: str) -> dbc.Navbar:
    badge_color = "primary" if mode == "paper" else "danger"
    # Reset is paper-only. In live mode the button is simply omitted — reset
    # has no business touching a live account, and the server-side callback
    # also raises if anyone forges the click.
    right_group: list[object] = [
        dbc.Badge(mode.upper(), color=badge_color, className="me-2 fs-6"),
        html.Span(id="connection-dot", className="me-2"),
    ]
    if mode == "paper":
        right_group.append(
            dbc.Button(
                "Reset",
                id="open-reset-modal",
                color="warning",
                size="sm",
                outline=True,
                n_clicks=0,
            )
        )
    return dbc.Navbar(
        dbc.Container([
            dbc.NavbarBrand("bread", className="fw-bold"),
            dbc.Nav([
                dbc.NavItem(dbc.NavLink("Portfolio", href="/", active="exact")),
                dbc.NavItem(dbc.NavLink("Strategies", href="/strategies", active="exact")),
                dbc.NavItem(dbc.NavLink("Trades", href="/trades", active="exact")),
            ], navbar=True, className="me-auto"),
            *right_group,
        ], fluid=True),
        color="dark",
        dark=True,
        className="mb-4",
    )


def _reset_preview_body() -> list[object]:
    """Modal body content shown before the user clicks 'Proceed'."""
    return [
        dbc.Alert(
            "This will reset the paper environment for clean testing.",
            color="warning",
            className="mb-3",
        ),
        html.H6("Will be cleared", className="fw-bold"),
        html.Ul([
            html.Li("Open Alpaca orders — cancelled via API"),
            html.Li("Open Alpaca positions — closed via API"),
            html.Li("Local order log, signals, portfolio snapshots"),
            html.Li("Event alerts and Claude usage log"),
        ]),
        html.H6("Will be preserved", className="fw-bold"),
        html.Ul([html.Li("Market data (OHLCV bar cache)")]),
        html.Hr(),
        html.H6("Manual step (cannot be automated)", className="fw-bold"),
        html.P([
            "Alpaca's account reset (restore starting cash) is not exposed "
            "via API. After this runs, open the ",
            html.A(
                "Alpaca paper dashboard",
                href=ALPACA_PAPER_DASHBOARD_URL,
                target="_blank",
                rel="noopener noreferrer",
            ),
            " and click Account → \"Reset Account\" to restore your starting balance.",
        ]),
    ]


def _reset_result_body(report: ResetReport) -> list[object]:
    """Modal body content shown after the reset completes."""
    rows = [
        ("Broker orders cancelled", report.broker_orders_cancelled),
        ("Broker positions closed", report.broker_positions_closed),
        ("Local orders deleted", report.orders_deleted),
        ("Local signals deleted", report.signals_deleted),
        ("Local snapshots deleted", report.snapshots_deleted),
        ("Local alerts deleted", report.alerts_deleted),
        ("Claude usage deleted", report.claude_usage_deleted),
        ("Bars preserved in cache", report.bars_preserved),
    ]
    return [
        dbc.Alert("Reset complete.", color="success", className="mb-3"),
        dbc.Table(
            [
                html.Tbody([
                    html.Tr([html.Td(label), html.Td(str(value))])
                    for label, value in rows
                ])
            ],
            bordered=False,
            size="sm",
            className="mb-3",
        ),
        html.H6("Finish the reset", className="fw-bold"),
        html.P([
            "To restore starting cash, open the ",
            html.A(
                "Alpaca paper dashboard",
                href=ALPACA_PAPER_DASHBOARD_URL,
                target="_blank",
                rel="noopener noreferrer",
            ),
            " and click Account → \"Reset Account\".",
        ]),
    ]


def _reset_error_body(message: str) -> list[object]:
    return [dbc.Alert(f"Reset failed: {message}", color="danger")]


def _make_reset_modal() -> dbc.Modal:
    return dbc.Modal(
        [
            dbc.ModalHeader(dbc.ModalTitle("Reset Paper Environment")),
            dbc.ModalBody(_reset_preview_body(), id="reset-modal-body"),
            dbc.ModalFooter([
                dbc.Button(
                    "Cancel",
                    id="reset-cancel-btn",
                    color="secondary",
                    outline=True,
                    n_clicks=0,
                ),
                dbc.Button(
                    "Proceed",
                    id="reset-proceed-btn",
                    color="danger",
                    n_clicks=0,
                ),
            ]),
        ],
        id="reset-modal",
        is_open=False,
        backdrop="static",
        size="lg",
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

    layout_children: list[object] = [
        _make_navbar(config.mode),
        dcc.Interval(
            id="refresh-interval",
            interval=_MARKET_INTERVAL_MS,
            n_intervals=0,
        ),
        dcc.Store(id="market-interval", data=_MARKET_INTERVAL_MS),
        dcc.Store(id="off-interval", data=_OFF_INTERVAL_MS),
    ]
    if config.mode == "paper":
        layout_children.append(_make_reset_modal())
    layout_children.append(dash.page_container)
    app.layout = dbc.Container(layout_children, fluid=True, className="pb-4")

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

    # Reset modal: open/close and run the reset when user clicks Proceed.
    # Paper-only — never registered in live mode.
    if config.mode == "paper":
        @app.callback(
            Output("reset-modal", "is_open"),
            Output("reset-modal-body", "children"),
            Output("reset-proceed-btn", "disabled"),
            Output("reset-proceed-btn", "children"),
            Input("open-reset-modal", "n_clicks"),
            Input("reset-cancel-btn", "n_clicks"),
            Input("reset-proceed-btn", "n_clicks"),
            State("reset-modal", "is_open"),
            prevent_initial_call=True,
        )
        def _reset_modal_callback(
            open_clicks: int,
            cancel_clicks: int,
            proceed_clicks: int,
            is_open: bool,
        ) -> tuple[bool, list[object], bool, str]:
            ctx = dash.callback_context
            if not ctx.triggered:
                return is_open, _reset_preview_body(), False, "Proceed"
            trigger_id = ctx.triggered[0]["prop_id"].split(".")[0]

            if trigger_id == "open-reset-modal":
                return True, _reset_preview_body(), False, "Proceed"
            if trigger_id == "reset-cancel-btn":
                return False, _reset_preview_body(), False, "Proceed"
            if trigger_id == "reset-proceed-btn":
                d = app.server.config["data"]
                try:
                    report = d.run_reset()
                except Exception as exc:
                    return True, _reset_error_body(str(exc)), True, "Done"
                return True, _reset_result_body(report), True, "Done"
            return is_open, _reset_preview_body(), False, "Proceed"

    # Warm up: trigger Dash's lazy _setup_server so the callback map is fully
    # populated before the threaded server starts accepting real requests.
    # Without this, concurrent GET + POST on first page load race past the
    # one-time init flag, causing "Callback not found" KeyErrors.
    with app.server.test_client() as client:
        client.get("/")

    return app
