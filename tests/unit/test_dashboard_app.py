"""Tests for dashboard application factory."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bread.dashboard.app import create_app


def _make_config():
    """Build a minimal AppConfig for testing."""
    from bread.core.config import AppConfig

    return AppConfig.model_validate({
        "mode": "paper",
        "db": {"path": ":memory:"},
        "alpaca": {
            "paper_api_key": "test-key",
            "paper_secret_key": "test-secret",
        },
        "strategies": [
            {"name": "etf_momentum", "config_path": "strategies/etf_momentum.yaml"},
        ],
    })


def _mock_dashboard_data():
    """Build a MagicMock that satisfies DashboardData's interface."""
    mock = MagicMock()
    mock.broker_available = False
    mock.get_account_summary.return_value = {
        "equity": 0.0, "cash": 0.0, "buying_power": 0.0,
        "daily_pnl": 0.0, "daily_pct": 0.0, "drawdown_pct": 0.0,
    }
    mock.get_positions.return_value = []
    mock.get_open_orders.return_value = []
    mock.get_equity_curve.return_value = []
    mock.get_drawdown_series.return_value = []
    mock.get_bot_activity.return_value = {
        "last_tick": None, "ticks_today": 0, "signals_today": 0, "trades_today": 0,
        "status": "No Data", "status_color": "secondary",
        "market_status": "Closed", "market_status_color": "secondary", "market_next": "",
    }
    mock.get_strategy_status.return_value = []
    mock.strategy_names = []
    mock.get_recent_signals.return_value = []
    mock.get_account_label.return_value = ""
    return mock


@pytest.fixture()
def dash_app():
    """Create a Dash app with DashboardData mocked to avoid DB/broker I/O."""
    with patch("bread.dashboard.app.DashboardData", return_value=_mock_dashboard_data()):
        return create_app(_make_config())


# Expected callback outputs from page modules (portfolio + trades).
_EXPECTED_PAGE_CALLBACKS = [
    # portfolio.py
    "portfolio-kpi-row.children",
    "equity-chart.children",
    "drawdown-chart.children",
    "positions-table.children",
    "orders-table.children",
    "bot-activity-row.children",
    "strategy-status-panel.children",
    "signals-strategy-filter.options",
    "signals-table.children",
    # trades.py
    "trades-strategy-filter.options",
    "pnl-chart.children",
]


class TestCreateApp:
    def test_returns_dash_app(self, dash_app):
        import dash

        assert isinstance(dash_app, dash.Dash)

    def test_page_callbacks_registered(self, dash_app):
        """All page callbacks must be in callback_map after create_app (warmup)."""
        for cb_key in _EXPECTED_PAGE_CALLBACKS:
            assert cb_key in dash_app.callback_map, f"missing callback: {cb_key}"

    def test_global_callback_map_drained(self, dash_app):
        """Warmup should move all callbacks from GLOBAL to app.callback_map."""
        from dash._callback import GLOBAL_CALLBACK_MAP

        assert len(GLOBAL_CALLBACK_MAP) == 0

    def test_app_callbacks_registered(self, dash_app):
        """App-level callbacks (connection dot, refresh interval) must exist."""
        assert "connection-dot.children" in dash_app.callback_map
        assert "refresh-interval.interval" in dash_app.callback_map

    def test_pages_discovered(self, dash_app):
        """Both portfolio and trades pages should be in the page registry."""
        from dash._pages import PAGE_REGISTRY

        paths = {p["path"] for p in PAGE_REGISTRY.values()}
        assert "/" in paths
        assert "/trades" in paths

    def test_callback_dispatch_succeeds(self, dash_app):
        """A callback POST should return 200, not KeyError."""
        import json

        with dash_app.server.test_client() as client:
            payload = {
                "output": "portfolio-kpi-row.children",
                "outputs": {"id": "portfolio-kpi-row", "property": "children"},
                "inputs": [
                    {"id": "refresh-interval", "property": "n_intervals", "value": 0}
                ],
                "changedPropIds": ["refresh-interval.n_intervals"],
                "state": [],
            }
            resp = client.post(
                "/_dash-update-component",
                data=json.dumps(payload),
                content_type="application/json",
            )
            assert resp.status_code == 200
