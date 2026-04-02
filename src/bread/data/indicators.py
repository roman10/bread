"""Technical indicator computation using pandas-ta."""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import pandas_ta as ta

from bread.core.config import IndicatorSettings
from bread.core.exceptions import InsufficientHistoryError

logger = logging.getLogger(__name__)


def _fmt_stddev(v: float) -> str:
    return str(int(v)) if v == int(v) else str(v)


def compute_indicators(df: pd.DataFrame, settings: IndicatorSettings) -> pd.DataFrame:
    """Add indicator columns to an OHLCV DataFrame.

    Returns a copy with leading NaN rows trimmed.
    Raises InsufficientHistoryError if the input is too short.
    """
    longest_window = max(
        max(settings.sma_periods),
        max(settings.ema_periods),
        settings.rsi_period,
        settings.macd_slow + settings.macd_signal,
        settings.atr_period,
        settings.bollinger_period,
        settings.volume_sma_period,
    )
    if len(df) < longest_window:
        raise InsufficientHistoryError(
            f"Need at least {longest_window} rows, got {len(df)}"
        )

    result = df.copy()

    # SMA
    for period in settings.sma_periods:
        result[f"sma_{period}"] = ta.sma(result["close"], length=period)

    # EMA
    for period in settings.ema_periods:
        result[f"ema_{period}"] = ta.ema(result["close"], length=period)

    # RSI
    result[f"rsi_{settings.rsi_period}"] = ta.rsi(
        result["close"], length=settings.rsi_period
    )

    # MACD
    macd_df = ta.macd(
        result["close"],
        fast=settings.macd_fast,
        slow=settings.macd_slow,
        signal=settings.macd_signal,
    )
    if macd_df is not None:
        result["macd"] = macd_df.iloc[:, 0]
        result["macd_signal"] = macd_df.iloc[:, 1]
        result["macd_hist"] = macd_df.iloc[:, 2]

    # ATR
    result[f"atr_{settings.atr_period}"] = ta.atr(
        result["high"], result["low"], result["close"],
        length=settings.atr_period,
    )

    # Bollinger Bands
    sdv = _fmt_stddev(settings.bollinger_stddev)
    bb_df = ta.bbands(
        result["close"],
        length=settings.bollinger_period,
        std=settings.bollinger_stddev,  # type: ignore[arg-type]
    )
    if bb_df is not None:
        bp = settings.bollinger_period
        result[f"bb_lower_{bp}_{sdv}"] = bb_df.iloc[:, 0]
        result[f"bb_mid_{bp}_{sdv}"] = bb_df.iloc[:, 1]
        result[f"bb_upper_{bp}_{sdv}"] = bb_df.iloc[:, 2]

    # Volume SMA
    result[f"volume_sma_{settings.volume_sma_period}"] = ta.sma(
        result["volume"].astype(float), length=settings.volume_sma_period
    )

    # Determine indicator columns (everything except original OHLCV)
    ohlcv = {"open", "high", "low", "close", "volume"}
    indicator_cols = [c for c in result.columns if c not in ohlcv]

    # Trim leading NaN rows
    first_valid = result[indicator_cols].first_valid_index()
    if first_valid is not None:
        loc = result.index.get_loc(first_valid)
        trimmed_count = int(loc) if isinstance(loc, (int, np.integer)) else 0
        if trimmed_count > 0:
            logger.debug("Trimming %d leading rows with NaN indicators", trimmed_count)
        result = result.loc[first_valid:]  # type: ignore[misc]

    # Drop any remaining rows with NaN in indicator columns
    before = len(result)
    result = result.dropna(subset=indicator_cols)
    dropped = before - len(result)
    if dropped > 0:
        logger.debug("Dropped %d additional rows with NaN indicators", dropped)

    return result


def get_indicator_columns(settings: IndicatorSettings) -> list[str]:
    """Return the list of indicator column names for the given settings."""
    cols: list[str] = []
    for p in settings.sma_periods:
        cols.append(f"sma_{p}")
    for p in settings.ema_periods:
        cols.append(f"ema_{p}")
    cols.append(f"rsi_{settings.rsi_period}")
    cols.extend(["macd", "macd_signal", "macd_hist"])
    cols.append(f"atr_{settings.atr_period}")
    sdv = _fmt_stddev(settings.bollinger_stddev)
    bp = settings.bollinger_period
    cols.extend([f"bb_lower_{bp}_{sdv}", f"bb_mid_{bp}_{sdv}", f"bb_upper_{bp}_{sdv}"])
    cols.append(f"volume_sma_{settings.volume_sma_period}")
    return cols
