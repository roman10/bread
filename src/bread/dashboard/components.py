"""Shared dashboard components — KPI cards used by multiple pages."""

from __future__ import annotations

from datetime import UTC, datetime

import dash_bootstrap_components as dbc
from dash import html


def make_kpi_card(
    title: str,
    value: str,
    subtitle: str = "",
    color: str = "secondary",
    info: str = "",
    card_id: str = "",
) -> dbc.Card:
    """Build a single KPI card. Pass *info* + *card_id* to show a ⓘ tooltip."""
    if info and card_id:
        icon_id = f"kpi-info-{card_id}"
        title_content: str | list = [
            title,
            html.Span(
                "ⓘ",
                id=icon_id,
                style={"cursor": "default", "fontSize": "0.8em", "opacity": "0.45",
                       "marginLeft": "4px"},
            ),
        ]
        tooltip = dbc.Tooltip(
            info,
            target=icon_id,
            placement="bottom",
            style={"maxWidth": "300px", "textAlign": "left"},
        )
    else:
        title_content = title
        tooltip = None

    return dbc.Card(
        dbc.CardBody(
            [
                html.H6(
                    title_content,
                    className="card-title mb-1 opacity-75",
                    style={"fontSize": "0.75rem"},
                ),
                html.H4(value, className=f"text-{color} mb-0", style={"fontWeight": "600"}),
                html.Small(subtitle, className="opacity-75") if subtitle else html.Span(),
                tooltip,
            ]
        ),
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


def make_strategy_explanation(info: dict[str, str | list[str]]) -> list[object]:
    """Build modal body content for a strategy explanation.

    *info* keys: summary, what (list of steps), why, universe, effectiveness_good
    (list), effectiveness_bad (list).
    """
    sections: list[object] = [
        html.P(info["summary"], style={"fontSize": "1.05rem", "lineHeight": "1.6"}),
        html.H6("What it does", className="text-info mt-3 mb-1"),
        html.Ol([html.Li(step) for step in info["what"]]),
        html.H6("Why it works", className="text-info mt-3 mb-1"),
        html.P(info["why"]),
        html.H6("Why these ETFs?", className="text-info mt-3 mb-1"),
        html.P(info["universe"]),
        html.H6("Effectiveness", className="text-info mt-3 mb-1"),
        html.P("Works well when:", className="mb-1 fw-bold", style={"fontSize": "0.9rem"}),
        html.Ul([html.Li(g) for g in info["effectiveness_good"]]),
        html.P("Watch out:", className="mb-1 fw-bold", style={"fontSize": "0.9rem"}),
        html.Ul([html.Li(b) for b in info["effectiveness_bad"]]),
    ]
    return sections


def format_local_dt(
    dt: datetime | None,
    fmt: str = "%-I:%M %p %Z",
    *,
    fallback: str = "",
) -> str:
    """Format a UTC datetime for display in the system's local timezone."""
    if dt is None:
        return fallback
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local_dt = dt.astimezone()  # no arg = system timezone
    return local_dt.strftime(fmt)
