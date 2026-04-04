"""Alert manager — apprise-based notifications for trade events and risk breaches."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import apprise

if TYPE_CHECKING:
    from bread.core.config import AlertSettings

logger = logging.getLogger(__name__)


class AlertManager:
    """Sends notifications via apprise with per-type rate limiting."""

    def __init__(self, config: AlertSettings) -> None:
        self._config = config
        self._apprise = apprise.Apprise()
        for url in config.urls:
            self._apprise.add(url)
        self._last_sent: dict[str, datetime] = {}

    def notify_trade(
        self, symbol: str, side: str, qty: int, price: float, reason: str,
    ) -> None:
        """Send trade execution notification (normal priority)."""
        if not self._config.on_trade:
            return
        qty_str = f" {qty}" if qty > 0 else ""
        body = f"{side}{qty_str} {symbol} @ ${price:,.2f} — reason: {reason}"
        self._send("trade", f"Trade: {side} {symbol}", body, apprise.NotifyType.INFO)

    def notify_daily_summary(
        self,
        equity: float,
        daily_pnl: float,
        daily_pct: float,
        trades_today: int,
        wins: int,
        losses: int,
    ) -> None:
        """Send end-of-day summary (normal priority)."""
        if not self._config.on_daily_summary:
            return
        sign = "+" if daily_pnl >= 0 else ""
        body = (
            f"Daily P&L: {sign}${daily_pnl:,.2f} ({sign}{daily_pct:.1f}%) | "
            f"{trades_today} trades ({wins}W/{losses}L) | "
            f"Equity: ${equity:,.2f}"
        )
        self._send("daily_summary", "Daily Summary", body, apprise.NotifyType.INFO)

    def notify_risk_breach(self, breach_type: str, details: str) -> None:
        """Send risk limit breach alert (high priority)."""
        if not self._config.on_risk_breach:
            return
        self._send(
            f"risk_{breach_type}",
            f"Risk Breach: {breach_type}",
            details,
            apprise.NotifyType.WARNING,
        )

    def notify_event_alert(
        self,
        symbol: str,
        headline: str,
        details: str,
        event_type: str,
    ) -> None:
        """Send market event alert (high priority)."""
        if not self._config.on_research:
            return
        body = f"{symbol} [{event_type}]: {headline}\n{details[:300]}"
        self._send(
            f"event_{symbol}",
            f"Market Event: {symbol}",
            body,
            apprise.NotifyType.WARNING,
        )

    def notify_error(self, error: str) -> None:
        """Send system error alert (critical priority)."""
        if not self._config.on_error:
            return
        self._send("error", "System Error", error[:500], apprise.NotifyType.FAILURE)

    def _send(self, alert_type: str, title: str, body: str, notify_type: str) -> None:
        """Send notification with rate limiting."""
        if not self._config.enabled:
            return

        if not self._should_send(alert_type):
            logger.debug("Rate-limited alert type=%s", alert_type)
            return

        try:
            self._apprise.notify(title=title, body=body, notify_type=notify_type)
            self._last_sent[alert_type] = datetime.now(UTC)
            logger.info("Alert sent: type=%s title=%s", alert_type, title)
        except Exception:
            logger.exception("Failed to send alert type=%s", alert_type)

    def _should_send(self, alert_type: str) -> bool:
        """Check if enough time has passed since last alert of this type."""
        last = self._last_sent.get(alert_type)
        if last is None:
            return True
        elapsed = (datetime.now(UTC) - last).total_seconds()
        return elapsed >= self._config.rate_limit_seconds
