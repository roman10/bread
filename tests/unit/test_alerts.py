"""Tests for monitoring.alerts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from bread.core.config import AlertSettings
from bread.monitoring.alerts import AlertManager


def _config(**overrides) -> AlertSettings:
    defaults = {
        "enabled": True,
        "urls": ["json://localhost"],
        "on_trade": True,
        "on_daily_summary": True,
        "on_risk_breach": True,
        "on_error": True,
        "rate_limit_seconds": 60,
    }
    defaults.update(overrides)
    return AlertSettings(**defaults)


class TestAlertManager:
    @patch("bread.monitoring.alerts.apprise.Apprise")
    def test_notify_trade_sends(self, mock_apprise_cls: MagicMock) -> None:
        mock_inst = mock_apprise_cls.return_value
        mgr = AlertManager(_config())

        mgr.notify_trade("SPY", "BUY", 10, 500.0, "rsi bounce")

        mock_inst.notify.assert_called_once()
        call_kwargs = mock_inst.notify.call_args
        assert "SPY" in call_kwargs.kwargs.get("body", call_kwargs[1].get("body", ""))

    @patch("bread.monitoring.alerts.apprise.Apprise")
    def test_notify_daily_summary_sends(self, mock_apprise_cls: MagicMock) -> None:
        mock_inst = mock_apprise_cls.return_value
        mgr = AlertManager(_config())

        mgr.notify_daily_summary(10234.50, 127.50, 1.3, 2, 1, 1)

        mock_inst.notify.assert_called_once()

    @patch("bread.monitoring.alerts.apprise.Apprise")
    def test_notify_risk_breach_sends(self, mock_apprise_cls: MagicMock) -> None:
        mock_inst = mock_apprise_cls.return_value
        mgr = AlertManager(_config())

        mgr.notify_risk_breach("daily_loss", "Daily loss 1.5% exceeded")

        mock_inst.notify.assert_called_once()

    @patch("bread.monitoring.alerts.apprise.Apprise")
    def test_notify_error_sends(self, mock_apprise_cls: MagicMock) -> None:
        mock_inst = mock_apprise_cls.return_value
        mgr = AlertManager(_config())

        mgr.notify_error("RuntimeError: something broke")

        mock_inst.notify.assert_called_once()

    @patch("bread.monitoring.alerts.apprise.Apprise")
    def test_disabled_alerts_not_sent(self, mock_apprise_cls: MagicMock) -> None:
        mock_inst = mock_apprise_cls.return_value
        mgr = AlertManager(_config(enabled=False))

        mgr.notify_trade("SPY", "BUY", 10, 500.0, "test")
        mgr.notify_error("test error")

        mock_inst.notify.assert_not_called()

    @patch("bread.monitoring.alerts.apprise.Apprise")
    def test_per_type_toggle_off(self, mock_apprise_cls: MagicMock) -> None:
        mock_inst = mock_apprise_cls.return_value
        mgr = AlertManager(_config(on_trade=False))

        mgr.notify_trade("SPY", "BUY", 10, 500.0, "test")

        mock_inst.notify.assert_not_called()

    @patch("bread.monitoring.alerts.apprise.Apprise")
    def test_rate_limiting(self, mock_apprise_cls: MagicMock) -> None:
        mock_inst = mock_apprise_cls.return_value
        mgr = AlertManager(_config(rate_limit_seconds=60))

        # First call goes through
        mgr.notify_trade("SPY", "BUY", 10, 500.0, "test")
        assert mock_inst.notify.call_count == 1

        # Second call within rate limit window is suppressed
        mgr.notify_trade("QQQ", "BUY", 5, 400.0, "test")
        assert mock_inst.notify.call_count == 1

    @patch("bread.monitoring.alerts.apprise.Apprise")
    def test_rate_limit_allows_after_window(self, mock_apprise_cls: MagicMock) -> None:
        mock_inst = mock_apprise_cls.return_value
        mgr = AlertManager(_config(rate_limit_seconds=60))

        mgr.notify_trade("SPY", "BUY", 10, 500.0, "test")
        assert mock_inst.notify.call_count == 1

        # Simulate time passing by backdating the last_sent
        mgr._last_sent["trade"] = datetime.now(UTC) - timedelta(seconds=61)

        mgr.notify_trade("QQQ", "BUY", 5, 400.0, "test")
        assert mock_inst.notify.call_count == 2

    @patch("bread.monitoring.alerts.apprise.Apprise")
    def test_notify_continues_after_apprise_exception(
        self, mock_apprise_cls: MagicMock,
    ) -> None:
        mock_inst = mock_apprise_cls.return_value
        mock_inst.notify.side_effect = RuntimeError("connection failed")
        mgr = AlertManager(_config())

        # Should not raise
        mgr.notify_trade("SPY", "BUY", 10, 500.0, "test")

        # Should not record in last_sent (so next attempt isn't rate-limited)
        assert "trade" not in mgr._last_sent

    @patch("bread.monitoring.alerts.apprise.Apprise")
    def test_different_types_not_rate_limited_together(
        self, mock_apprise_cls: MagicMock,
    ) -> None:
        mock_inst = mock_apprise_cls.return_value
        mgr = AlertManager(_config(rate_limit_seconds=60))

        mgr.notify_trade("SPY", "BUY", 10, 500.0, "test")
        mgr.notify_error("some error")

        # Both should go through — different types
        assert mock_inst.notify.call_count == 2

    @patch("bread.monitoring.alerts.apprise.Apprise")
    def test_title_prefix_applied(self, mock_apprise_cls: MagicMock) -> None:
        mock_inst = mock_apprise_cls.return_value
        mgr = AlertManager(_config(), title_prefix='[paper "Main" (PA123)]')

        mgr.notify_trade("SPY", "BUY", 10, 500.0, "test")

        title = mock_inst.notify.call_args.kwargs["title"]
        assert title == '[paper "Main" (PA123)] Trade: BUY SPY'

    @patch("bread.monitoring.alerts.apprise.Apprise")
    def test_no_prefix_default(self, mock_apprise_cls: MagicMock) -> None:
        mock_inst = mock_apprise_cls.return_value
        mgr = AlertManager(_config())

        mgr.notify_trade("SPY", "BUY", 10, 500.0, "test")

        title = mock_inst.notify.call_args.kwargs["title"]
        assert title == "Trade: BUY SPY"
