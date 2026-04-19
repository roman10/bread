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
        "headerTooltip": "% of completed trades that made money",
    },
    {
        "field": "realized_pnl", "headerName": "Realized", "width": 110,
        "type": "numericColumn",
        "valueFormatter": {
            "function":
            "(params.value >= 0 ? '+$' : '-$') + Math.abs(params.value).toFixed(2)"
        },
        "cellStyle": {
            "function":
            "params.value >= 0 ? {'color': '#00bc8c'} : {'color': '#e74c3c'}"
        },
        "headerTooltip": "Locked-in profit/loss from fully closed trades (bought and sold)",
    },
    {
        "field": "unrealized_pnl", "headerName": "Unrealized", "width": 120,
        "type": "numericColumn",
        "valueFormatter": {
            "function":
            "(params.value >= 0 ? '+$' : '-$') + Math.abs(params.value).toFixed(2)"
        },
        "cellStyle": {
            "function":
            "params.value >= 0 ? {'color': '#00bc8c'} : {'color': '#e74c3c'}"
        },
        "headerTooltip": "Paper profit/loss on open positions at today's price — not yet locked in",
    },
    {
        "field": "total_pnl", "headerName": "Total P&L", "width": 120,
        "type": "numericColumn", "sort": "desc", "sortIndex": 0,
        "valueFormatter": {
            "function":
            "(params.value >= 0 ? '+$' : '-$') + Math.abs(params.value).toFixed(2)"
        },
        "cellStyle": {
            "function":
            "params.value >= 0 ? {'color': '#00bc8c'} : {'color': '#e74c3c'}"
        },
        "headerTooltip": "Realized + Unrealized combined",
    },
    {
        "field": "open_positions", "headerName": "Open", "width": 75,
        "type": "numericColumn",
        "headerTooltip": "Number of positions this strategy currently holds",
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
        "headerTooltip": "Average profit or loss per trade. Positive = profitable on average",
    },
    {
        "field": "profit_factor", "headerName": "Profit Factor", "width": 120,
        "type": "numericColumn",
        "valueFormatter": {
            "function":
            "params.value === null ? '∞' : params.value.toFixed(2)"
        },
        "headerTooltip": "Total gains ÷ total losses. Above 1.0 = profitable. ∞ = no losses yet",
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
        "headerTooltip": "The single most profitable completed trade",
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
        "headerTooltip": "The single biggest losing trade",
    },
    {
        "field": "avg_hold_days", "headerName": "Avg Hold", "width": 110,
        "type": "numericColumn",
        "valueFormatter": {"function": "params.value.toFixed(1) + 'd'"},
        "headerTooltip": "Average number of days a position is held before selling",
    },
]

# -- Layout --

layout = dbc.Container([
    html.Div(id="strategies-kpi-row"),
    dbc.Row([
        dbc.Col([
            dbc.Label("Lookback (days)", className="small"),
            dcc.Slider(
                id="strategies-days-filter",
                min=7, max=365, step=None, value=90,
                marks={
                    v: {"label": str(v), "style": {"color": "#dee2e6"}}
                    for v in [7, 30, 90, 180, 365]
                },
            ),
        ], md=6),
    ], className="mb-3"),
    html.H6("Strategy Leaderboard", className="mb-2 mt-3"),
    html.Div(id="strategies-leaderboard-table"),
    html.P(
        "Strategies appear once they have at least one completed round-trip "
        "in the selected window. Ranking is by Total P&L (realized + "
        "unrealized as-of-now). Strategies with only open positions (no "
        "round-trips yet) are visible on the Portfolio page. Use "
        "`bread compare` for backtest-based selection — paper sample sizes "
        "alone are too small for confident strategy choice.",
        className="small mt-3 opacity-75",
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
    realized_pnl = sum(r["realized_pnl"] for r in rows)
    unrealized_pnl = sum(r["unrealized_pnl"] for r in rows)
    total_pnl = realized_pnl + unrealized_pnl
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

    pnl_subtitle = (
        f"realized {format_currency(realized_pnl, show_sign=True)} · "
        f"unrealized {format_currency(unrealized_pnl, show_sign=True)} (now)"
    )

    cards = [
        make_kpi_card(
            "Strategies w/ Trades",
            str(active_count),
            subtitle="round-trips in window",
            color="info",
            info=(
                "How many of your active strategies have made at least one complete "
                "buy-then-sell trade in the selected time window."
            ),
            card_id="strats-with-trades",
        ),
        make_kpi_card(
            "Total P&L", format_currency(total_pnl, show_sign=True),
            subtitle=pnl_subtitle,
            color=pnl_color(total_pnl),
            info=(
                "Combined profit/loss across all strategies. "
                "Realized = locked-in gains/losses from fully closed trades. "
                "Unrealized = paper gains/losses on positions still open, valued at today's price."
            ),
            card_id="strats-total-pnl",
        ),
        make_kpi_card(
            "Combined Win Rate", format_pct(weighted_win_rate),
            color=win_rate_color,
            info=(
                "Percentage of completed trades that made money, weighted by trade count "
                "across all strategies. Above 50% means more wins than losses."
            ),
            card_id="combined-win-rate",
        ),
        make_kpi_card(
            "Total Trades", str(total_trades), color="info",
            info=(
                "Total number of completed buy-then-sell round-trips across all strategies "
                "in the selected window."
            ),
            card_id="total-trades",
        ),
    ]
    kpi_row = make_kpi_row(cards)

    if not rows:
        table = html.P(
            "No completed trades in this window. Strategies will appear here "
            "as soon as they generate round-trip trades.",
            className="opacity-75",
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
