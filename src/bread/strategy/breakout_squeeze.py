"""Bollinger Band Squeeze Breakout strategy implementation."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from bread.core.config import IndicatorSettings
from bread.core.exceptions import StrategyError
from bread.core.models import Signal, SignalDirection
from bread.data.indicator_specs import (
    ATR,
    BBLower,
    BBMid,
    BBUpper,
    MACDHist,
    VolumeSMA,
)
from bread.strategy.base import Strategy, load_strategy_config
from bread.strategy.registry import register

logger = logging.getLogger(__name__)


@register("breakout_squeeze")
class BreakoutSqueeze(Strategy):
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
        self._bollinger_period: int = entry.get("bollinger_period", 20)
        self._bollinger_stddev: float = entry.get("bollinger_stddev", 2.0)
        self._squeeze_lookback: int = entry.get("squeeze_lookback", 50)
        self._squeeze_percentile: float = entry.get("squeeze_percentile", 20.0)
        self._volume_sma_period: int = entry.get("volume_sma_period", 20)
        self._volume_mult: float = entry.get("volume_mult", 1.2)

        # Exit params
        self._atr_stop_mult: float = exit_.get("atr_stop_mult", 1.5)
        self._time_stop: int = exit_.get("time_stop_days", 10)

        self._atr_period: int = indicator_settings.atr_period

        if self._volume_mult < 1.0:
            raise StrategyError(f"volume_mult must be >= 1.0, got {self._volume_mult}")

        bp, sdv = self._bollinger_period, self._bollinger_stddev
        bb_lower = BBLower(bp, sdv)
        bb_mid = BBMid(bp, sdv)
        bb_upper = BBUpper(bp, sdv)
        atr = ATR(self._atr_period)
        vol_sma = VolumeSMA(self._volume_sma_period)
        self._declare_indicators(
            indicator_settings, bb_lower, bb_mid, bb_upper, atr, vol_sma,
            MACDHist(),
            extras={"close", "volume"},
        )

        self._col_bb_lower = bb_lower.column
        self._col_bb_mid = bb_mid.column
        self._col_bb_upper = bb_upper.column
        self._col_atr = atr.column
        self._col_vol_sma = vol_sma.column

    @property
    def name(self) -> str:
        return "breakout_squeeze"

    @property
    def universe(self) -> list[str]:
        return list(self._universe)

    @property
    def min_history_days(self) -> int:
        return max(
            self._bollinger_period, self._squeeze_lookback,
            self._atr_period, self._volume_sma_period,
        )

    @property
    def time_stop_days(self) -> int:
        return self._time_stop

    def evaluate(self, universe: dict[str, pd.DataFrame]) -> list[Signal]:
        """Evaluate strategy on enriched DataFrames."""
        return self._evaluate_universe(universe, self._evaluate_symbol)

    def _evaluate_symbol(self, symbol: str, df: pd.DataFrame) -> Signal | None:
        """Evaluate a single symbol. Returns at most one signal."""
        if len(df) < self._squeeze_lookback:
            return None

        last = df.iloc[-1]
        close = float(last["close"])
        bb_lower = float(last[self._col_bb_lower])
        bb_mid = float(last[self._col_bb_mid])
        bb_upper = float(last[self._col_bb_upper])
        atr = float(last[self._col_atr])
        volume = float(last["volume"])
        vol_sma = float(last[self._col_vol_sma])
        macd_hist = float(last["macd_hist"])

        stop_loss_pct = self._atr_stop_mult * atr / close
        if stop_loss_pct <= 0:
            raise StrategyError(f"Non-positive stop_loss_pct for {symbol}: {stop_loss_pct}")

        now = datetime.now(UTC)

        # Check EXIT conditions first
        # Price falls back below BB mid
        if close < bb_mid:
            return Signal(
                symbol=symbol,
                direction=SignalDirection.SELL,
                strength=1.0,
                stop_loss_pct=stop_loss_pct,
                strategy_name=self.name,
                reason=(
                    f"SELL: close={close:.2f} < bb_mid={bb_mid:.2f} "
                    f"breakout failed"
                ),
                timestamp=now,
            )

        # MACD histogram turns negative
        if macd_hist < 0:
            return Signal(
                symbol=symbol,
                direction=SignalDirection.SELL,
                strength=1.0,
                stop_loss_pct=stop_loss_pct,
                strategy_name=self.name,
                reason=f"SELL: macd_hist={macd_hist:.4f} < 0 momentum fading",
                timestamp=now,
            )

        # Check ENTRY conditions
        # 1. Compute BB width and check for squeeze
        if bb_mid <= 0:
            return None
        bb_width = (bb_upper - bb_lower) / bb_mid

        # Compute BB width over lookback period
        lookback = df.iloc[-self._squeeze_lookback:]
        bb_widths = (
            (lookback[self._col_bb_upper] - lookback[self._col_bb_lower])
            / lookback[self._col_bb_mid]
        )
        # Check if current width is in lowest percentile
        percentile_threshold = bb_widths.quantile(self._squeeze_percentile / 100.0)
        if bb_width > percentile_threshold:
            return None

        # 2. Price breaks above upper band
        if close <= bb_upper:
            return None

        # 3. Volume confirmation
        if volume <= self._volume_mult * vol_sma:
            return None

        # 4. MACD histogram positive (trend confirmation)
        if macd_hist <= 0:
            return None

        # All entry conditions met -- BUY
        vol_ratio = volume / vol_sma if vol_sma > 0 else 0.0
        strength = max(0.0, min(1.0, vol_ratio - 1.0))

        return Signal(
            symbol=symbol,
            direction=SignalDirection.BUY,
            strength=strength,
            stop_loss_pct=stop_loss_pct,
            strategy_name=self.name,
            reason=(
                f"BUY: squeeze detected (bb_width={bb_width:.4f}, "
                f"threshold={percentile_threshold:.4f}), "
                f"close={close:.2f} > bb_upper={bb_upper:.2f}, "
                f"vol_ratio={vol_ratio:.1f}x, macd_hist={macd_hist:.4f}"
            ),
            timestamp=now,
        )
