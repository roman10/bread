"""Unit tests for AlpacaDataProvider."""

from __future__ import annotations

import pandas as pd

from bread.data.alpaca_data import AlpacaDataProvider


def _make_ohlcv(start: str, periods: int, tz: str | None = "UTC") -> pd.DataFrame:
    """Helper to create a simple OHLCV DataFrame."""
    idx = pd.DatetimeIndex(
        pd.date_range(start, periods=periods, freq="B", tz=tz),
        name="timestamp",
    )
    return pd.DataFrame(
        {
            "open": [1.0] * periods,
            "high": [2.0] * periods,
            "low": [0.5] * periods,
            "close": [1.5] * periods,
            "volume": [100] * periods,
        },
        index=idx,
    )


class TestNormalize:
    def test_tz_aware_timestamps_not_double_localized(self) -> None:
        """Regression: _normalize must handle already tz-aware UTC timestamps."""
        df = _make_ohlcv("2025-01-02", 5)
        result = AlpacaDataProvider._normalize(df, "TEST")
        assert result.index.tz is not None
        assert len(result) == 5

    def test_tz_naive_timestamps_get_localized(self) -> None:
        """Tz-naive timestamps should be localized to UTC."""
        df = _make_ohlcv("2025-01-02", 5, tz=None)
        result = AlpacaDataProvider._normalize(df, "TEST")
        assert str(result.index.tz) == "UTC"


class TestSplitBatch:
    def _make_multi_index_df(self, symbols: list[str], periods: int = 5) -> pd.DataFrame:
        """Create a MultiIndex (symbol, timestamp) DataFrame like Alpaca returns."""
        frames = []
        for sym in symbols:
            dates = pd.date_range("2025-01-02", periods=periods, freq="B", tz="UTC")
            df = pd.DataFrame(
                {
                    "open": [1.0] * periods,
                    "high": [2.0] * periods,
                    "low": [0.5] * periods,
                    "close": [1.5] * periods,
                    "volume": [100] * periods,
                },
                index=pd.MultiIndex.from_arrays(
                    [[sym] * periods, dates], names=["symbol", "timestamp"]
                ),
            )
            frames.append(df)
        return pd.concat(frames)

    def test_splits_multi_symbol_response(self) -> None:
        df = self._make_multi_index_df(["SPY", "QQQ"])
        result = AlpacaDataProvider._split_batch(
            AlpacaDataProvider, df, ["SPY", "QQQ"]  # type: ignore[arg-type]
        )
        assert set(result.keys()) == {"SPY", "QQQ"}
        for sym_df in result.values():
            assert not isinstance(sym_df.index, pd.MultiIndex)
            assert len(sym_df) == 5
            assert sym_df.index.name == "timestamp"

    def test_handles_partial_response(self) -> None:
        """If a symbol is missing from the response, it's omitted with a warning."""
        df = self._make_multi_index_df(["SPY"])
        result = AlpacaDataProvider._split_batch(
            AlpacaDataProvider, df, ["SPY", "MISSING"]  # type: ignore[arg-type]
        )
        assert "SPY" in result
        assert "MISSING" not in result

    def test_handles_single_symbol_non_multiindex(self) -> None:
        """Batch of 1 may return a regular (non-MultiIndex) DataFrame."""
        df = _make_ohlcv("2025-01-02", 5)
        result = AlpacaDataProvider._split_batch(
            AlpacaDataProvider, df, ["SPY"]  # type: ignore[arg-type]
        )
        assert "SPY" in result
        assert len(result["SPY"]) == 5
