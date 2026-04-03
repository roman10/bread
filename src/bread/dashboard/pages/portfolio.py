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
        "field": "entry_price", "headerName": "Entry", "width": 100,
        "type": "numericColumn",
        "valueFormatter": {"function": "'$' + params.value.toFixed(2)"},
    },
    {
        "field": "current_price", "headerName": "Current", "width": 100,
        "type": "numericColumn",
        "valueFormatter": {"function": "'$' + params.value.toFixed(2)"},
    },
    {
        "field": "unrealized_pnl", "headerName": "P&L", "width": 110,
        "type": "numericColumn",
        "valueFormatter": {
            "function":
            "(params.value >= 0 ? '+$' : '-$') + Math.abs(params.value).toFixed(2)"
        },
        "cellStyle": {
            "function":
            "params.value >= 0 ? {'color': '#00bc8c'} : {'color': '#e74c3c'}"
        },
    },
    {
        "field": "unrealized_pct", "headerName": "P&L %", "width": 90,
        "type": "numericColumn",
        "valueFormatter": {
            "function":
            "(params.value >= 0 ? '+' : '') + params.value.toFixed(1) + '%'"
        },
        "cellStyle": {
            "function":
            "params.value >= 0 ? {'color': '#00bc8c'} : {'color': '#e74c3c'}"
        },
    },
    {
        "field": "market_value", "headerName": "Value", "width": 110,
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

# -- Layout --

layout = dbc.Container([
    html.Div(id="portfolio-kpi-row"),
    dbc.Row([
        dbc.Col(html.Div(id="equity-chart"), md=7),
        dbc.Col(html.Div(id="drawdown-chart"), md=5),
    ], className="mb-4"),
    html.H6("Open Positions", className="text-muted mb-2"),
    html.Div(id="positions-table"),
    html.H6("Open Orders", className="text-muted mb-2 mt-4"),
    html.Div(id="orders-table"),
], fluid=True)


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
            "Buying Power", format_currency(s["buying_power"]), color="info",
        ),
        make_kpi_card(
            "Drawdown",
            format_pct(s["drawdown_pct"]),
            color="danger" if s["drawdown_pct"] > 5 else "warning"
            if s["drawdown_pct"] > 2 else "secondary",
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
