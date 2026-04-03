"""Shared dashboard components — KPI cards used by multiple pages."""

from __future__ import annotations

import dash_bootstrap_components as dbc
from dash import html


def make_kpi_card(
    title: str,
    value: str,
    subtitle: str = "",
    color: str = "secondary",
) -> dbc.Card:
    """Build a single KPI card."""
    return dbc.Card(
        dbc.CardBody([
            html.H6(title, className="card-title text-muted mb-1",
                     style={"fontSize": "0.75rem"}),
            html.H4(value, className=f"text-{color} mb-0",
                     style={"fontWeight": "600"}),
            html.Small(subtitle, className="text-muted") if subtitle else html.Span(),
        ]),
        className="h-100",
    )


def make_kpi_row(cards: list[dbc.Card]) -> dbc.Row:
    """Arrange KPI cards in an evenly-spaced row."""
    cols = [dbc.Col(card, className="mb-3") for card in cards]
    return dbc.Row(cols, className="g-3")


def format_currency(value: float, show_sign: bool = False) -> str:
    """Format a float as $X,XXX.XX with optional sign."""
    if value < 0:
        return f"-${abs(value):,.2f}"
    sign = "+" if show_sign else ""
    return f"{sign}${value:,.2f}"


def format_pct(value: float, show_sign: bool = False) -> str:
    """Format a float as X.XX% with optional sign."""
    sign = "+" if show_sign and value >= 0 else ""
    return f"{sign}{value:.2f}%"


def pnl_color(value: float) -> str:
    """Return bootstrap color name for P&L values."""
    if value > 0:
        return "success"
    if value < 0:
        return "danger"
    return "secondary"
