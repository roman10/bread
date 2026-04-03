"""Fixed fractional position sizing."""

from __future__ import annotations


def compute_position_size(
    equity: float,
    risk_pct: float,
    stop_loss_pct: float,
    max_position_pct: float,
    price: float,
) -> int:
    """Compute number of shares to buy using fixed fractional sizing.

    Formula: position_value = (equity * risk_pct) / stop_loss_pct,
    capped at equity * max_position_pct.

    Returns number of whole shares (>= 0).
    """
    if equity <= 0 or price <= 0 or stop_loss_pct <= 0 or risk_pct <= 0:
        return 0

    risk_dollars = equity * risk_pct
    position_value = risk_dollars / stop_loss_pct
    max_value = equity * max_position_pct
    capped_value = min(position_value, max_value)
    shares = int(capped_value / price)
    return max(shares, 0)
