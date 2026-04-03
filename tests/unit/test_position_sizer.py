"""Tests for risk.position_sizer."""

from bread.risk.position_sizer import compute_position_size


class TestComputePositionSize:
    def test_standard_case(self) -> None:
        # equity=10K, risk=0.5%, stop=5%, price=$100
        # risk_dollars=50, position_value=1000, shares=10
        assert compute_position_size(10_000, 0.005, 0.05, 0.20, 100.0) == 10

    def test_max_position_cap(self) -> None:
        # equity=10K, risk=1%, stop=1%, price=$100
        # risk_dollars=100, position_value=10_000 -> capped at 20% = 2000 -> 20 shares
        assert compute_position_size(10_000, 0.01, 0.01, 0.20, 100.0) == 20

    def test_zero_equity(self) -> None:
        assert compute_position_size(0, 0.005, 0.05, 0.20, 100.0) == 0

    def test_negative_equity(self) -> None:
        assert compute_position_size(-1000, 0.005, 0.05, 0.20, 100.0) == 0

    def test_very_high_price(self) -> None:
        # equity=10K, position_value=1000, price=600_000 -> 0 shares
        assert compute_position_size(10_000, 0.005, 0.05, 0.20, 600_000.0) == 0

    def test_zero_stop_loss(self) -> None:
        assert compute_position_size(10_000, 0.005, 0, 0.20, 100.0) == 0

    def test_zero_price(self) -> None:
        assert compute_position_size(10_000, 0.005, 0.05, 0.20, 0) == 0

    def test_small_equity_rounds_down(self) -> None:
        # equity=500, risk=0.5%, stop=5%, price=$100
        # risk_dollars=2.5, position_value=50, shares=0
        assert compute_position_size(500, 0.005, 0.05, 0.20, 100.0) == 0

    def test_fractional_shares_truncated(self) -> None:
        # equity=10K, risk=0.5%, stop=5%, price=$450
        # risk_dollars=50, position_value=1000, shares=int(1000/450)=2
        assert compute_position_size(10_000, 0.005, 0.05, 0.20, 450.0) == 2
