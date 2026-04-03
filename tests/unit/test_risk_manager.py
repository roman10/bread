"""Tests for risk.manager."""

from __future__ import annotations

from datetime import UTC, date, datetime

from bread.core.config import RiskSettings
from bread.core.models import Position, Signal, SignalDirection
from bread.risk.manager import RiskManager


def _make_signal(symbol: str = "SPY", stop_loss_pct: float = 0.05) -> Signal:
    return Signal(
        symbol=symbol,
        direction=SignalDirection.BUY,
        strength=0.5,
        stop_loss_pct=stop_loss_pct,
        strategy_name="test",
        reason="test signal",
        timestamp=datetime.now(UTC),
    )


def _make_position(symbol: str = "QQQ") -> Position:
    return Position(
        symbol=symbol,
        qty=10,
        entry_price=100.0,
        stop_loss_price=95.0,
        take_profit_price=110.0,
        broker_order_id="test-123",
        strategy_name="test",
        entry_date=date.today(),
    )


class TestRiskManager:
    def test_happy_path(self) -> None:
        rm = RiskManager(RiskSettings())
        shares, result = rm.evaluate(
            signal=_make_signal(),
            price=100.0,
            buying_power=5_000.0,
            equity=10_000.0,
            positions=[],
            peak_equity=10_000.0,
            daily_pnl=0.0,
            weekly_pnl=0.0,
            day_trade_count=0,
        )
        assert shares > 0
        assert result.approved is True

    def test_rejection_over_concentration(self) -> None:
        rm = RiskManager(RiskSettings())
        # 5 positions already held
        positions = [_make_position(f"SYM{i}") for i in range(5)]
        shares, result = rm.evaluate(
            signal=_make_signal(),
            price=100.0,
            buying_power=5_000.0,
            equity=10_000.0,
            positions=positions,
            peak_equity=10_000.0,
            daily_pnl=0.0,
            weekly_pnl=0.0,
            day_trade_count=0,
        )
        assert shares > 0  # sizing still computes
        assert result.approved is False

    def test_zero_sizing_tiny_equity(self) -> None:
        rm = RiskManager(RiskSettings())
        shares, result = rm.evaluate(
            signal=_make_signal(),
            price=500.0,  # SPY-like price, tiny equity
            buying_power=100.0,
            equity=100.0,
            positions=[],
            peak_equity=100.0,
            daily_pnl=0.0,
            weekly_pnl=0.0,
            day_trade_count=0,
        )
        assert shares == 0
        assert result.approved is False
        assert "too small" in result.rejections[0]

    def test_drawdown_rejection(self) -> None:
        rm = RiskManager(RiskSettings())
        shares, result = rm.evaluate(
            signal=_make_signal(),
            price=100.0,
            buying_power=5_000.0,
            equity=9_200.0,
            positions=[],
            peak_equity=10_000.0,
            daily_pnl=0.0,
            weekly_pnl=0.0,
            day_trade_count=0,
        )
        assert result.approved is False
        assert "drawdown" in result.rejections[0]
