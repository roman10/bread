"""Pre-trade validation chain. Every BUY signal passes through before becoming an order."""

from __future__ import annotations

from dataclasses import dataclass, field

from bread.core.config import RiskSettings
from bread.core.models import Position, Signal
from bread.risk.limits import (
    check_asset_class_exposure,
    check_daily_loss,
    check_drawdown,
    check_max_positions,
    check_pdt,
    check_position_concentration,
    check_weekly_loss,
)


@dataclass(frozen=True)
class ValidationResult:
    approved: bool
    rejections: list[str] = field(default_factory=list)


def validate_signal(
    signal: Signal,
    position_size: int,
    price: float,
    buying_power: float,
    equity: float,
    positions: list[Position],
    config: RiskSettings,
    peak_equity: float,
    daily_pnl: float,
    weekly_pnl: float,
    day_trade_count: int,
) -> ValidationResult:
    """Run all validators in order. Short-circuit on first failure."""
    # 1. Position size > 0
    if position_size <= 0:
        return ValidationResult(approved=False, rejections=["position too small to trade"])

    proposed_value = position_size * price

    # 2. Buying power
    if buying_power < proposed_value:
        return ValidationResult(
            approved=False,
            rejections=[
                f"insufficient buying power: ${buying_power:,.2f} < ${proposed_value:,.2f}"
            ],
        )

    # 3. Position limit
    passed, reason = check_max_positions(len(positions), config.max_positions)
    if not passed:
        return ValidationResult(approved=False, rejections=[reason])

    # 4a. Single position concentration
    passed, reason = check_position_concentration(proposed_value, equity, config.max_position_pct)
    if not passed:
        return ValidationResult(approved=False, rejections=[reason])

    # 4b. Asset class exposure
    position_values = [(p.symbol, p.qty * p.entry_price) for p in positions]
    passed, reason = check_asset_class_exposure(
        signal.symbol,
        position_values,
        proposed_value,
        equity,
        config.asset_classes,
        config.max_asset_class_pct,
    )
    if not passed:
        return ValidationResult(approved=False, rejections=[reason])

    # 5a. Daily loss
    passed, reason = check_daily_loss(daily_pnl, equity, config.max_daily_loss_pct)
    if not passed:
        return ValidationResult(approved=False, rejections=[reason])

    # 5b. Weekly loss
    passed, reason = check_weekly_loss(weekly_pnl, equity, config.max_weekly_loss_pct)
    if not passed:
        return ValidationResult(approved=False, rejections=[reason])

    # 5c. Max drawdown
    passed, reason = check_drawdown(equity, peak_equity, config.max_drawdown_pct)
    if not passed:
        return ValidationResult(approved=False, rejections=[reason])

    # 6. PDT guard
    passed, reason = check_pdt(day_trade_count, equity, config.pdt_enabled)
    if not passed:
        return ValidationResult(approved=False, rejections=[reason])

    return ValidationResult(approved=True)
