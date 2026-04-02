"""Unit tests for technical indicator computation."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from bread.core.config import IndicatorSettings
from bread.core.exceptions import InsufficientHistoryError
from bread.data.indicators import compute_indicators, get_indicator_columns


def _make_ohlcv(rows: int = 250) -> pd.DataFrame:
    """Create a synthetic OHLCV DataFrame with realistic-ish data."""
    np.random.seed(42)
    dates = pd.bdate_range(start=date(2024, 1, 2), periods=rows, tz="UTC")
    close = 100 + np.cumsum(np.random.randn(rows) * 0.5)
    return pd.DataFrame(
        {
            "open": close - np.random.rand(rows) * 0.5,
            "high": close + np.random.rand(rows) * 1.0,
            "low": close - np.random.rand(rows) * 1.0,
            "close": close,
            "volume": np.random.randint(500_000, 2_000_000, size=rows),
        },
        index=pd.DatetimeIndex(dates[:rows], name="timestamp"),
    )


@pytest.fixture()
def default_settings() -> IndicatorSettings:
    return IndicatorSettings()


class TestComputeIndicators:
    def test_all_indicator_columns_present(
        self, default_settings: IndicatorSettings
    ) -> None:
        df = _make_ohlcv(250)
        result = compute_indicators(df, default_settings)
        expected = get_indicator_columns(default_settings)
        for col in expected:
            assert col in result.columns, f"Missing column: {col}"

    def test_no_null_indicator_values(
        self, default_settings: IndicatorSettings
    ) -> None:
        df = _make_ohlcv(250)
        result = compute_indicators(df, default_settings)
        indicator_cols = get_indicator_columns(default_settings)
        for col in indicator_cols:
            assert not result[col].isna().any(), f"NaN found in {col}"

    def test_ohlcv_columns_preserved(
        self, default_settings: IndicatorSettings
    ) -> None:
        df = _make_ohlcv(250)
        result = compute_indicators(df, default_settings)
        for col in ["open", "high", "low", "close", "volume"]:
            assert col in result.columns

    def test_insufficient_history_raises(
        self, default_settings: IndicatorSettings
    ) -> None:
        df = _make_ohlcv(10)  # way too few rows
        with pytest.raises(InsufficientHistoryError):
            compute_indicators(df, default_settings)

    def test_sma_spot_check(self, default_settings: IndicatorSettings) -> None:
        df = _make_ohlcv(250)
        result = compute_indicators(df, default_settings)
        # SMA 20 at the last row should equal the mean of the last 20 closes
        last_20_close = df["close"].iloc[-20:].mean()
        computed_sma = result["sma_20"].iloc[-1]
        assert abs(computed_sma - last_20_close) < 0.01

    def test_indicator_count_matches(
        self, default_settings: IndicatorSettings
    ) -> None:
        cols = get_indicator_columns(default_settings)
        assert len(cols) == 14


class TestGetIndicatorColumns:
    def test_dynamic_naming(self) -> None:
        settings = IndicatorSettings(sma_periods=[10, 30], rsi_period=7)
        cols = get_indicator_columns(settings)
        assert "sma_10" in cols
        assert "sma_30" in cols
        assert "rsi_7" in cols
        assert "sma_20" not in cols  # not in custom periods
