"""Portfolio overview page — KPI cards, equity curve, positions, orders."""

from __future__ import annotations

import dash
import dash_ag_grid as dag
import dash_bootstrap_components as dbc
from dash import Input, Output, callback, dcc, html
from flask import current_app

from bread.dashboard.charts import make_drawdown_figure, make_equity_figure
from bread.dashboard.components import (
    format_currency,
    format_local_dt,
    format_pct,
    make_kpi_card,
    make_kpi_row,
    pnl_color,
)

dash.register_page(__name__, path="/", name="Portfolio")

# -- AG Grid column definitions --

_POSITION_COLS = [
    {"field": "symbol", "headerName": "Symbol", "width": 90},
    {"field": "qty", "headerName": "Qty", "width": 70, "type": "numericColumn"},
    {
        "field": "entry_price",
        "headerName": "Entry",
        "width": 100,
        "type": "numericColumn",
        "valueFormatter": {"function": "'$' + params.value.toFixed(2)"},
    },
    {
        "field": "current_price",
        "headerName": "Current",
        "width": 100,
        "type": "numericColumn",
        "valueFormatter": {"function": "'$' + params.value.toFixed(2)"},
    },
    {
        "field": "unrealized_pnl",
        "headerName": "P&L",
        "width": 110,
        "type": "numericColumn",
        "valueFormatter": {
            "function": "(params.value >= 0 ? '+$' : '-$') + Math.abs(params.value).toFixed(2)"
        },
        "cellStyle": {
            "function": "params.value >= 0 ? {'color': '#00bc8c'} : {'color': '#e74c3c'}"
        },
    },
    {
        "field": "unrealized_pct",
        "headerName": "P&L %",
        "width": 90,
        "type": "numericColumn",
        "valueFormatter": {
            "function": "(params.value >= 0 ? '+' : '') + params.value.toFixed(1) + '%'"
        },
        "cellStyle": {
            "function": "params.value >= 0 ? {'color': '#00bc8c'} : {'color': '#e74c3c'}"
        },
    },
    {
        "field": "market_value",
        "headerName": "Value",
        "width": 110,
        "type": "numericColumn",
        "valueFormatter": {"function": "'$' + params.value.toLocaleString()"},
    },
]

_ORDER_COLS = [
    {"field": "symbol", "headerName": "Symbol", "width": 90},
    {"field": "side", "headerName": "Side", "width": 70},
    {"field": "qty", "headerName": "Qty", "width": 70},
    {"field": "type", "headerName": "Type", "width": 100},
    {"field": "status", "headerName": "Status", "width": 100},
    {"field": "submitted_at", "headerName": "Submitted", "flex": 1},
]

_STRATEGY_COLS = [
    {"field": "name", "headerName": "Strategy", "width": 160},
    {
        "field": "status",
        "headerName": "Status",
        "width": 110,
        "cellStyle": {
            "function": (
                "params.value === 'active' ? {'color': '#00bc8c'} : "
                "params.value === 'disabled' ? {'color': '#888'} : "
                "{'color': '#f39c12'}"
            )
        },
    },
    {"field": "enabled", "headerName": "Enabled", "width": 90},
    {"field": "modes", "headerName": "Modes", "width": 120},
    {
        "field": "weight",
        "headerName": "Weight",
        "width": 80,
        "type": "numericColumn",
        "valueFormatter": {"function": "params.value.toFixed(2)"},
    },
    {"field": "universe", "headerName": "Universe", "flex": 1, "tooltipField": "universe"},
]

_SIGNAL_COLS = [
    {"field": "time", "headerName": "Time", "width": 200, "sort": "desc"},
    {"field": "strategy", "headerName": "Strategy", "width": 140},
    {"field": "symbol", "headerName": "Symbol", "width": 80},
    {
        "field": "direction",
        "headerName": "Direction",
        "width": 90,
        "cellStyle": {
            "function": "params.value === 'BUY' ? {'color': '#00bc8c'} : {'color': '#e74c3c'}"
        },
    },
    {
        "field": "strength",
        "headerName": "Strength",
        "width": 90,
        "type": "numericColumn",
        "valueFormatter": {"function": "params.value.toFixed(2)"},
    },
    {
        "field": "stop_loss_pct",
        "headerName": "Stop %",
        "width": 80,
        "type": "numericColumn",
        "valueFormatter": {"function": "params.value.toFixed(1) + '%'"},
    },
    {"field": "reason", "headerName": "Reason", "flex": 1},
]

# -- Layout --

layout = dbc.Container(
    [
        html.Div(id="portfolio-kpi-row"),
        html.H6("Bot Activity", className="text-muted mb-2 mt-3"),
        html.Div(id="bot-activity-row"),
        dbc.Row(
            [
                dbc.Col(html.Div(id="equity-chart"), md=7),
                dbc.Col(html.Div(id="drawdown-chart"), md=5),
            ],
            className="mb-4",
        ),
        html.H6("Strategy Status", className="text-muted mb-2"),
        html.Div(id="strategy-status-panel"),
        html.H6("Open Positions", className="text-muted mb-2 mt-4"),
        html.Div(id="positions-table"),
        html.H6("Open Orders", className="text-muted mb-2 mt-4"),
        html.Div(id="orders-table"),
        html.H6("Recent Signals", className="text-muted mb-2 mt-4"),
        dbc.Row(
            [
                dbc.Col(
                    [
                        dcc.Dropdown(
                            id="signals-strategy-filter",
                            placeholder="All strategies",
                            clearable=True,
                            className="dash-bootstrap",
                        ),
                    ],
                    md=3,
                ),
            ],
            className="mb-2",
        ),
        html.Div(id="signals-table"),
    ],
    fluid=True,
)


# -- Callbacks --


@callback(
    Output("portfolio-kpi-row", "children"),
    Input("refresh-interval", "n_intervals"),
)
def update_kpi(_n: int) -> dbc.Row:
    data = current_app.config["data"]
    s = data.get_account_summary()
    cards = [
        make_kpi_card("Equity", format_currency(s["equity"]), color="light"),
        make_kpi_card(
            "Daily P&L",
            format_currency(s["daily_pnl"], show_sign=True),
            subtitle=format_pct(s["daily_pct"], show_sign=True),
            color=pnl_color(s["daily_pnl"]),
        ),
        make_kpi_card(
            "Buying Power",
            format_currency(s["buying_power"]),
            color="info",
        ),
        make_kpi_card(
            "Drawdown",
            format_pct(s["drawdown_pct"]),
            color="danger"
            if s["drawdown_pct"] > 5
            else "warning"
            if s["drawdown_pct"] > 2
            else "secondary",
        ),
    ]
    return make_kpi_row(cards)


@callback(
    Output("equity-chart", "children"),
    Input("refresh-interval", "n_intervals"),
)
def update_equity_chart(_n: int) -> dcc.Graph:
    data = current_app.config["data"]
    summaries = data.get_equity_curve(days=90)
    fig = make_equity_figure(summaries)
    return dcc.Graph(figure=fig, config={"displayModeBar": False})


@callback(
    Output("drawdown-chart", "children"),
    Input("refresh-interval", "n_intervals"),
)
def update_drawdown_chart(_n: int) -> dcc.Graph:
    data = current_app.config["data"]
    series = data.get_drawdown_series()
    fig = make_drawdown_figure(series)
    return dcc.Graph(figure=fig, config={"displayModeBar": False})


@callback(
    Output("positions-table", "children"),
    Input("refresh-interval", "n_intervals"),
)
def update_positions(_n: int) -> dag.AgGrid | html.P:
    data = current_app.config["data"]
    positions = data.get_positions()
    if not positions:
        return html.P("No open positions", className="text-muted")
    return dag.AgGrid(
        rowData=positions,
        columnDefs=_POSITION_COLS,
        defaultColDef={"sortable": True, "resizable": True},
        dashGridOptions={"domLayout": "autoHeight"},
        className="ag-theme-alpine-dark",
        style={"width": "100%"},
    )


@callback(
    Output("orders-table", "children"),
    Input("refresh-interval", "n_intervals"),
)
def update_orders(_n: int) -> dag.AgGrid | html.P:
    data = current_app.config["data"]
    orders = data.get_open_orders()
    if not orders:
        return html.P("No open orders", className="text-muted")
    return dag.AgGrid(
        rowData=orders,
        columnDefs=_ORDER_COLS,
        defaultColDef={"sortable": True, "resizable": True},
        dashGridOptions={"domLayout": "autoHeight"},
        className="ag-theme-alpine-dark",
        style={"width": "100%"},
    )


# -- Bot Activity, Strategy Status, Recent Signals callbacks --


@callback(
    Output("bot-activity-row", "children"),
    Input("refresh-interval", "n_intervals"),
)
def update_bot_activity(_n: int) -> dbc.Row:
    data = current_app.config["data"]
    activity = data.get_bot_activity()

    last_tick = activity["last_tick"]
    last_tick_str = format_local_dt(last_tick, fmt="%-I:%M:%S %p %Z", fallback="Never")

    cards = [
        make_kpi_card(
            "Market",
            activity["market_status"],
            subtitle=activity["market_next"],
            color=activity["market_status_color"],
        ),
        make_kpi_card("Bot Status", activity["status"], color=activity["status_color"]),
        make_kpi_card("Last Tick", last_tick_str, color="light"),
        make_kpi_card("Ticks Today", str(activity["ticks_today"]), color="info"),
        make_kpi_card("Signals Today", str(activity["signals_today"]), color="info"),
        make_kpi_card("Trades Today", str(activity["trades_today"]), color="info"),
    ]
    return make_kpi_row(cards)


@callback(
    Output("strategy-status-panel", "children"),
    Input("refresh-interval", "n_intervals"),
)
def update_strategy_status(_n: int) -> dag.AgGrid | html.P:
    data = current_app.config["data"]
    strategies = data.get_strategy_status()
    if not strategies:
        return html.P("No strategies configured", className="text-muted")
    return dag.AgGrid(
        rowData=strategies,
        columnDefs=_STRATEGY_COLS,
        defaultColDef={"sortable": True, "resizable": True},
        dashGridOptions={"domLayout": "autoHeight"},
        className="ag-theme-alpine-dark",
        style={"width": "100%"},
    )


@callback(
    Output("signals-strategy-filter", "options"),
    Input("refresh-interval", "n_intervals"),
)
def update_signals_filter_options(_n: int) -> list[dict[str, str]]:
    data = current_app.config["data"]
    return [{"label": s, "value": s} for s in data.strategy_names]


@callback(
    Output("signals-table", "children"),
    Input("signals-strategy-filter", "value"),
    Input("refresh-interval", "n_intervals"),
)
def update_signals_table(strategy: str | None, _n: int) -> dag.AgGrid | html.P:
    data = current_app.config["data"]
    signals = data.get_recent_signals(hours=24, strategy=strategy)
    if not signals:
        return html.P("No signals in the last 24 hours", className="text-muted")
    return dag.AgGrid(
        rowData=signals,
        columnDefs=_SIGNAL_COLS,
        defaultColDef={"sortable": True, "resizable": True},
        dashGridOptions={
            "domLayout": "autoHeight",
            "pagination": True,
            "paginationPageSize": 15,
        },
        className="ag-theme-alpine-dark",
        style={"width": "100%"},
    )
