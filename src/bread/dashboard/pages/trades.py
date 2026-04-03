"""Trade journal page — filters, P&L chart, journal table, summary stats."""

from __future__ import annotations

from datetime import date, timedelta

import dash
import dash_ag_grid as dag
import dash_bootstrap_components as dbc
from dash import Input, Output, callback, dcc, html
from flask import current_app

from bread.dashboard.charts import make_pnl_figure
from bread.dashboard.components import (
    format_currency,
    format_pct,
    make_kpi_card,
    make_kpi_row,
    pnl_color,
)

dash.register_page(__name__, path="/trades", name="Trades")

# -- AG Grid column definitions --

_JOURNAL_COLS = [
    {"field": "exit_date", "headerName": "Date", "width": 110, "sort": "desc"},
    {"field": "symbol", "headerName": "Symbol", "width": 80},
    {"field": "qty", "headerName": "Qty", "width": 60, "type": "numericColumn"},
    {
        "field": "entry_price", "headerName": "Entry", "width": 95,
        "type": "numericColumn",
        "valueFormatter": {"function": "'$' + params.value.toFixed(2)"},
    },
    {
        "field": "exit_price", "headerName": "Exit", "width": 95,
        "type": "numericColumn",
        "valueFormatter": {"function": "'$' + params.value.toFixed(2)"},
    },
    {
        "field": "pnl", "headerName": "P&L", "width": 100,
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
        "field": "pnl_pct", "headerName": "P&L %", "width": 80,
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
    {"field": "hold_days", "headerName": "Hold", "width": 65, "type": "numericColumn"},
    {"field": "strategy_name", "headerName": "Strategy", "width": 120},
    {"field": "exit_reason", "headerName": "Reason", "flex": 1},
]

# -- Layout --

layout = dbc.Container([
    html.Div(id="trades-kpi-row"),
    # Filters
    dbc.Row([
        dbc.Col([
            dbc.Label("Strategy", className="text-muted small"),
            dcc.Dropdown(
                id="trades-strategy-filter",
                placeholder="All strategies",
                className="dash-bootstrap",
            ),
        ], md=3),
        dbc.Col([
            dbc.Label("Symbol", className="text-muted small"),
            dbc.Input(
                id="trades-symbol-filter", type="text",
                placeholder="e.g. SPY", debounce=True,
            ),
        ], md=2),
        dbc.Col([
            dbc.Label("Lookback (days)", className="text-muted small"),
            dcc.Slider(
                id="trades-days-filter",
                min=7, max=365, step=None, value=30,
                marks={7: "7", 30: "30", 90: "90", 180: "180", 365: "365"},
            ),
        ], md=4),
        dbc.Col([
            dbc.Label("P&L Period", className="text-muted small"),
            dbc.RadioItems(
                id="trades-period-toggle",
                options=[
                    {"label": "Daily", "value": "daily"},
                    {"label": "Weekly", "value": "weekly"},
                    {"label": "Monthly", "value": "monthly"},
                ],
                value="daily",
                inline=True,
                className="mt-1",
            ),
        ], md=3),
    ], className="mb-3"),
    # P&L chart
    html.Div(id="pnl-chart"),
    # Journal table
    html.H6("Trade Journal", className="text-muted mb-2 mt-3"),
    html.Div(id="journal-table"),
], fluid=True)


# -- Callbacks --


@callback(
    Output("trades-strategy-filter", "options"),
    Input("refresh-interval", "n_intervals"),
)
def update_strategy_options(_n: int) -> list[dict]:
    data = current_app.config["data"]
    return [{"label": s, "value": s} for s in data.strategy_names]


@callback(
    Output("trades-kpi-row", "children"),
    Output("journal-table", "children"),
    Input("trades-strategy-filter", "value"),
    Input("trades-symbol-filter", "value"),
    Input("trades-days-filter", "value"),
    Input("refresh-interval", "n_intervals"),
)
def update_journal(
    strategy: str | None,
    symbol: str | None,
    days: int,
    _n: int,
) -> tuple:
    data = current_app.config["data"]
    start = date.today() - timedelta(days=days)
    symbol_upper = symbol.upper().strip() if symbol else None

    entries = data.get_journal(start=start, strategy=strategy, symbol=symbol_upper)
    summary = data.get_journal_summary(entries)

    # KPI cards
    total_pnl = summary["total_pnl"]
    cards = [
        make_kpi_card(
            "Total P&L", format_currency(total_pnl, show_sign=True),
            color=pnl_color(total_pnl),
        ),
        make_kpi_card(
            "Win Rate", format_pct(summary["win_rate_pct"]),
            color="success" if summary["win_rate_pct"] > 50 else "warning",
        ),
        make_kpi_card(
            "Expectancy", format_currency(summary["expectancy"], show_sign=True),
            color=pnl_color(summary["expectancy"]),
        ),
        make_kpi_card(
            "Trades", str(summary["total_trades"]), color="info",
        ),
    ]
    kpi_row = make_kpi_row(cards)

    # Journal table
    if not entries:
        table = html.P("No completed trades in this period.", className="text-muted")
    else:
        rows = [
            {
                "exit_date": e.exit_date.isoformat(),
                "symbol": e.symbol,
                "qty": e.qty,
                "entry_price": e.entry_price,
                "exit_price": e.exit_price,
                "pnl": e.pnl,
                "pnl_pct": e.pnl_pct,
                "hold_days": e.hold_days,
                "strategy_name": e.strategy_name,
                "exit_reason": e.exit_reason,
            }
            for e in entries
        ]
        table = dag.AgGrid(
            rowData=rows,
            columnDefs=_JOURNAL_COLS,
            defaultColDef={"sortable": True, "resizable": True},
            dashGridOptions={"domLayout": "autoHeight", "pagination": True,
                             "paginationPageSize": 25},
            className="ag-theme-alpine-dark",
            style={"width": "100%"},
        )

    return kpi_row, table


@callback(
    Output("pnl-chart", "children"),
    Input("trades-period-toggle", "value"),
    Input("refresh-interval", "n_intervals"),
)
def update_pnl_chart(period: str, _n: int) -> dcc.Graph:
    data = current_app.config["data"]
    period_data = data.get_period_pnl(period)
    fig = make_pnl_figure(period_data)
    return dcc.Graph(figure=fig, config={"displayModeBar": False})
