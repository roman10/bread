"""Abstract strategy interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from bread.core.models import Signal


class Strategy(ABC):
    @abstractmethod
    def evaluate(self, universe: dict[str, pd.DataFrame]) -> list[Signal]:
        """Evaluate the strategy on enriched OHLCV+indicator DataFrames.

        Args:
            universe: mapping of symbol -> DataFrame with OHLCV + indicator columns.
                      Each DataFrame has a UTC DatetimeIndex named 'timestamp',
                      sorted ascending, with indicator columns from compute_indicators().

        Returns:
            List of Signal objects. May be empty if no conditions are met.
        """
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique strategy identifier (e.g. 'etf_momentum')."""
        ...

    @property
    @abstractmethod
    def universe(self) -> list[str]:
        """List of symbols this strategy trades."""
        ...

    @property
    @abstractmethod
    def min_history_days(self) -> int:
        """Minimum number of trading days of history required for evaluation."""
        ...

    @property
    @abstractmethod
    def time_stop_days(self) -> int:
        """Number of trading bars to hold before a time-stop exit."""
        ...
