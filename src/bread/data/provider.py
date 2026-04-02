"""Abstract data provider contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

import pandas as pd


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
