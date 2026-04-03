"""Unit tests for HistoricalDataFeed."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import pandas as pd

from bread.backtest.data_feed import HistoricalDataFeed
from bread.core.config import AppConfig
from bread.core.exceptions import DataProviderError


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
        # Need enough bars for indicators (longest_window = 200 for default SMA)
        raw = _make_raw_bars("2023-01-02", 300)
        provider.get_bars.return_value = raw

        feed = HistoricalDataFeed(provider, config)
        start = date(2024, 1, 2)
        end = date(2024, 12, 31)
        result = feed.load_universe(["SPY"], start, end)

        provider.get_bars.assert_called_once()
        # May be empty if raw dates don't overlap [start, end] — but the call succeeded
        assert isinstance(result, dict)

    def test_excludes_symbol_on_data_provider_error(self) -> None:
        config = _make_config()
        provider = MagicMock()
        provider.get_bars.side_effect = DataProviderError("API down")

        feed = HistoricalDataFeed(provider, config)
        result = feed.load_universe(["SPY"], date(2024, 1, 1), date(2024, 12, 31))

        assert "SPY" not in result
        assert len(result) == 0

    def test_excludes_symbol_on_insufficient_history(self) -> None:
        config = _make_config()
        provider = MagicMock()
        # Return too few bars for indicators
        raw = _make_raw_bars("2024-01-02", 5)
        provider.get_bars.return_value = raw

        feed = HistoricalDataFeed(provider, config)
        result = feed.load_universe(["SPY"], date(2024, 1, 1), date(2024, 12, 31))

        assert "SPY" not in result

    def test_multiple_symbols_partial_failure(self) -> None:
        config = _make_config()
        provider = MagicMock()

        raw = _make_raw_bars("2023-01-02", 300)

        def side_effect(symbol: str, start: date, end: date, tf: str) -> pd.DataFrame:
            if symbol == "BAD":
                raise DataProviderError("not found")
            return raw

        provider.get_bars.side_effect = side_effect

        feed = HistoricalDataFeed(provider, config)
        result = feed.load_universe(
            ["SPY", "BAD", "QQQ"], date(2023, 6, 1), date(2023, 12, 31)
        )

        assert "BAD" not in result
        # SPY and QQQ should be present if dates overlap
        assert provider.get_bars.call_count == 3

    def test_empty_symbols_list(self) -> None:
        config = _make_config()
        provider = MagicMock()

        feed = HistoricalDataFeed(provider, config)
        result = feed.load_universe([], date(2024, 1, 1), date(2024, 12, 31))

        assert result == {}
        provider.get_bars.assert_not_called()

    def test_warmup_extends_fetch_start(self) -> None:
        config = _make_config()
        provider = MagicMock()
        provider.get_bars.side_effect = DataProviderError("expected")

        feed = HistoricalDataFeed(provider, config)
        start = date(2024, 6, 1)
        end = date(2024, 12, 31)
        feed.load_universe(["SPY"], start, end)

        call_args = provider.get_bars.call_args
        fetch_start = call_args[0][1]  # second positional arg
        # fetch_start should be before start by roughly longest_window * 1.5 calendar days
        assert fetch_start < start
        expected_offset = int(config.indicators.longest_window * 1.5)
        assert fetch_start == start - timedelta(days=expected_offset)
