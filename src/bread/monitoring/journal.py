"""Trade journal — query layer on OrderLog for round-trips and open positions."""

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


@dataclass(frozen=True)
class OpenPosition:
    """One unmatched BUY — a currently-open leg owned by a specific strategy."""

    symbol: str
    strategy_name: str
    qty: int
    entry_price: float
    current_price: float
    unrealized_pnl: float


def _pair_orders(
    rows: list[OrderLog],
) -> tuple[list[JournalEntry], dict[str, list[OrderLog]]]:
    """FIFO-pair BUYs with SELLs by (symbol, strategy_name).

    Two strategies can hold the same symbol simultaneously (see
    memory/project_multi_strategy_positions.md). Pairing by symbol alone
    would cross-attribute their trades: strategy_a's BUY could pair with
    strategy_b's SELL, polluting both strategies' per-strategy P&L. The
    engine logs every SELL with the opener's strategy_name, so pairing by
    (symbol, strategy_name) keeps each strategy's round-trips isolated.

    Returns (journal_entries, unmatched_buys_by_symbol). ``unmatched`` is
    still keyed on symbol alone — consumers (open-position builders)
    aggregate by symbol and attribute per-row via BUY.strategy_name.
    """
    buys: dict[tuple[str, str], list[OrderLog]] = {}
    sells: dict[tuple[str, str], list[OrderLog]] = {}
    for row in rows:
        key = (row.symbol, row.strategy_name)
        if row.side == "BUY":
            buys.setdefault(key, []).append(row)
        elif row.side == "SELL":
            sells.setdefault(key, []).append(row)

    entries: list[JournalEntry] = []
    consumed: dict[tuple[str, str], int] = {}

    # For each sell, match the earliest unmatched buy (FIFO) within the same
    # (symbol, strategy). If the candidate BUY is timestamped AFTER the SELL,
    # the SELL is orphan (its opening BUY predates our history); skip the
    # SELL but leave buy_idx alone — that BUY belongs to a later SELL.
    for key, sell_list in sells.items():
        sym, _ = key
        buy_list = buys.get(key, [])
        buy_idx = 0
        for sell_order in sell_list:
            if buy_idx >= len(buy_list):
                logger.debug("Orphan SELL for %s/%s — no unmatched BUY", sym, key[1])
                continue

            buy_order = buy_list[buy_idx]

            if (
                buy_order.filled_at_utc is not None
                and sell_order.filled_at_utc is not None
                and buy_order.filled_at_utc > sell_order.filled_at_utc
            ):
                logger.debug(
                    "Orphan SELL for %s/%s — BUY missing from history", sym, key[1]
                )
                continue

            buy_idx += 1

            if buy_order.filled_price is None or sell_order.filled_price is None:
                logger.debug("Missing fill price for %s/%s — skipping pair", sym, key[1])
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

            entries.append(JournalEntry(
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
            ))
        consumed[key] = buy_idx

    unmatched: dict[str, list[OrderLog]] = {}
    for key, buy_list in buys.items():
        sym, _ = key
        c = consumed.get(key, 0)
        if c < len(buy_list):
            unmatched.setdefault(sym, []).extend(buy_list[c:])

    return entries, unmatched


def _fetch_filled_rows(session: Session) -> list[OrderLog]:
    query = (
        select(OrderLog)
        .where(OrderLog.status == "FILLED")
        .order_by(OrderLog.filled_at_utc.asc())
    )
    return list(session.execute(query).scalars().all())


def get_journal(
    session: Session,
    *,
    start: date | None = None,
    end: date | None = None,
    strategy: str | None = None,
    symbol: str | None = None,
    limit: int = 10_000,
) -> list[JournalEntry]:
    """Query completed round-trip trades from OrderLog.

    Default limit is deliberately high so summary stats computed on the
    returned list aren't silently distorted by truncation.
    """
    entries, _ = _pair_orders(_fetch_filled_rows(session))

    if start is not None:
        entries = [e for e in entries if e.exit_date >= start]
    if end is not None:
        entries = [e for e in entries if e.exit_date <= end]
    if strategy is not None:
        entries = [e for e in entries if e.strategy_name == strategy]
    if symbol is not None:
        entries = [e for e in entries if e.symbol == symbol]

    entries.sort(key=lambda e: e.exit_date, reverse=True)
    return entries[:limit]


def _build_open_positions(
    unmatched: dict[str, list[OrderLog]],
    prices: dict[str, float],
) -> list[OpenPosition]:
    """Turn unmatched BUYs into OpenPositions using injected current prices.

    Symbols without a price entry are skipped (reconcile gap or broker
    unavailable). One entry per unmatched BUY row — consumers aggregate.
    """
    out: list[OpenPosition] = []
    for sym, buy_list in unmatched.items():
        price = prices.get(sym)
        if price is None:
            logger.debug("No current price for %s — skipping unrealized calc", sym)
            continue
        for b in buy_list:
            if b.filled_price is None:
                continue
            entry_price = float(b.filled_price)
            out.append(OpenPosition(
                symbol=sym,
                strategy_name=b.strategy_name,
                qty=b.qty,
                entry_price=entry_price,
                current_price=price,
                unrealized_pnl=round((price - entry_price) * b.qty, 2),
            ))
    return out


def get_open_positions(
    session: Session,
    current_prices: dict[str, float],
) -> list[OpenPosition]:
    """Return one OpenPosition per unmatched BUY."""
    _, unmatched = _pair_orders(_fetch_filled_rows(session))
    return _build_open_positions(unmatched, current_prices)


def get_journal_summary(entries: list[JournalEntry]) -> dict[str, float | int]:
    """Compute realized-only summary stats from journal entries."""
    if not entries:
        return {
            "win_rate_pct": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "expectancy": 0.0,
            "realized_pnl": 0.0,
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
    realized_pnl = sum(e.pnl for e in entries)
    expectancy = realized_pnl / total

    return {
        "win_rate_pct": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": expectancy,
        "realized_pnl": realized_pnl,
        "total_trades": total,
        "best_trade": max(e.pnl for e in entries),
        "worst_trade": min(e.pnl for e in entries),
    }


@dataclass(frozen=True)
class StrategyPnLSummary:
    """Per-strategy P&L breakdown — surfaced on the dashboard leaderboard."""

    strategy_name: str
    total_trades: int
    win_rate_pct: float
    realized_pnl: float
    unrealized_pnl: float
    total_pnl: float  # realized + unrealized
    open_positions: int  # distinct open symbols for this strategy
    expectancy: float
    profit_factor: float  # sum(wins) / |sum(losses)|; inf if zero losses
    best_trade: float
    worst_trade: float
    avg_hold_days: float


def get_all_strategies_summary(
    session: Session,
    *,
    days: int = 365,
    current_prices: dict[str, float] | None = None,
) -> list[StrategyPnLSummary]:
    """Group completed trades by strategy and compute per-strategy summary stats.

    Realized path: completed round-trips whose exit_date falls in the lookback
    window. Unrealized path: sum of OpenPosition.unrealized_pnl per strategy
    across the full history (a position opened before the window but still
    open today still has P&L-on-the-books). Gating unchanged — only strategies
    with at least one completed round-trip in the window appear.

    Sorted by total_pnl (realized + unrealized) descending.
    """
    rows = _fetch_filled_rows(session)
    entries, unmatched = _pair_orders(rows)

    start = date.today() - timedelta(days=days)
    entries = [e for e in entries if e.exit_date >= start]

    opens = _build_open_positions(unmatched, current_prices or {})
    unreal_by_strat: dict[str, float] = {}
    open_symbols_by_strat: dict[str, set[str]] = {}
    for p in opens:
        unreal_by_strat.setdefault(p.strategy_name, 0.0)
        unreal_by_strat[p.strategy_name] += p.unrealized_pnl
        open_symbols_by_strat.setdefault(p.strategy_name, set()).add(p.symbol)

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

        realized = float(base["realized_pnl"])
        unrealized = round(unreal_by_strat.get(strategy_name, 0.0), 2)
        open_count = len(open_symbols_by_strat.get(strategy_name, set()))

        summaries.append(
            StrategyPnLSummary(
                strategy_name=strategy_name,
                total_trades=int(base["total_trades"]),
                win_rate_pct=float(base["win_rate_pct"]),
                realized_pnl=realized,
                unrealized_pnl=unrealized,
                total_pnl=round(realized + unrealized, 2),
                open_positions=open_count,
                expectancy=float(base["expectancy"]),
                profit_factor=profit_factor,
                best_trade=float(base["best_trade"]),
                worst_trade=float(base["worst_trade"]),
                avg_hold_days=avg_hold,
            )
        )

    summaries.sort(key=lambda s: s.total_pnl, reverse=True)
    return summaries
