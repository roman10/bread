"""MACD Bullish Divergence strategy implementation."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from bread.core.config import IndicatorSettings
from bread.core.exceptions import StrategyError
from bread.core.models import Signal, SignalDirection
from bread.data.indicators import _fmt_stddev
from bread.strategy.base import Strategy, load_strategy_config
from bread.strategy.registry import register

logger = logging.getLogger(__name__)


@register("macd_divergence")
class MacdDivergence(Strategy):
    def __init__(
        self, config_path: Path, indicator_settings: IndicatorSettings,
        *, universe: list[str] | None = None,
    ) -> None:
        """Load strategy-specific config from YAML."""
        cfg = load_strategy_config(config_path)

        self._universe: list[str] = universe if universe is not None else cfg.get("universe", [])
        entry = cfg.get("entry", {})
        exit_ = cfg.get("exit", {})

        # Entry params
        self._divergence_lookback: int = entry.get("divergence_lookback", 20)
        self._rsi_period: int = entry.get("rsi_period", 14)
        self._rsi_entry_max: float = entry.get("rsi_entry_max", 45)
        self._swing_low_window: int = entry.get("swing_low_window", 3)

        # Exit params
        self._rsi_exit: float = exit_.get("rsi_exit", 65)
        self._atr_stop_mult: float = exit_.get("atr_stop_mult", 2.0)
        self._time_stop: int = exit_.get("time_stop_days", 12)

        self._atr_period: int = indicator_settings.atr_period
        self._bollinger_period: int = indicator_settings.bollinger_period
        self._bollinger_stddev: float = indicator_settings.bollinger_stddev

        # Validate indicator compatibility
        if self._rsi_period != indicator_settings.rsi_period:
            raise StrategyError(
                f"RSI period {self._rsi_period} != "
                f"indicator setting {indicator_settings.rsi_period}"
            )

        # Column names
        self._col_rsi = f"rsi_{self._rsi_period}"
        self._col_atr = f"atr_{self._atr_period}"
        sdv = _fmt_stddev(self._bollinger_stddev)
        bp = self._bollinger_period
        self._col_bb_upper = f"bb_upper_{bp}_{sdv}"

        self._required_cols = {
            "close", self._col_rsi, self._col_atr, self._col_bb_upper,
            "macd_hist",
        }

    @property
    def name(self) -> str:
        return "macd_divergence"

    @property
    def universe(self) -> list[str]:
        return list(self._universe)

    @property
    def min_history_days(self) -> int:
        return max(
            self._divergence_lookback, self._rsi_period, self._atr_period,
        )

    @property
    def time_stop_days(self) -> int:
        return self._time_stop

    def evaluate(self, universe: dict[str, pd.DataFrame]) -> list[Signal]:
        """Evaluate strategy on enriched DataFrames."""
        return self._evaluate_universe(universe, self._evaluate_symbol)

    def _find_swing_lows(self, series: pd.Series, window: int) -> list[int]:
        """Find indices of swing lows in a series.

        A swing low is a point where the value is lower than the surrounding
        `window` bars on each side.
        """
        lows: list[int] = []
        for i in range(window, len(series) - window):
            val = series.iloc[i]
            left = series.iloc[i - window:i]
            right = series.iloc[i + 1:i + window + 1]
            if (val <= left).all() and (val <= right).all():
                lows.append(i)
        return lows

    def _evaluate_symbol(self, symbol: str, df: pd.DataFrame) -> Signal | None:
        """Evaluate a single symbol. Returns at most one signal."""
        if len(df) < self._divergence_lookback:
            return None

        last = df.iloc[-1]
        close = float(last["close"])
        rsi = float(last[self._col_rsi])
        atr = float(last[self._col_atr])
        bb_upper = float(last[self._col_bb_upper])
        macd_hist = float(last["macd_hist"])

        stop_loss_pct = self._atr_stop_mult * atr / close
        if stop_loss_pct <= 0:
            raise StrategyError(f"Non-positive stop_loss_pct for {symbol}: {stop_loss_pct}")

        now = datetime.now(UTC)

        # Check EXIT conditions first
        if rsi > self._rsi_exit:
            return Signal(
                symbol=symbol,
                direction=SignalDirection.SELL,
                strength=1.0,
                stop_loss_pct=stop_loss_pct,
                strategy_name=self.name,
                reason=f"SELL: rsi={rsi:.1f} > {self._rsi_exit} exit threshold",
                timestamp=now,
            )

        # MACD histogram turns negative after being positive
        prev_hist = float(df.iloc[-2]["macd_hist"])
        if macd_hist < 0 and prev_hist >= 0:
            return Signal(
                symbol=symbol,
                direction=SignalDirection.SELL,
                strength=1.0,
                stop_loss_pct=stop_loss_pct,
                strategy_name=self.name,
                reason=(
                    f"SELL: macd_hist crossed negative "
                    f"({prev_hist:.4f} -> {macd_hist:.4f})"
                ),
                timestamp=now,
            )

        if close > bb_upper:
            return Signal(
                symbol=symbol,
                direction=SignalDirection.SELL,
                strength=1.0,
                stop_loss_pct=stop_loss_pct,
                strategy_name=self.name,
                reason=(
                    f"SELL: close={close:.2f} > bb_upper={bb_upper:.2f} "
                    f"target reached"
                ),
                timestamp=now,
            )

        # Check ENTRY conditions
        # 1. Detect bullish divergence in lookback window
        lookback = df.iloc[-self._divergence_lookback:]
        price_series = lookback["close"]
        hist_series = lookback["macd_hist"]

        swing_lows = self._find_swing_lows(price_series, self._swing_low_window)
        if len(swing_lows) < 2:
            return None

        # Compare the two most recent swing lows
        idx1, idx2 = swing_lows[-2], swing_lows[-1]
        price_low1 = float(price_series.iloc[idx1])
        price_low2 = float(price_series.iloc[idx2])
        hist_low1 = float(hist_series.iloc[idx1])
        hist_low2 = float(hist_series.iloc[idx2])

        # Bullish divergence: price makes lower low, MACD makes higher low
        if not (price_low2 < price_low1 and hist_low2 > hist_low1):
            return None

        # 2. RSI below entry threshold
        if rsi >= self._rsi_entry_max:
            return None

        # 3. MACD histogram turning up (today > yesterday)
        if macd_hist <= float(df.iloc[-2]["macd_hist"]):
            return None

        # All entry conditions met -- BUY
        # Strength: magnitude of divergence
        price_divergence = abs(price_low1 - price_low2) / close if close > 0 else 0.0
        hist_divergence = abs(hist_low2 - hist_low1) / atr if atr > 0 else 0.0
        strength = max(0.0, min(1.0, price_divergence + hist_divergence))

        return Signal(
            symbol=symbol,
            direction=SignalDirection.BUY,
            strength=strength,
            stop_loss_pct=stop_loss_pct,
            strategy_name=self.name,
            reason=(
                f"BUY: bullish divergence detected, "
                f"price lows={price_low1:.2f}->{price_low2:.2f} (lower), "
                f"macd_hist lows={hist_low1:.4f}->{hist_low2:.4f} (higher), "
                f"rsi={rsi:.1f}"
            ),
            timestamp=now,
        )
