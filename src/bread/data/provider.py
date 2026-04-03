"""Abstract data provider contract."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import date

import pandas as pd

logger = logging.getLogger(__name__)


class DataProvider(ABC):
    @abstractmethod
    def get_bars(
        self,
        symbol: str,
        start: date,
        end: date,
        timeframe: str,
    ) -> pd.DataFrame:
        """Fetch OHLCV bars.

        Returns a DataFrame with:
        - Sorted ascending by timestamp
        - Timezone-aware UTC DatetimeIndex named 'timestamp'
        - Columns: open, high, low, close, volume
        - No duplicate timestamps
        """
        ...

    def get_bars_batch(
        self,
        symbols: list[str],
        start: date,
        end: date,
        timeframe: str,
    ) -> dict[str, pd.DataFrame]:
        """Fetch OHLCV bars for multiple symbols in one request.

        Default implementation falls back to sequential get_bars() calls.
        Subclasses may override for true batch API support.

        Returns dict of symbol -> DataFrame. Symbols that fail to fetch are
        omitted with a warning (no exception raised for partial failures).
        """
        result: dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            try:
                result[symbol] = self.get_bars(symbol, start, end, timeframe)
            except Exception:
                logger.warning("Failed to fetch %s, skipping", symbol)
        return result
