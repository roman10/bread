"""Chart builder functions for the dashboard.

Separated from page modules so they can be tested without a Dash app context.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import plotly.graph_objects as go

if TYPE_CHECKING:
    from bread.monitoring.tracker import DailySummary

_DARK_TEMPLATE = "plotly_dark"
_CHART_LAYOUT = {
    "template": _DARK_TEMPLATE,
    "paper_bgcolor": "rgba(0,0,0,0)",
    "plot_bgcolor": "rgba(0,0,0,0)",
    "margin": {"l": 40, "r": 20, "t": 30, "b": 30},
    "height": 300,
}


def _no_data_figure(title: str) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(**_CHART_LAYOUT, title=title)
    fig.add_annotation(
        text="No data yet", xref="paper", yref="paper",
        x=0.5, y=0.5, showarrow=False, font={"size": 16, "color": "#888"},
    )
    return fig


def make_equity_figure(summaries: list[DailySummary]) -> go.Figure:
    """Equity curve line chart from daily summaries."""
    if not summaries:
        return _no_data_figure("Equity Curve")

    dates = [s.date for s in summaries]
    equities = [s.close_equity for s in summaries]
    fig = go.Figure(go.Scatter(
        x=dates, y=equities, mode="lines",
        line={"color": "#00bc8c", "width": 2},
        fill="tozeroy", fillcolor="rgba(0,188,140,0.1)",
    ))
    fig.update_layout(**_CHART_LAYOUT, title="Equity Curve")
    fig.update_yaxes(tickprefix="$")
    return fig


def make_drawdown_figure(series: list[tuple]) -> go.Figure:
    """Drawdown area chart (negative values, red fill)."""
    if not series:
        return _no_data_figure("Drawdown")

    dates = [d for d, _ in series]
    dd = [-pct for _, pct in series]
    fig = go.Figure(go.Scatter(
        x=dates, y=dd, mode="lines",
        line={"color": "#e74c3c", "width": 2},
        fill="tozeroy", fillcolor="rgba(231,76,60,0.15)",
    ))
    fig.update_layout(**_CHART_LAYOUT, title="Drawdown")
    fig.update_yaxes(ticksuffix="%")
    return fig


def make_pnl_figure(period_data: list[tuple[str, float, float]]) -> go.Figure:
    """P&L bar chart with green/red coloring by period."""
    if not period_data:
        return _no_data_figure("P&L by Period")

    labels = [d[0] for d in period_data]
    pnls = [d[1] for d in period_data]
    colors = ["#00bc8c" if v >= 0 else "#e74c3c" for v in pnls]

    fig = go.Figure(go.Bar(x=labels, y=pnls, marker_color=colors))
    fig.update_layout(**_CHART_LAYOUT, title="P&L by Period")
    fig.update_yaxes(tickprefix="$")
    return fig
