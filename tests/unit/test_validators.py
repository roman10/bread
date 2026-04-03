"""Tests for risk.validators."""

from __future__ import annotations

from datetime import UTC, date, datetime

from bread.core.config import RiskSettings
from bread.core.models import Position, Signal, SignalDirection
from bread.risk.validators import validate_signal


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


def _make_position(symbol: str = "QQQ", qty: int = 10, entry_price: float = 100.0) -> Position:
    return Position(
        symbol=symbol,
        qty=qty,
        entry_price=entry_price,
        stop_loss_price=95.0,
        take_profit_price=110.0,
        broker_order_id="test-123",
        strategy_name="test",
        entry_date=date.today(),
    )


DEFAULT_CONFIG = RiskSettings()


class TestValidateSignal:
    def test_all_pass(self) -> None:
        result = validate_signal(
            signal=_make_signal(),
            position_size=10,
            price=100.0,
            buying_power=5_000.0,
            equity=10_000.0,
            positions=[],
            config=DEFAULT_CONFIG,
            peak_equity=10_000.0,
            daily_pnl=0.0,
            weekly_pnl=0.0,
            day_trade_count=0,
        )
        assert result.approved is True
        assert result.rejections == []

    def test_zero_position_size(self) -> None:
        result = validate_signal(
            signal=_make_signal(),
            position_size=0,
            price=100.0,
            buying_power=5_000.0,
            equity=10_000.0,
            positions=[],
            config=DEFAULT_CONFIG,
            peak_equity=10_000.0,
            daily_pnl=0.0,
            weekly_pnl=0.0,
            day_trade_count=0,
        )
        assert result.approved is False
        assert "too small" in result.rejections[0]

    def test_insufficient_buying_power(self) -> None:
        result = validate_signal(
            signal=_make_signal(),
            position_size=10,
            price=100.0,
            buying_power=500.0,  # need 1000
            equity=10_000.0,
            positions=[],
            config=DEFAULT_CONFIG,
            peak_equity=10_000.0,
            daily_pnl=0.0,
            weekly_pnl=0.0,
            day_trade_count=0,
        )
        assert result.approved is False
        assert "buying power" in result.rejections[0]

    def test_max_positions_exceeded(self) -> None:
        positions = [_make_position(f"SYM{i}") for i in range(5)]
        result = validate_signal(
            signal=_make_signal(),
            position_size=10,
            price=100.0,
            buying_power=5_000.0,
            equity=10_000.0,
            positions=positions,
            config=DEFAULT_CONFIG,
            peak_equity=10_000.0,
            daily_pnl=0.0,
            weekly_pnl=0.0,
            day_trade_count=0,
        )
        assert result.approved is False
        assert "max positions" in result.rejections[0]

    def test_asset_class_exposure(self) -> None:
        # Two equity_broad positions at 20% each, adding SPY (third)
        positions = [
            _make_position("QQQ", qty=20, entry_price=100.0),  # 2000
            _make_position("IWM", qty=20, entry_price=100.0),  # 2000
        ]
        result = validate_signal(
            signal=_make_signal("SPY"),
            position_size=20,
            price=100.0,
            buying_power=5_000.0,
            equity=10_000.0,
            positions=positions,
            config=DEFAULT_CONFIG,
            peak_equity=10_000.0,
            daily_pnl=0.0,
            weekly_pnl=0.0,
            day_trade_count=0,
        )
        assert result.approved is False
        assert "asset class" in result.rejections[0]

    def test_daily_loss_halt(self) -> None:
        result = validate_signal(
            signal=_make_signal(),
            position_size=10,
            price=100.0,
            buying_power=5_000.0,
            equity=10_000.0,
            positions=[],
            config=DEFAULT_CONFIG,
            peak_equity=10_000.0,
            daily_pnl=-200.0,  # 2% > 1.5% limit
            weekly_pnl=0.0,
            day_trade_count=0,
        )
        assert result.approved is False
        assert "daily loss" in result.rejections[0]

    def test_drawdown_halt(self) -> None:
        result = validate_signal(
            signal=_make_signal(),
            position_size=10,
            price=100.0,
            buying_power=5_000.0,
            equity=9_200.0,  # 8% drawdown from 10K
            positions=[],
            config=DEFAULT_CONFIG,
            peak_equity=10_000.0,
            daily_pnl=0.0,
            weekly_pnl=0.0,
            day_trade_count=0,
        )
        assert result.approved is False
        assert "drawdown" in result.rejections[0]

    def test_pdt_blocked(self) -> None:
        result = validate_signal(
            signal=_make_signal(),
            position_size=10,
            price=100.0,
            buying_power=5_000.0,
            equity=10_000.0,
            positions=[],
            config=DEFAULT_CONFIG,
            peak_equity=10_000.0,
            daily_pnl=0.0,
            weekly_pnl=0.0,
            day_trade_count=3,  # at PDT limit
        )
        assert result.approved is False
        assert "PDT" in result.rejections[0]

    def test_short_circuit_stops_at_first_failure(self) -> None:
        # Both position size=0 AND buying_power insufficient, but only first reason reported
        result = validate_signal(
            signal=_make_signal(),
            position_size=0,
            price=100.0,
            buying_power=0.0,
            equity=10_000.0,
            positions=[],
            config=DEFAULT_CONFIG,
            peak_equity=10_000.0,
            daily_pnl=0.0,
            weekly_pnl=0.0,
            day_trade_count=0,
        )
        assert result.approved is False
        assert len(result.rejections) == 1
        assert "too small" in result.rejections[0]
