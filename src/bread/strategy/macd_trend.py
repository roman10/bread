"""MACD Histogram Trend Following strategy implementation."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from bread.core.config import IndicatorSettings
from bread.core.exceptions import StrategyError
from bread.core.models import Signal, SignalDirection
from bread.strategy.base import Strategy, load_strategy_config
from bread.strategy.registry import register

logger = logging.getLogger(__name__)


@register("macd_trend")
class MacdTrend(Strategy):
    def __init__(self, config_path: Path, indicator_settings: IndicatorSettings) -> None:
        """Load strategy-specific config from YAML."""
        cfg = load_strategy_config(config_path)

        self._universe: list[str] = cfg.get("universe", [])
        entry = cfg.get("entry", {})
        exit_ = cfg.get("exit", {})

        # Entry params
        self._ema_trend_filter: int = entry.get("ema_trend_filter", 21)
        self._volume_sma_period: int = entry.get("volume_sma_period", 20)
        self._volume_mult: float = entry.get("volume_mult", 1.0)
        self._require_macd_above_signal: bool = entry.get("require_macd_above_signal", True)

        # Exit params
        self._atr_stop_mult: float = exit_.get("atr_stop_mult", 1.5)
        self._time_stop: int = exit_.get("time_stop_days", 12)

        self._atr_period: int = indicator_settings.atr_period
        self._macd_warmup: int = indicator_settings.macd_slow + indicator_settings.macd_signal

        # Validate indicator compatibility
        if self._ema_trend_filter not in indicator_settings.ema_periods:
            raise StrategyError(
                f"EMA period {self._ema_trend_filter} not in "
                f"indicator settings {indicator_settings.ema_periods}"
            )
        if self._volume_mult < 1.0:
            raise StrategyError(f"volume_mult must be >= 1.0, got {self._volume_mult}")
        if self._volume_sma_period != indicator_settings.volume_sma_period:
            raise StrategyError(
                f"Volume SMA period {self._volume_sma_period} != "
                f"indicator setting {indicator_settings.volume_sma_period}"
            )

        # Column names
        self._col_ema = f"ema_{self._ema_trend_filter}"
        self._col_atr = f"atr_{self._atr_period}"
        self._col_vol_sma = f"volume_sma_{self._volume_sma_period}"

        self._required_cols = {
            "close", "volume",
            self._col_ema, self._col_atr, self._col_vol_sma,
            "macd", "macd_signal", "macd_hist",
        }

    @property
    def name(self) -> str:
        return "macd_trend"

    @property
    def universe(self) -> list[str]:
        return list(self._universe)

    @property
    def min_history_days(self) -> int:
        return max(self._ema_trend_filter, self._atr_period, self._volume_sma_period,
                   self._macd_warmup)

    @property
    def time_stop_days(self) -> int:
        return self._time_stop

    def evaluate(self, universe: dict[str, pd.DataFrame]) -> list[Signal]:
        """Evaluate strategy on enriched DataFrames."""
        return self._evaluate_universe(universe, self._evaluate_symbol)

    def _evaluate_symbol(self, symbol: str, df: pd.DataFrame) -> Signal | None:
        """Evaluate a single symbol. Returns at most one signal."""
        if len(df) < 2:
            return None

        last = df.iloc[-1]
        prev = df.iloc[-2]
        close = float(last["close"])
        ema = float(last[self._col_ema])
        atr = float(last[self._col_atr])
        volume = float(last["volume"])
        vol_sma = float(last[self._col_vol_sma])
        macd_line = float(last["macd"])
        macd_sig = float(last["macd_signal"])
        hist_now = float(last["macd_hist"])
        hist_prev = float(prev["macd_hist"])

        stop_loss_pct = self._atr_stop_mult * atr / close
        if stop_loss_pct <= 0:
            raise StrategyError(f"Non-positive stop_loss_pct for {symbol}: {stop_loss_pct}")

        now = datetime.now(UTC)

        # Check EXIT conditions first
        # Histogram crosses from positive to negative
        if hist_now < 0 and hist_prev >= 0:
            return Signal(
                symbol=symbol,
                direction=SignalDirection.SELL,
                strength=1.0,
                stop_loss_pct=stop_loss_pct,
                strategy_name=self.name,
                reason=(
                    f"SELL: macd_hist crossed negative "
                    f"({hist_prev:.4f} -> {hist_now:.4f})"
                ),
                timestamp=now,
            )

        # MACD line crosses below signal line
        if macd_line < macd_sig and float(prev["macd"]) >= float(prev["macd_signal"]):
            return Signal(
                symbol=symbol,
                direction=SignalDirection.SELL,
                strength=1.0,
                stop_loss_pct=stop_loss_pct,
                strategy_name=self.name,
                reason=(
                    f"SELL: macd={macd_line:.4f} crossed below "
                    f"signal={macd_sig:.4f}"
                ),
                timestamp=now,
            )

        # Check ENTRY conditions
        # 1. MACD histogram crosses from negative to positive
        if not (hist_now > 0 and hist_prev <= 0):
            return None

        # 2. MACD line above signal line (optional but default on)
        if self._require_macd_above_signal and macd_line <= macd_sig:
            return None

        # 3. Price above EMA trend filter
        if close <= ema:
            return None

        # 4. Volume confirmation
        if volume <= self._volume_mult * vol_sma:
            return None

        # All entry conditions met -- BUY
        # Strength: histogram crossover magnitude relative to ATR
        hist_magnitude = abs(hist_now) / atr if atr > 0 else 0.0
        strength = max(0.0, min(1.0, hist_magnitude))

        return Signal(
            symbol=symbol,
            direction=SignalDirection.BUY,
            strength=strength,
            stop_loss_pct=stop_loss_pct,
            strategy_name=self.name,
            reason=(
                f"BUY: macd_hist crossed positive "
                f"({hist_prev:.4f} -> {hist_now:.4f}), "
                f"close={close:.2f} > ema{self._ema_trend_filter}={ema:.2f}, "
                f"macd={macd_line:.4f} > signal={macd_sig:.4f}"
            ),
            timestamp=now,
        )
