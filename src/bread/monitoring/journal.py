"""Trade journal — query layer on OrderLog for completed round-trips."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from bread.db.models import OrderLog

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JournalEntry:
    symbol: str
    strategy_name: str
    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float
    qty: int
    pnl: float
    pnl_pct: float
    hold_days: int
    entry_reason: str
    exit_reason: str


def get_journal(
    session: Session,
    *,
    start: date | None = None,
    end: date | None = None,
    strategy: str | None = None,
    symbol: str | None = None,
    limit: int = 100,
) -> list[JournalEntry]:
    """Query completed round-trip trades from OrderLog.

    Pairs BUY fills with subsequent SELL fills by symbol (FIFO). Attribution
    (JournalEntry.strategy_name) comes from the BUY row — the strategy that
    selected the trade. Returns entries sorted by exit_date descending.
    """
    query = (
        select(OrderLog)
        .where(OrderLog.status == "FILLED")
        .order_by(OrderLog.filled_at_utc.asc())
    )

    rows = session.execute(query).scalars().all()

    # Separate buys and sells by symbol only. The engine enforces one open
    # position per symbol (engine rejects BUY if symbol already held; SELL is
    # only emitted when a position exists), so the interleaved sequence for
    # any symbol is B1,S1,B2,S2,... — FIFO on symbol alone cannot cross
    # unrelated lots. The pair's strategy_name is taken from the BUY row
    # (the strategy that selected the trade); exit attribution remains
    # queryable via the raw SELL row's strategy_name.
    buys: dict[str, list[OrderLog]] = {}
    sells: dict[str, list[OrderLog]] = {}

    for row in rows:
        if row.side == "BUY":
            buys.setdefault(row.symbol, []).append(row)
        elif row.side == "SELL":
            sells.setdefault(row.symbol, []).append(row)

    # Pair: for each sell, match the earliest unmatched buy (FIFO).
    # If the candidate BUY is timestamped AFTER the SELL, the SELL is orphan
    # (its opening BUY predates our history); skip the SELL but leave buy_idx
    # alone — that BUY belongs to a later SELL.
    entries: list[JournalEntry] = []
    for sym, sell_list in sells.items():
        buy_list = buys.get(sym, [])
        buy_idx = 0
        for sell_order in sell_list:
            if buy_idx >= len(buy_list):
                logger.debug("Orphan SELL for %s — no unmatched BUY", sym)
                continue

            buy_order = buy_list[buy_idx]

            if (
                buy_order.filled_at_utc is not None
                and sell_order.filled_at_utc is not None
                and buy_order.filled_at_utc > sell_order.filled_at_utc
            ):
                logger.debug("Orphan SELL for %s — BUY missing from history", sym)
                continue

            buy_idx += 1

            if buy_order.filled_price is None or sell_order.filled_price is None:
                logger.debug("Missing fill price for %s — skipping pair", sym)
                continue

            entry_price = float(buy_order.filled_price)
            exit_price = float(sell_order.filled_price)
            qty = buy_order.qty

            pnl = round((exit_price - entry_price) * qty, 2)
            pnl_pct = round(
                (exit_price - entry_price) / entry_price * 100, 4,
            ) if entry_price > 0 else 0.0

            entry_dt = (buy_order.filled_at_utc or buy_order.created_at_utc).date()
            exit_dt = (sell_order.filled_at_utc or sell_order.created_at_utc).date()
            hold_days = (exit_dt - entry_dt).days

            entry = JournalEntry(
                symbol=sym,
                strategy_name=buy_order.strategy_name,
                entry_date=entry_dt,
                entry_price=entry_price,
                exit_date=exit_dt,
                exit_price=exit_price,
                qty=qty,
                pnl=pnl,
                pnl_pct=pnl_pct,
                hold_days=hold_days,
                entry_reason=buy_order.reason,
                exit_reason=sell_order.reason,
            )
            entries.append(entry)

    # Apply filters
    if start is not None:
        entries = [e for e in entries if e.exit_date >= start]
    if end is not None:
        entries = [e for e in entries if e.exit_date <= end]
    if strategy is not None:
        entries = [e for e in entries if e.strategy_name == strategy]
    if symbol is not None:
        entries = [e for e in entries if e.symbol == symbol]

    # Sort by exit_date descending
    entries.sort(key=lambda e: e.exit_date, reverse=True)

    return entries[:limit]


def get_journal_summary(entries: list[JournalEntry]) -> dict[str, float | int]:
    """Compute summary stats from journal entries."""
    if not entries:
        return {
            "win_rate_pct": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "expectancy": 0.0,
            "total_pnl": 0.0,
            "total_trades": 0,
            "best_trade": 0.0,
            "worst_trade": 0.0,
        }

    wins = [e for e in entries if e.pnl > 0]
    losses = [e for e in entries if e.pnl <= 0]
    total = len(entries)

    win_rate = len(wins) / total * 100
    avg_win = sum(e.pnl for e in wins) / len(wins) if wins else 0.0
    avg_loss = sum(e.pnl for e in losses) / len(losses) if losses else 0.0
    total_pnl = sum(e.pnl for e in entries)
    expectancy = total_pnl / total

    return {
        "win_rate_pct": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": expectancy,
        "total_pnl": total_pnl,
        "total_trades": total,
        "best_trade": max(e.pnl for e in entries),
        "worst_trade": min(e.pnl for e in entries),
    }


@dataclass(frozen=True)
class StrategyPnLSummary:
    """Per-strategy realized P&L breakdown — surfaced on the dashboard."""

    strategy_name: str
    total_trades: int
    win_rate_pct: float
    total_pnl: float
    expectancy: float
    profit_factor: float  # sum(wins) / |sum(losses)|; inf if zero losses
    best_trade: float
    worst_trade: float
    avg_hold_days: float


def get_all_strategies_summary(
    session: Session,
    *,
    days: int = 365,
) -> list[StrategyPnLSummary]:
    """Group completed trades by strategy and compute per-strategy summary stats.

    Reuses get_journal() and get_journal_summary() so the FIFO pair-matching
    logic stays single-source-of-truth. Returns an entry only for strategies
    that have at least one completed round-trip in the window. Sorted by
    total_pnl descending so the best earner appears first.
    """
    start = date.today() - timedelta(days=days)
    # Pull a generous limit so the leaderboard isn't silently truncated.
    entries = get_journal(session, start=start, limit=10_000)

    by_strategy: dict[str, list[JournalEntry]] = {}
    for e in entries:
        by_strategy.setdefault(e.strategy_name, []).append(e)

    summaries: list[StrategyPnLSummary] = []
    for strategy_name, group in by_strategy.items():
        base = get_journal_summary(group)

        winning_pnl = sum(e.pnl for e in group if e.pnl > 0)
        losing_pnl = sum(e.pnl for e in group if e.pnl <= 0)
        if losing_pnl < 0:
            profit_factor = (
                winning_pnl / abs(losing_pnl) if winning_pnl > 0 else 0.0
            )
        elif winning_pnl > 0:
            profit_factor = float("inf")
        else:
            profit_factor = 0.0

        avg_hold = sum(e.hold_days for e in group) / len(group)

        summaries.append(
            StrategyPnLSummary(
                strategy_name=strategy_name,
                total_trades=int(base["total_trades"]),
                win_rate_pct=float(base["win_rate_pct"]),
                total_pnl=float(base["total_pnl"]),
                expectancy=float(base["expectancy"]),
                profit_factor=profit_factor,
                best_trade=float(base["best_trade"]),
                worst_trade=float(base["worst_trade"]),
                avg_hold_days=avg_hold,
            )
        )

    summaries.sort(key=lambda s: s.total_pnl, reverse=True)
    return summaries
