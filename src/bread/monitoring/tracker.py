"""P&L tracker — aggregates portfolio snapshots into daily/weekly/monthly summaries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select

from bread.db.models import PortfolioSnapshot

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


@dataclass(frozen=True)
class DailySummary:
    date: date
    open_equity: float
    close_equity: float
    pnl: float
    pnl_pct: float
    open_positions: int
    high_equity: float
    low_equity: float


def get_daily_summaries(
    session: Session,
    start: date | None = None,
    end: date | None = None,
) -> list[DailySummary]:
    """Aggregate snapshots into daily summaries.

    Groups snapshots by date. For each day:
    - open_equity = first snapshot of the day
    - close_equity = last snapshot of the day
    - pnl = close_equity - open_equity
    - high/low = max/min equity across all snapshots that day
    """
    query = select(PortfolioSnapshot).order_by(PortfolioSnapshot.timestamp_utc.asc())
    snapshots = session.execute(query).scalars().all()

    if not snapshots:
        return []

    # Group by date
    by_date: dict[date, list[PortfolioSnapshot]] = {}
    for snap in snapshots:
        d = snap.timestamp_utc.date()
        by_date.setdefault(d, []).append(snap)

    summaries: list[DailySummary] = []
    for d, snaps in sorted(by_date.items()):
        if start is not None and d < start:
            continue
        if end is not None and d > end:
            continue

        open_eq = snaps[0].equity
        close_eq = snaps[-1].equity
        pnl = close_eq - open_eq
        pnl_pct = (pnl / open_eq * 100) if open_eq > 0 else 0.0

        summaries.append(DailySummary(
            date=d,
            open_equity=open_eq,
            close_equity=close_eq,
            pnl=pnl,
            pnl_pct=pnl_pct,
            open_positions=snaps[-1].open_positions,
            high_equity=max(s.equity for s in snaps),
            low_equity=min(s.equity for s in snaps),
        ))

    return summaries


def _aggregate_periods(
    summaries: list[DailySummary],
    key_fn: Callable[[DailySummary], str],
) -> list[tuple[str, float, float]]:
    """Group daily summaries by key_fn and compute open→close P&L per group."""
    grouped: dict[str, list[DailySummary]] = {}
    for s in summaries:
        label = key_fn(s)
        grouped.setdefault(label, []).append(s)

    result: list[tuple[str, float, float]] = []
    for label, days in sorted(grouped.items()):
        open_eq = days[0].open_equity
        close_eq = days[-1].close_equity
        pnl = close_eq - open_eq
        pnl_pct = (pnl / open_eq * 100) if open_eq > 0 else 0.0
        result.append((label, pnl, pnl_pct))
    return result


def get_period_pnl(
    session: Session,
    period: Literal["daily", "weekly", "monthly"],
) -> list[tuple[str, float, float]]:
    """Return (period_label, pnl, pnl_pct) tuples.

    - daily: last 30 days
    - weekly: last 12 weeks
    - monthly: last 12 months
    """
    today = date.today()

    if period == "daily":
        start = today - timedelta(days=30)
        summaries = get_daily_summaries(session, start=start)
        return [
            (s.date.isoformat(), s.pnl, s.pnl_pct)
            for s in summaries
        ]

    if period == "weekly":
        start = today - timedelta(weeks=12)
        summaries = get_daily_summaries(session, start=start)

        def _week_key(s: DailySummary) -> str:
            year, week, _ = s.date.isocalendar()
            return f"{year}-W{week:02d}"

        return _aggregate_periods(summaries, _week_key)

    # monthly
    start_date = today - timedelta(days=365)
    summaries = get_daily_summaries(session, start=start_date)
    return _aggregate_periods(
        summaries, lambda s: s.date.strftime("%Y-%m"),
    )


def get_drawdown_series(
    session: Session,
) -> list[tuple[date, float]]:
    """Return (date, drawdown_pct) series from portfolio snapshots.

    Computes rolling peak equity and current drawdown at each snapshot.
    Returns one entry per day (using end-of-day snapshot).
    """
    query = select(PortfolioSnapshot).order_by(PortfolioSnapshot.timestamp_utc.asc())
    snapshots = session.execute(query).scalars().all()

    if not snapshots:
        return []

    # Get last snapshot per day
    by_date: dict[date, PortfolioSnapshot] = {}
    for snap in snapshots:
        by_date[snap.timestamp_utc.date()] = snap  # last one wins

    peak = 0.0
    result: list[tuple[date, float]] = []
    for d in sorted(by_date):
        eq = by_date[d].equity
        peak = max(peak, eq)
        dd = ((peak - eq) / peak * 100) if peak > 0 else 0.0
        result.append((d, dd))

    return result
