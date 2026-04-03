"""Tests for risk.limits."""

from bread.risk.limits import (
    check_asset_class_exposure,
    check_daily_loss,
    check_drawdown,
    check_max_positions,
    check_pdt,
    check_position_concentration,
    check_weekly_loss,
)

ASSET_CLASSES = {
    "equity_broad": ["SPY", "QQQ", "IWM", "DIA"],
    "financials": ["XLF"],
    "technology": ["XLK"],
}


class TestCheckMaxPositions:
    def test_under_limit(self) -> None:
        passed, reason = check_max_positions(3, 5)
        assert passed is True
        assert reason == ""

    def test_at_limit(self) -> None:
        passed, reason = check_max_positions(5, 5)
        assert passed is False
        assert "max positions exceeded" in reason

    def test_over_limit(self) -> None:
        passed, reason = check_max_positions(6, 5)
        assert passed is False


class TestCheckPositionConcentration:
    def test_under_limit(self) -> None:
        passed, _ = check_position_concentration(1_000, 10_000, 0.20)
        assert passed is True

    def test_over_limit(self) -> None:
        passed, reason = check_position_concentration(3_000, 10_000, 0.20)
        assert passed is False
        assert "concentration" in reason

    def test_zero_equity(self) -> None:
        passed, _ = check_position_concentration(1_000, 0, 0.20)
        assert passed is False


class TestCheckAssetClassExposure:
    def test_under_limit(self) -> None:
        # One SPY position at 20%, adding QQQ at 19% -> 39% < 40%
        positions = [("SPY", 2_000)]
        passed, _ = check_asset_class_exposure(
            "QQQ", positions, 1_900, 10_000, ASSET_CLASSES, 0.40
        )
        assert passed is True

    def test_over_limit(self) -> None:
        # Two equity_broad at 20% each, adding third -> 60% > 40%
        positions = [("SPY", 2_000), ("QQQ", 2_000)]
        passed, reason = check_asset_class_exposure(
            "IWM", positions, 2_000, 10_000, ASSET_CLASSES, 0.40
        )
        assert passed is False
        assert "equity_broad" in reason

    def test_unknown_class_passes(self) -> None:
        # Symbol not in any class -> no constraint
        passed, _ = check_asset_class_exposure(
            "AAPL", [], 2_000, 10_000, ASSET_CLASSES, 0.40
        )
        assert passed is True

    def test_different_class_ok(self) -> None:
        # SPY (equity_broad) and XLF (financials) are different classes
        positions = [("SPY", 2_000)]
        passed, _ = check_asset_class_exposure(
            "XLF", positions, 2_000, 10_000, ASSET_CLASSES, 0.40
        )
        assert passed is True


class TestCheckDailyLoss:
    def test_no_loss(self) -> None:
        passed, _ = check_daily_loss(50.0, 10_000, 0.015)
        assert passed is True

    def test_within_limit(self) -> None:
        passed, _ = check_daily_loss(-100.0, 10_000, 0.015)
        assert passed is True

    def test_at_limit(self) -> None:
        passed, reason = check_daily_loss(-150.0, 10_000, 0.015)
        assert passed is False
        assert "daily loss" in reason

    def test_zero_equity(self) -> None:
        passed, _ = check_daily_loss(-50.0, 0, 0.015)
        assert passed is False


class TestCheckWeeklyLoss:
    def test_within_limit(self) -> None:
        passed, _ = check_weekly_loss(-200.0, 10_000, 0.03)
        assert passed is True

    def test_at_limit(self) -> None:
        passed, reason = check_weekly_loss(-300.0, 10_000, 0.03)
        assert passed is False
        assert "weekly loss" in reason


class TestCheckDrawdown:
    def test_no_drawdown(self) -> None:
        passed, _ = check_drawdown(10_000, 10_000, 0.07)
        assert passed is True

    def test_under_limit(self) -> None:
        passed, _ = check_drawdown(9_500, 10_000, 0.07)
        assert passed is True

    def test_at_limit(self) -> None:
        passed, reason = check_drawdown(9_300, 10_000, 0.07)
        assert passed is False
        assert "drawdown" in reason

    def test_zero_peak(self) -> None:
        passed, _ = check_drawdown(9_000, 0, 0.07)
        assert passed is False


class TestCheckPdt:
    def test_under_limit(self) -> None:
        passed, _ = check_pdt(2, 10_000, True)
        assert passed is True

    def test_at_limit(self) -> None:
        passed, reason = check_pdt(3, 10_000, True)
        assert passed is False
        assert "PDT" in reason

    def test_high_equity_exempt(self) -> None:
        passed, _ = check_pdt(5, 30_000, True)
        assert passed is True

    def test_disabled(self) -> None:
        passed, _ = check_pdt(5, 10_000, False)
        assert passed is True
