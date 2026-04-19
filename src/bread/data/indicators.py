"""Technical indicator computation using pure pandas (no external TA library)."""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from bread.core.config import IndicatorSettings
from bread.core.exceptions import InsufficientHistoryError
from bread.data.indicator_specs import (
    ATR,
    EMA,
    RSI,
    SMA,
    BBLower,
    BBMid,
    BBUpper,
    MACDHist,
    MACDLine,
    MACDSignal,
    ReturnPct,
    VolumeSMA,
    specs_for_settings,
)

logger = logging.getLogger(__name__)


def _sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(length).mean()


def _ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def _rsi(close: pd.Series, length: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _macd(
    close: pd.Series, fast: int, slow: int, signal: int
) -> pd.DataFrame:
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return pd.DataFrame({0: macd_line, 1: signal_line, 2: histogram})


def _atr(
    high: pd.Series, low: pd.Series, close: pd.Series, length: int
) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()


def _bbands(close: pd.Series, length: int, std: float) -> pd.DataFrame:
    mid = close.rolling(length).mean()
    stddev = close.rolling(length).std(ddof=1)
    upper = mid + std * stddev
    lower = mid - std * stddev
    return pd.DataFrame({0: lower, 1: mid, 2: upper})


def compute_indicators(df: pd.DataFrame, settings: IndicatorSettings) -> pd.DataFrame:
    """Add indicator columns to an OHLCV DataFrame.

    Returns a copy with leading NaN rows trimmed.
    Raises InsufficientHistoryError if the input is too short.
    """
    longest_window = settings.longest_window
    if len(df) < longest_window:
        raise InsufficientHistoryError(
            f"Need at least {longest_window} rows, got {len(df)}"
        )

    result = df.copy()

    # Column names come from indicator_specs.py — single source of truth.
    for period in settings.sma_periods:
        result[SMA(period).column] = _sma(result["close"], length=period)

    for period in settings.ema_periods:
        result[EMA(period).column] = _ema(result["close"], length=period)

    result[RSI(settings.rsi_period).column] = _rsi(
        result["close"], length=settings.rsi_period
    )

    macd_df = _macd(
        result["close"],
        fast=settings.macd_fast,
        slow=settings.macd_slow,
        signal=settings.macd_signal,
    )
    result[MACDLine().column] = macd_df.iloc[:, 0]
    result[MACDSignal().column] = macd_df.iloc[:, 1]
    result[MACDHist().column] = macd_df.iloc[:, 2]

    result[ATR(settings.atr_period).column] = _atr(
        result["high"], result["low"], result["close"],
        length=settings.atr_period,
    )

    bp, sdv = settings.bollinger_period, settings.bollinger_stddev
    bb_df = _bbands(result["close"], length=bp, std=sdv)
    result[BBLower(bp, sdv).column] = bb_df.iloc[:, 0]
    result[BBMid(bp, sdv).column] = bb_df.iloc[:, 1]
    result[BBUpper(bp, sdv).column] = bb_df.iloc[:, 2]

    result[VolumeSMA(settings.volume_sma_period).column] = _sma(
        result["volume"].astype(float), length=settings.volume_sma_period
    )

    for period in settings.return_periods:
        result[ReturnPct(period).column] = result["close"].pct_change(period)

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
    return [s.column for s in specs_for_settings(settings)]
