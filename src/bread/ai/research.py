"""Event monitoring — periodic web search for market-moving events."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select, update

from bread.ai.models import EventAlert
from bread.db.models import EventAlertLog

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from bread.ai.client import ClaudeClient
    from bread.ai.models import MarketResearch
    from bread.monitoring.alerts import AlertManager

logger = logging.getLogger(__name__)

MAX_RESEARCH_SYMBOLS = 30


def collect_research_symbols(
    held: list[str],
    watchlist: list[str],
    max_symbols: int = MAX_RESEARCH_SYMBOLS,
) -> tuple[list[str], list[str]]:
    """Build the research symbol universe.

    Returns ``(all_symbols, held_only)`` with held positions first,
    deduplicated, capped at *max_symbols*.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for sym in held:
        if sym not in seen:
            seen.add(sym)
            ordered.append(sym)
    for sym in watchlist:
        if sym not in seen:
            seen.add(sym)
            ordered.append(sym)
    all_symbols = ordered[:max_symbols]
    all_set = set(all_symbols)
    held_only = [s for s in held if s in all_set]
    return all_symbols, held_only


def get_active_alerts(
    session_factory: sessionmaker[Session],
    symbols: list[str],
    max_age_hours: int = 24,
) -> list[EventAlert]:
    """Query DB for active high/medium event alerts.

    Returns an empty list on any error (fail-open).
    """
    if not symbols:
        return []
    cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
    try:
        with session_factory() as session:
            rows = (
                session.execute(
                    select(EventAlertLog)
                    .where(
                        EventAlertLog.symbol.in_(symbols),
                        EventAlertLog.is_active.is_(True),
                        EventAlertLog.scanned_at_utc >= cutoff,
                        EventAlertLog.severity.in_(["high", "medium"]),
                    )
                    .order_by(EventAlertLog.scanned_at_utc.desc())
                )
                .scalars()
                .all()
            )
            return [
                EventAlert(
                    symbol=r.symbol,
                    severity=r.severity,
                    headline=r.headline,
                    details=r.details,
                    event_type=r.event_type,
                    source=r.source,
                )
                for r in rows
            ]
    except Exception:
        logger.exception("Failed to query active event alerts")
        return []


def run_research_scan(
    claude_client: ClaudeClient,
    session_factory: sessionmaker[Session],
    held_symbols: list[str],
    watchlist_symbols: list[str],
    alert_manager: AlertManager | None = None,
) -> None:
    """Execute one research scan cycle.

    Called by the scheduler.  Never raises — all errors are logged and
    swallowed (fail-open).
    """
    try:
        symbols, held = collect_research_symbols(held_symbols, watchlist_symbols)
        if not symbols:
            logger.info("Research scan: no symbols to research")
            return

        logger.info(
            "Research scan: scanning %d symbols (%d held)", len(symbols), len(held)
        )

        research = claude_client.research_events(symbols, held)

        _store_results(session_factory, research)
        _deactivate_stale_alerts(session_factory, max_age_hours=48)
        _send_high_severity_alerts(research, alert_manager)

        notable = [e for e in research.events if e.severity in ("high", "medium")]
        logger.info(
            "Research scan complete: %d events found, %d notable. Summary: %s",
            len(research.events),
            len(notable),
            research.scan_summary[:200],
        )
    except Exception:
        logger.exception("Research scan failed (non-fatal)")


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _store_results(
    session_factory: sessionmaker[Session],
    research: MarketResearch,
) -> None:
    """Persist research results to the ``event_alerts`` table."""
    try:
        with session_factory() as session:
            for event in research.events:
                if event.severity == "none":
                    continue
                session.add(
                    EventAlertLog(
                        symbol=event.symbol,
                        severity=event.severity,
                        headline=event.headline,
                        details=event.details,
                        event_type=event.event_type,
                        source=event.source,
                        scan_summary=research.scan_summary,
                        is_active=True,
                        scanned_at_utc=datetime.now(UTC),
                    )
                )
            session.commit()
    except Exception:
        logger.exception("Failed to store research results")


def _deactivate_stale_alerts(
    session_factory: sessionmaker[Session],
    max_age_hours: int = 48,
) -> None:
    """Mark alerts older than *max_age_hours* as inactive."""
    try:
        cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
        with session_factory() as session:
            session.execute(
                update(EventAlertLog)
                .where(
                    EventAlertLog.is_active.is_(True),
                    EventAlertLog.scanned_at_utc < cutoff,
                )
                .values(is_active=False)
            )
            session.commit()
    except Exception:
        logger.exception("Failed to deactivate stale alerts")


def _send_high_severity_alerts(
    research: MarketResearch,
    alert_manager: AlertManager | None,
) -> None:
    """Send notifications for high-severity events."""
    if alert_manager is None:
        return
    for event in research.events:
        if event.severity == "high":
            alert_manager.notify_event_alert(
                event.symbol,
                event.headline,
                event.details,
                event.event_type,
            )
