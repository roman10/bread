"""Reset the paper-trading environment for clean testing.

Clears local trade history and optionally soft-resets the Alpaca account via
API. Alpaca's "Reset Account" web-dashboard button (which restores starting
cash) is NOT exposed via REST/SDK, so callers must surface
``ResetReport.manual_instructions`` to the user.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bread.core.exceptions import BreadError
from bread.db.models import (
    ClaudeUsageLog,
    EventAlertLog,
    MarketDataCache,
    OrderLog,
    PortfolioSnapshot,
    SignalLog,
)

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from bread.core.config import AppConfig
    from bread.execution.broker import Broker

logger = logging.getLogger(__name__)


ALPACA_PAPER_DASHBOARD_URL = "https://app.alpaca.markets/paper/dashboard/overview"

MANUAL_INSTRUCTIONS = (
    "Alpaca's account reset (restore starting cash, wipe ledger) is NOT "
    "available via the API. To complete the reset:\n"
    f"  1. Open {ALPACA_PAPER_DASHBOARD_URL}\n"
    "  2. Click your account menu → \"Reset Account\"\n"
    "  3. Confirm — your paper cash will be restored to the default balance."
)


@dataclass
class ResetReport:
    """Summary of a reset run — what was cleared and what the user must still do."""

    broker_orders_cancelled: int
    broker_positions_closed: int
    orders_deleted: int
    signals_deleted: int
    snapshots_deleted: int
    alerts_deleted: int
    claude_usage_deleted: int
    bars_preserved: int
    manual_instructions: str = MANUAL_INSTRUCTIONS


def reset_environment(
    config: AppConfig,
    broker: Broker | None,
    engine: Engine,
) -> ResetReport:
    """Reset the paper environment.

    Steps (in order):
      1. Refuse if ``config.mode == "live"`` — reset has no business touching
         a live account.
      2. If ``broker`` is provided: cancel all open orders, then close all
         open positions via Alpaca's bulk endpoints. Both calls log-on-failure
         rather than raising so the local cleanup still runs.
      3. Delete rows from every trade-history table (orders, signals,
         snapshots, event alerts, Claude usage log). The
         ``market_data_cache`` table is preserved to avoid re-downloading
         years of OHLCV bars.

    Returns a ``ResetReport`` with counts and the manual instructions string.
    """
    if config.mode != "paper":
        raise BreadError(
            "Refusing to reset in live mode. Reset is paper-only."
        )

    broker_orders_cancelled = 0
    broker_positions_closed = 0
    if broker is not None:
        broker_orders_cancelled = broker.cancel_all_orders()
        broker_positions_closed = broker.close_all_positions()

    # Keep market_data_cache rows — expensive to rebuild and not trade state.
    # session.query(...).delete() returns affected-row count as int, avoiding
    # the Result.rowcount typing lint that `session.execute(delete(...))` hits.
    with Session(engine) as session:
        orders_deleted = session.query(OrderLog).delete()
        signals_deleted = session.query(SignalLog).delete()
        snapshots_deleted = session.query(PortfolioSnapshot).delete()
        alerts_deleted = session.query(EventAlertLog).delete()
        claude_usage_deleted = session.query(ClaudeUsageLog).delete()
        bars_preserved = (
            session.execute(select(func.count(MarketDataCache.id))).scalar_one() or 0
        )
        session.commit()

    logger.info(
        "Reset complete: broker_orders=%d broker_positions=%d "
        "orders_deleted=%d signals_deleted=%d snapshots_deleted=%d "
        "alerts_deleted=%d claude_usage_deleted=%d bars_preserved=%d",
        broker_orders_cancelled, broker_positions_closed,
        orders_deleted, signals_deleted, snapshots_deleted,
        alerts_deleted, claude_usage_deleted, bars_preserved,
    )

    return ResetReport(
        broker_orders_cancelled=broker_orders_cancelled,
        broker_positions_closed=broker_positions_closed,
        orders_deleted=orders_deleted,
        signals_deleted=signals_deleted,
        snapshots_deleted=snapshots_deleted,
        alerts_deleted=alerts_deleted,
        claude_usage_deleted=claude_usage_deleted,
        bars_preserved=bars_preserved,
    )
