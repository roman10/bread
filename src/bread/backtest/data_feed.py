"""Historical data feed for backtesting."""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd

from bread.core.config import AppConfig
from bread.core.exceptions import InsufficientHistoryError
from bread.data.indicators import compute_indicators
from bread.data.provider import DataProvider

logger = logging.getLogger(__name__)


class HistoricalDataFeed:
    def __init__(self, provider: DataProvider, config: AppConfig) -> None:
        self._provider = provider
        self._config = config

    def load_universe(
        self,
        symbols: list[str],
        start: date,
        end: date,
    ) -> dict[str, pd.DataFrame]:
        """Fetch and enrich data for all symbols.

        Returns dict of symbol -> enriched DataFrame filtered to [start, end].
        """
        longest = self._config.indicators.longest_window
        fetch_start = start - timedelta(days=int(longest * 1.5))

        raw_batch = self._provider.get_bars_batch(
            symbols, fetch_start, end, self._config.data.default_timeframe,
        )

        result: dict[str, pd.DataFrame] = {}

        for symbol in symbols:
            raw = raw_batch.get(symbol)
            if raw is None:
                logger.warning("Failed to fetch data for %s, excluding from universe", symbol)
                continue

            try:
                enriched = compute_indicators(raw, self._config.indicators)
            except InsufficientHistoryError:
                logger.warning(
                    "Insufficient history for indicators on %s, excluding from universe", symbol
                )
                continue

            # Filter to [start, end] using .date() comparison on UTC DatetimeIndex
            mask = (enriched.index.date >= start) & (enriched.index.date <= end)  # type: ignore[attr-defined]
            filtered = enriched.loc[mask]

            if filtered.empty:
                logger.warning("No data for %s in [%s, %s] after filtering", symbol, start, end)
                continue

            result[symbol] = filtered

        logger.info(
            "Loaded universe: %d/%d symbols with data in [%s, %s]",
            len(result), len(symbols), start, end,
        )
        return result
