"""Unit tests for HistoricalDataFeed."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import pandas as pd

from bread.backtest.data_feed import HistoricalDataFeed
from bread.core.config import AppConfig


def _make_config() -> AppConfig:
    return AppConfig(
        mode="paper",
        alpaca={"paper_api_key": "k", "paper_secret_key": "s"},
    )


def _make_raw_bars(start: str, periods: int) -> pd.DataFrame:
    """Create minimal OHLCV bars that pass through compute_indicators."""
    dates = pd.bdate_range(start=start, periods=periods, tz="UTC")
    close = [100.0 + i * 0.1 for i in range(periods)]
    return pd.DataFrame(
        {
            "open": [c - 0.5 for c in close],
            "high": [c + 1.0 for c in close],
            "low": [c - 1.0 for c in close],
            "close": close,
            "volume": [1_000_000] * periods,
        },
        index=pd.DatetimeIndex(dates, name="timestamp"),
    )


class TestLoadUniverse:
    def test_returns_enriched_data_for_valid_symbols(self) -> None:
        config = _make_config()
        provider = MagicMock()
        raw = _make_raw_bars("2023-01-02", 300)
        provider.get_bars_batch.return_value = {"SPY": raw}

        feed = HistoricalDataFeed(provider, config)
        start = date(2024, 1, 2)
        end = date(2024, 12, 31)
        result = feed.load_universe(["SPY"], start, end)

        provider.get_bars_batch.assert_called_once()
        assert isinstance(result, dict)

    def test_excludes_symbol_missing_from_batch(self) -> None:
        config = _make_config()
        provider = MagicMock()
        # Batch returns empty dict (symbol not available)
        provider.get_bars_batch.return_value = {}

        feed = HistoricalDataFeed(provider, config)
        result = feed.load_universe(["SPY"], date(2024, 1, 1), date(2024, 12, 31))

        assert "SPY" not in result
        assert len(result) == 0

    def test_excludes_symbol_on_insufficient_history(self) -> None:
        config = _make_config()
        provider = MagicMock()
        raw = _make_raw_bars("2024-01-02", 5)
        provider.get_bars_batch.return_value = {"SPY": raw}

        feed = HistoricalDataFeed(provider, config)
        result = feed.load_universe(["SPY"], date(2024, 1, 1), date(2024, 12, 31))

        assert "SPY" not in result

    def test_multiple_symbols_partial_failure(self) -> None:
        config = _make_config()
        provider = MagicMock()

        raw = _make_raw_bars("2023-01-02", 300)
        # BAD is missing from batch response (Alpaca couldn't find it)
        provider.get_bars_batch.return_value = {"SPY": raw, "QQQ": raw}

        feed = HistoricalDataFeed(provider, config)
        result = feed.load_universe(
            ["SPY", "BAD", "QQQ"], date(2023, 6, 1), date(2023, 12, 31)
        )

        assert "BAD" not in result
        provider.get_bars_batch.assert_called_once()

    def test_empty_symbols_list(self) -> None:
        config = _make_config()
        provider = MagicMock()
        provider.get_bars_batch.return_value = {}

        feed = HistoricalDataFeed(provider, config)
        result = feed.load_universe([], date(2024, 1, 1), date(2024, 12, 31))

        assert result == {}

    def test_warmup_extends_fetch_start(self) -> None:
        config = _make_config()
        provider = MagicMock()
        provider.get_bars_batch.return_value = {}

        feed = HistoricalDataFeed(provider, config)
        start = date(2024, 6, 1)
        end = date(2024, 12, 31)
        feed.load_universe(["SPY"], start, end)

        call_args = provider.get_bars_batch.call_args
        fetch_start = call_args[0][1]  # second positional arg
        assert fetch_start < start
        expected_offset = int(config.indicators.longest_window * 1.5)
        assert fetch_start == start - timedelta(days=expected_offset)
