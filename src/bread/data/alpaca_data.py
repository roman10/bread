"""Alpaca Markets data provider."""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError, Timeout
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

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
        adapter = HTTPAdapter(pool_maxsize=20)
        self._client._session.mount("https://", adapter)
        self._config = config
        self._retrier = Retrying(
            retry=retry_if_exception_type(
                (ConnectionError, Timeout, DataProviderRateLimitError)
            ),
            stop=stop_after_attempt(config.data.max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=4),
            reraise=True,
        )

    # Maximum symbols per batch request to stay within Alpaca limits
    _BATCH_CHUNK_SIZE = 100

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
        df = self._retrier(self._do_fetch, symbol, start, end, tf)
        return self._normalize(df, symbol)

    def get_bars_batch(
        self,
        symbols: list[str],
        start: date,
        end: date,
        timeframe: str,
    ) -> dict[str, pd.DataFrame]:
        """Fetch bars for multiple symbols in batched API calls."""
        symbols = [s.upper() for s in symbols]
        tf = _TIMEFRAME_MAP.get(timeframe)
        if tf is None:
            raise DataProviderError(f"Unsupported timeframe: {timeframe}")

        if not symbols:
            return {}

        result: dict[str, pd.DataFrame] = {}
        for i in range(0, len(symbols), self._BATCH_CHUNK_SIZE):
            chunk = symbols[i : i + self._BATCH_CHUNK_SIZE]
            logger.info(
                "Batch fetching %s bars for %d symbols from %s to %s",
                timeframe, len(chunk), start, end,
            )
            try:
                df = self._retrier(self._do_fetch_batch, chunk, start, end, tf)
            except Exception:
                logger.exception("Batch fetch failed for chunk, falling back to sequential")
                for sym in chunk:
                    try:
                        result[sym] = self.get_bars(sym, start, end, timeframe)
                    except Exception:
                        logger.warning("Failed to fetch %s, skipping", sym)
                continue

            result.update(self._split_batch(df, chunk))

        return result

    def _do_fetch(
        self,
        symbol: str,
        start: date,
        end: date,
        timeframe: TimeFrame,
    ) -> pd.DataFrame:
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

    def _do_fetch_batch(
        self,
        symbols: list[str],
        start: date,
        end: date,
        timeframe: TimeFrame,
    ) -> pd.DataFrame:
        try:
            request = StockBarsRequest(
                symbol_or_symbols=symbols,
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
            raise DataProviderResponseError(
                f"No data returned for batch: {symbols}"
            )

        return df

    def _split_batch(
        self,
        df: pd.DataFrame,
        requested_symbols: list[str],
    ) -> dict[str, pd.DataFrame]:
        """Split a MultiIndex batch response into per-symbol normalized DataFrames."""
        result: dict[str, pd.DataFrame] = {}

        if not isinstance(df.index, pd.MultiIndex):
            # Single symbol came back (edge case: batch of 1)
            if len(requested_symbols) == 1:
                result[requested_symbols[0]] = self._normalize(df, requested_symbols[0])
            return result

        symbols_in_response = df.index.get_level_values(0).unique()
        for sym in requested_symbols:
            if sym not in symbols_in_response:
                logger.warning("No data returned for %s in batch response", sym)
                continue
            sym_df = pd.DataFrame(df.loc[sym]).copy()
            result[sym] = self._normalize(sym_df, sym)

        return result

    @staticmethod
    def _normalize(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """Normalize Alpaca response into the provider contract."""
        if isinstance(df.index, pd.MultiIndex):
            df = df.droplevel("symbol")

        idx = pd.DatetimeIndex(df.index)
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        df.index = idx
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
