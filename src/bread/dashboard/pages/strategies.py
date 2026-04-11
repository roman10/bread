"""Strategies leaderboard page — per-strategy realized P&L for paper trading.

Shows one row per strategy that has at least one completed round-trip in the
selected lookback window. Sorted by total P&L descending. The data is
attributed via OrderLog.strategy_name and aggregated by reusing the same
FIFO pair-matching used by the Trades page (single source of truth).
"""

from __future__ import annotations

import dash
import dash_ag_grid as dag
import dash_bootstrap_components as dbc
from dash import Input, Output, callback, dcc, html
from flask import current_app

from bread.dashboard.components import (
    format_currency,
    format_pct,
    make_kpi_card,
    make_kpi_row,
    pnl_color,
)

dash.register_page(__name__, path="/strategies", name="Strategies")

# -- AG Grid column definitions --
# Mirrors the trades page formatting style for consistency.

_LEADERBOARD_COLS = [
    {"field": "strategy_name", "headerName": "Strategy", "flex": 1, "minWidth": 160},
    {
        "field": "total_trades", "headerName": "Trades", "width": 90,
        "type": "numericColumn", "sort": "desc",
    },
    {
        "field": "win_rate_pct", "headerName": "Win %", "width": 90,
        "type": "numericColumn",
        "valueFormatter": {"function": "params.value.toFixed(1) + '%'"},
        "cellStyle": {
            "function":
            "params.value >= 50 ? {'color': '#00bc8c'} : {'color': '#f39c12'}"
        },
    },
    {
        "field": "total_pnl", "headerName": "P&L", "width": 120,
        "type": "numericColumn", "sort": "desc", "sortIndex": 0,
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
        "field": "expectancy", "headerName": "Expectancy", "width": 120,
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
        "field": "profit_factor", "headerName": "Profit Factor", "width": 120,
        "type": "numericColumn",
        "valueFormatter": {
            "function":
            "params.value === null ? '∞' : params.value.toFixed(2)"
        },
    },
    {
        "field": "best_trade", "headerName": "Best", "width": 100,
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
        "field": "worst_trade", "headerName": "Worst", "width": 100,
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
        "field": "avg_hold_days", "headerName": "Avg Hold", "width": 110,
        "type": "numericColumn",
        "valueFormatter": {"function": "params.value.toFixed(1) + 'd'"},
    },
]

# -- Layout --

layout = dbc.Container([
    html.Div(id="strategies-kpi-row"),
    dbc.Row([
        dbc.Col([
            dbc.Label("Lookback (days)", className="text-muted small"),
            dcc.Slider(
                id="strategies-days-filter",
                min=7, max=365, step=None, value=90,
                marks={7: "7", 30: "30", 90: "90", 180: "180", 365: "365"},
            ),
        ], md=6),
    ], className="mb-3"),
    html.H6("Strategy Leaderboard", className="text-muted mb-2 mt-3"),
    html.Div(id="strategies-leaderboard-table"),
    html.P(
        "Strategies appear once they have at least one completed round-trip "
        "in the selected window. Ranking is by realized P&L (dollars). Use "
        "`bread compare` for backtest-based selection — paper sample sizes "
        "alone are too small for confident strategy choice.",
        className="text-muted small mt-3",
    ),
], fluid=True)


# -- Callbacks --


@callback(
    Output("strategies-kpi-row", "children"),
    Output("strategies-leaderboard-table", "children"),
    Input("strategies-days-filter", "value"),
    Input("refresh-interval", "n_intervals"),
)
def update_leaderboard(days: int, _n: int) -> tuple:
    data = current_app.config["data"]
    rows = data.get_strategy_leaderboard(days=days)

    # KPI cards: aggregate across all strategies in the window
    total_pnl = sum(r["total_pnl"] for r in rows)
    total_trades = sum(r["total_trades"] for r in rows)
    active_count = len(rows)
    if total_trades > 0:
        weighted_win_rate = (
            sum(r["win_rate_pct"] * r["total_trades"] for r in rows) / total_trades
        )
    else:
        weighted_win_rate = 0.0

    if total_trades == 0:
        # No trades in window — show neutral colors, not warning/danger
        win_rate_color = "secondary"
    elif weighted_win_rate > 50:
        win_rate_color = "success"
    else:
        win_rate_color = "warning"

    cards = [
        make_kpi_card(
            "Active Strategies", str(active_count), color="info",
        ),
        make_kpi_card(
            "Total P&L", format_currency(total_pnl, show_sign=True),
            color=pnl_color(total_pnl),
        ),
        make_kpi_card(
            "Combined Win Rate", format_pct(weighted_win_rate),
            color=win_rate_color,
        ),
        make_kpi_card(
            "Total Trades", str(total_trades), color="info",
        ),
    ]
    kpi_row = make_kpi_row(cards)

    if not rows:
        table = html.P(
            "No completed trades in this window. Strategies will appear here "
            "as soon as they generate round-trip trades.",
            className="text-muted",
        )
    else:
        table = dag.AgGrid(
            rowData=rows,
            columnDefs=_LEADERBOARD_COLS,
            defaultColDef={"sortable": True, "resizable": True},
            dashGridOptions={"domLayout": "autoHeight"},
            className="ag-theme-alpine-dark",
            style={"width": "100%"},
        )

    return kpi_row, table
