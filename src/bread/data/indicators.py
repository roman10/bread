"""Technical indicator computation using pure pandas (no external TA library)."""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from bread.core.config import IndicatorSettings
from bread.core.exceptions import InsufficientHistoryError

logger = logging.getLogger(__name__)


def _fmt_stddev(v: float) -> str:
    return str(int(v)) if v == int(v) else str(v)


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

    # SMA
    for period in settings.sma_periods:
        result[f"sma_{period}"] = _sma(result["close"], length=period)

    # EMA
    for period in settings.ema_periods:
        result[f"ema_{period}"] = _ema(result["close"], length=period)

    # RSI
    result[f"rsi_{settings.rsi_period}"] = _rsi(
        result["close"], length=settings.rsi_period
    )

    # MACD
    macd_df = _macd(
        result["close"],
        fast=settings.macd_fast,
        slow=settings.macd_slow,
        signal=settings.macd_signal,
    )
    result["macd"] = macd_df.iloc[:, 0]
    result["macd_signal"] = macd_df.iloc[:, 1]
    result["macd_hist"] = macd_df.iloc[:, 2]

    # ATR
    result[f"atr_{settings.atr_period}"] = _atr(
        result["high"], result["low"], result["close"],
        length=settings.atr_period,
    )

    # Bollinger Bands
    sdv = _fmt_stddev(settings.bollinger_stddev)
    bb_df = _bbands(
        result["close"],
        length=settings.bollinger_period,
        std=settings.bollinger_stddev,
    )
    bp = settings.bollinger_period
    result[f"bb_lower_{bp}_{sdv}"] = bb_df.iloc[:, 0]
    result[f"bb_mid_{bp}_{sdv}"] = bb_df.iloc[:, 1]
    result[f"bb_upper_{bp}_{sdv}"] = bb_df.iloc[:, 2]

    # Volume SMA
    result[f"volume_sma_{settings.volume_sma_period}"] = _sma(
        result["volume"].astype(float), length=settings.volume_sma_period
    )

    # Return periods (percentage change over N days)
    for period in settings.return_periods:
        result[f"return_{period}d"] = result["close"].pct_change(period)

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
    for p in settings.return_periods:
        cols.append(f"return_{p}d")
    return cols
