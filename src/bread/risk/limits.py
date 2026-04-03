"""Hard limits and circuit breakers. Each check is stateless and pure."""

from __future__ import annotations


def check_max_positions(open_count: int, max_positions: int) -> tuple[bool, str]:
    """Reject if at or above the position limit."""
    if open_count >= max_positions:
        return False, f"max positions exceeded ({open_count}/{max_positions})"
    return True, ""


def check_position_concentration(
    position_value: float,
    equity: float,
    max_position_pct: float,
) -> tuple[bool, str]:
    """Reject if a single position exceeds the concentration limit."""
    if equity <= 0:
        return False, "equity is zero or negative"
    ratio = position_value / equity
    if ratio > max_position_pct:
        return False, (
            f"position concentration {ratio:.1%} exceeds limit {max_position_pct:.0%}"
        )
    return True, ""


def check_asset_class_exposure(
    symbol: str,
    positions: list[tuple[str, float]],
    proposed_value: float,
    equity: float,
    asset_classes: dict[str, list[str]],
    max_asset_class_pct: float,
) -> tuple[bool, str]:
    """Reject if adding this position pushes an asset class over the limit.

    Args:
        positions: list of (symbol, market_value) for currently open positions.
    """
    if equity <= 0:
        return False, "equity is zero or negative"

    # Find which asset classes the symbol belongs to
    symbol_classes = [
        cls_name
        for cls_name, members in asset_classes.items()
        if symbol in members
    ]

    # If symbol not in any class, no concentration constraint
    if not symbol_classes:
        return True, ""

    for cls_name in symbol_classes:
        members = asset_classes[cls_name]
        existing_value = sum(val for sym, val in positions if sym in members)
        total = existing_value + proposed_value
        ratio = total / equity
        if ratio > max_asset_class_pct:
            return False, (
                f"asset class '{cls_name}' exposure {ratio:.1%} "
                f"exceeds limit {max_asset_class_pct:.0%}"
            )
    return True, ""


def check_daily_loss(
    daily_pnl: float,
    equity: float,
    max_daily_loss_pct: float,
) -> tuple[bool, str]:
    """Halt trading if daily loss exceeds limit."""
    if equity <= 0:
        return False, "equity is zero or negative"
    if daily_pnl < 0 and abs(daily_pnl) / equity >= max_daily_loss_pct:
        loss_pct = abs(daily_pnl) / equity
        return False, (
            f"daily loss {loss_pct:.2%} exceeds limit {max_daily_loss_pct:.1%}"
        )
    return True, ""


def check_weekly_loss(
    weekly_pnl: float,
    equity: float,
    max_weekly_loss_pct: float,
) -> tuple[bool, str]:
    """Halt trading if weekly loss exceeds limit."""
    if equity <= 0:
        return False, "equity is zero or negative"
    if weekly_pnl < 0 and abs(weekly_pnl) / equity >= max_weekly_loss_pct:
        loss_pct = abs(weekly_pnl) / equity
        return False, (
            f"weekly loss {loss_pct:.2%} exceeds limit {max_weekly_loss_pct:.1%}"
        )
    return True, ""


def check_drawdown(
    current_equity: float,
    peak_equity: float,
    max_drawdown_pct: float,
) -> tuple[bool, str]:
    """Halt all trading if drawdown from peak exceeds limit."""
    if peak_equity <= 0:
        return False, "peak equity is zero or negative"
    drawdown = (peak_equity - current_equity) / peak_equity
    if drawdown >= max_drawdown_pct:
        return False, (
            f"drawdown {drawdown:.2%} exceeds limit {max_drawdown_pct:.0%}"
        )
    return True, ""


def check_pdt(
    day_trade_count: int,
    equity: float,
    pdt_enabled: bool,
) -> tuple[bool, str]:
    """Block the 4th day trade for accounts under $25K (PDT rule)."""
    if not pdt_enabled:
        return True, ""
    if equity >= 25_000:
        return True, ""
    if day_trade_count >= 3:
        return False, (
            f"PDT guard: {day_trade_count} day trades in 5 days, "
            f"account equity ${equity:,.0f} < $25,000"
        )
    return True, ""
