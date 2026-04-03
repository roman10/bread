"""Unit tests for domain models."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bread.core.models import Signal, SignalDirection


class TestSignalDirection:
    def test_buy_value(self) -> None:
        assert SignalDirection.BUY == "BUY"

    def test_sell_value(self) -> None:
        assert SignalDirection.SELL == "SELL"


class TestSignal:
    def _make_signal(self, **overrides) -> Signal:  # type: ignore[no-untyped-def]
        defaults = {
            "symbol": "SPY",
            "direction": SignalDirection.BUY,
            "strength": 0.5,
            "stop_loss_pct": 0.05,
            "strategy_name": "test",
            "reason": "test reason",
            "timestamp": datetime.now(UTC),
        }
        defaults.update(overrides)
        return Signal(**defaults)

    def test_signal_is_frozen(self) -> None:
        sig = self._make_signal()
        with pytest.raises(AttributeError):
            sig.symbol = "QQQ"  # type: ignore[misc]

    def test_valid_signal(self) -> None:
        sig = self._make_signal()
        assert sig.symbol == "SPY"
        assert sig.strength == 0.5

    def test_strength_too_high(self) -> None:
        with pytest.raises(ValueError, match="strength"):
            self._make_signal(strength=1.5)

    def test_strength_too_low(self) -> None:
        with pytest.raises(ValueError, match="strength"):
            self._make_signal(strength=-0.1)

    def test_stop_loss_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="stop_loss_pct"):
            self._make_signal(stop_loss_pct=0)

    def test_stop_loss_negative(self) -> None:
        with pytest.raises(ValueError, match="stop_loss_pct"):
            self._make_signal(stop_loss_pct=-0.01)

    def test_boundary_strength_zero(self) -> None:
        sig = self._make_signal(strength=0.0)
        assert sig.strength == 0.0

    def test_boundary_strength_one(self) -> None:
        sig = self._make_signal(strength=1.0)
        assert sig.strength == 1.0
