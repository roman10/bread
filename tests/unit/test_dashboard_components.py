"""Tests for dashboard components and chart builders."""

from __future__ import annotations

from datetime import date

import plotly.graph_objects as go

from bread.dashboard.charts import make_drawdown_figure, make_equity_figure, make_pnl_figure
from bread.dashboard.components import (
    format_currency,
    format_pct,
    make_kpi_card,
    make_kpi_row,
    pnl_color,
)
from bread.monitoring.tracker import DailySummary


class TestKpiCard:
    def test_returns_card(self):
        import dash_bootstrap_components as dbc

        card = make_kpi_card("Equity", "$10,000", color="light")
        assert isinstance(card, dbc.Card)

    def test_kpi_row_with_cards(self):
        import dash_bootstrap_components as dbc

        cards = [
            make_kpi_card("A", "1"),
            make_kpi_card("B", "2"),
            make_kpi_card("C", "3"),
        ]
        row = make_kpi_row(cards)
        assert isinstance(row, dbc.Row)

    def test_kpi_row_empty(self):
        import dash_bootstrap_components as dbc

        row = make_kpi_row([])
        assert isinstance(row, dbc.Row)


class TestFormatHelpers:
    def test_format_currency_positive(self):
        assert format_currency(1234.56) == "$1,234.56"

    def test_format_currency_with_sign(self):
        assert format_currency(100.0, show_sign=True) == "+$100.00"
        assert format_currency(-50.0, show_sign=True) == "-$50.00"

    def test_format_currency_negative(self):
        assert format_currency(-1234.56) == "-$1,234.56"

    def test_format_pct(self):
        assert format_pct(5.25) == "5.25%"
        assert format_pct(5.25, show_sign=True) == "+5.25%"
        assert format_pct(-3.1, show_sign=True) == "-3.10%"

    def test_pnl_color(self):
        assert pnl_color(100) == "success"
        assert pnl_color(-50) == "danger"
        assert pnl_color(0) == "secondary"


class TestEquityChart:
    def test_empty_data(self):
        fig = make_equity_figure([])
        assert isinstance(fig, go.Figure)
        assert len(fig.layout.annotations) == 1
        assert "No data" in fig.layout.annotations[0].text

    def test_with_data(self):
        summaries = [
            DailySummary(
                date=date(2026, 3, i), open_equity=10000 + i * 100,
                close_equity=10000 + i * 100, pnl=100, pnl_pct=1.0,
                open_positions=1, high_equity=10000 + i * 100 + 50,
                low_equity=10000 + i * 100 - 50,
            )
            for i in range(1, 6)
        ]
        fig = make_equity_figure(summaries)
        assert isinstance(fig, go.Figure)
        assert len(fig.data) == 1


class TestDrawdownChart:
    def test_empty_data(self):
        fig = make_drawdown_figure([])
        assert isinstance(fig, go.Figure)
        assert len(fig.layout.annotations) == 1

    def test_with_data(self):
        series = [
            (date(2026, 3, 1), 0.0),
            (date(2026, 3, 2), 2.5),
            (date(2026, 3, 3), 5.0),
        ]
        fig = make_drawdown_figure(series)
        assert isinstance(fig, go.Figure)
        assert len(fig.data) == 1
        # Y values should be negated for visual
        assert fig.data[0].y[2] == -5.0


class TestPnlChart:
    def test_empty_data(self):
        fig = make_pnl_figure([])
        assert isinstance(fig, go.Figure)
        assert len(fig.layout.annotations) == 1

    def test_with_data(self):
        data = [
            ("2026-03-01", 150.0, 1.5),
            ("2026-03-02", -75.0, -0.75),
            ("2026-03-03", 200.0, 2.0),
        ]
        fig = make_pnl_figure(data)
        assert isinstance(fig, go.Figure)
        assert len(fig.data) == 1
        # Check green/red coloring
        colors = fig.data[0].marker.color
        assert colors[0] == "#00bc8c"  # positive
        assert colors[1] == "#e74c3c"  # negative
