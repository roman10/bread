"""Unit tests for AlpacaDataProvider."""

from __future__ import annotations

import pandas as pd

from bread.data.alpaca_data import AlpacaDataProvider


class TestNormalize:
    def test_tz_aware_timestamps_not_double_localized(self) -> None:
        """Regression: _normalize must handle already tz-aware UTC timestamps."""
        idx = pd.DatetimeIndex(
            pd.date_range("2025-01-02", periods=5, freq="B", tz="UTC"),
            name="timestamp",
        )
        df = pd.DataFrame(
            {
                "open": [1.0] * 5,
                "high": [2.0] * 5,
                "low": [0.5] * 5,
                "close": [1.5] * 5,
                "volume": [100] * 5,
            },
            index=idx,
        )
        result = AlpacaDataProvider._normalize(df, "TEST")
        assert result.index.tz is not None
        assert len(result) == 5

    def test_tz_naive_timestamps_get_localized(self) -> None:
        """Tz-naive timestamps should be localized to UTC."""
        idx = pd.DatetimeIndex(
            pd.date_range("2025-01-02", periods=5, freq="B"),
            name="timestamp",
        )
        df = pd.DataFrame(
            {
                "open": [1.0] * 5,
                "high": [2.0] * 5,
                "low": [0.5] * 5,
                "close": [1.5] * 5,
                "volume": [100] * 5,
            },
            index=idx,
        )
        result = AlpacaDataProvider._normalize(df, "TEST")
        assert str(result.index.tz) == "UTC"
