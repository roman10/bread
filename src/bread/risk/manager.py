"""Risk management engine orchestrating position sizing and validation."""

from __future__ import annotations

from bread.core.config import RiskSettings
from bread.core.models import Position, Signal
from bread.risk.position_sizer import compute_position_size
from bread.risk.validators import ValidationResult, validate_signal


class RiskManager:
    def __init__(self, config: RiskSettings) -> None:
        self._config = config

    def evaluate(
        self,
        signal: Signal,
        price: float,
        buying_power: float,
        equity: float,
        positions: list[Position],
        peak_equity: float,
        daily_pnl: float,
        weekly_pnl: float,
        day_trade_count: int,
    ) -> tuple[int, ValidationResult]:
        """Size the position, then validate.

        Returns (shares, validation_result).
        """
        shares = compute_position_size(
            equity=equity,
            risk_pct=self._config.risk_pct_per_trade,
            stop_loss_pct=signal.stop_loss_pct,
            max_position_pct=self._config.max_position_pct,
            price=price,
        )

        result = validate_signal(
            signal=signal,
            position_size=shares,
            price=price,
            buying_power=buying_power,
            equity=equity,
            positions=positions,
            config=self._config,
            peak_equity=peak_equity,
            daily_pnl=daily_pnl,
            weekly_pnl=weekly_pnl,
            day_trade_count=day_trade_count,
        )

        return shares, result
