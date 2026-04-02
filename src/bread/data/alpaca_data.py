"""Alpaca Markets data provider."""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from requests.exceptions import ConnectionError, Timeout
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from bread.core.config import AppConfig
from bread.core.exceptions import (
    DataProviderAuthError,
    DataProviderError,
    DataProviderRateLimitError,
    DataProviderResponseError,
)
from bread.data.provider import DataProvider

logger = logging.getLogger(__name__)

_TIMEFRAME_MAP = {
    "1Day": TimeFrame.Day,
}


class AlpacaDataProvider(DataProvider):
    def __init__(self, config: AppConfig) -> None:
        if config.mode == "paper":
            api_key = config.alpaca.paper_api_key
            secret_key = config.alpaca.paper_secret_key
        else:
            api_key = config.alpaca.live_api_key
            secret_key = config.alpaca.live_secret_key

        if not api_key or not secret_key:
            raise DataProviderAuthError(
                f"Missing API credentials for {config.mode} mode"
            )
        self._client = StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)
        self._config = config

    def get_bars(
        self,
        symbol: str,
        start: date,
        end: date,
        timeframe: str,
    ) -> pd.DataFrame:
        symbol = symbol.upper()
        tf = _TIMEFRAME_MAP.get(timeframe)
        if tf is None:
            raise DataProviderError(f"Unsupported timeframe: {timeframe}")

        logger.info("Fetching %s bars for %s from %s to %s", timeframe, symbol, start, end)
        df = self._fetch_with_retry(symbol, start, end, tf)
        return self._normalize(df, symbol)

    @retry(
        retry=retry_if_exception_type(
            (ConnectionError, Timeout, DataProviderRateLimitError)
        ),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        reraise=True,
    )
    def _fetch_with_retry(
        self,
        symbol: str,
        start: date,
        end: date,
        timeframe: TimeFrame,
    ) -> pd.DataFrame:
        # Classify and raise typed exceptions BEFORE tenacity sees them.
        # Auth errors and response errors are NOT retried.
        # ConnectionError, Timeout, and RateLimitError ARE retried by tenacity.
        try:
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=timeframe,
                start=start,
                end=end,
            )
            barset = self._client.get_stock_bars(request)
            df = barset.df  # type: ignore[union-attr]
        except (ConnectionError, Timeout):
            raise  # let tenacity handle these directly
        except Exception as exc:
            exc_str = str(exc).lower()
            if "401" in exc_str or "403" in exc_str or "forbidden" in exc_str:
                raise DataProviderAuthError(f"Authentication failed: {exc}") from exc
            if "429" in exc_str or "rate" in exc_str:
                raise DataProviderRateLimitError(f"Rate limited: {exc}") from exc
            raise DataProviderError(f"Alpaca request failed: {exc}") from exc

        if df is None or df.empty:
            raise DataProviderResponseError(f"No data returned for {symbol}")

        return df

    @staticmethod
    def _normalize(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """Normalize Alpaca response into the provider contract."""
        # Alpaca returns multi-index (symbol, timestamp); drop symbol level
        if isinstance(df.index, pd.MultiIndex):
            df = df.droplevel("symbol")

        df.index = pd.DatetimeIndex(df.index, tz="UTC")
        df.index.name = "timestamp"

        required_cols = ["open", "high", "low", "close", "volume"]
        rename_map = {}
        for col in required_cols:
            for existing in df.columns:
                if existing.lower() == col:
                    rename_map[existing] = col
        df = df.rename(columns=rename_map)

        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            raise DataProviderResponseError(
                f"Missing columns for {symbol}: {missing}"
            )

        df = df[required_cols].copy()
        df = df.sort_index()
        df = df[~df.index.duplicated(keep="last")]
        return df
