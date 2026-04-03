"""ETF Momentum strategy implementation."""

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


@register("etf_momentum")
class EtfMomentum(Strategy):
    def __init__(self, config_path: Path, indicator_settings: IndicatorSettings) -> None:
        """Load strategy-specific config from YAML."""
        cfg = load_strategy_config(config_path)

        self._universe: list[str] = cfg.get("universe", [])
        entry = cfg.get("entry", {})
        exit_ = cfg.get("exit", {})

        # Entry params
        self._sma_long: int = entry.get("sma_long", 200)
        self._rsi_period: int = entry.get("rsi_period", 14)
        self._rsi_oversold: float = entry.get("rsi_oversold", 30)
        self._sma_fast: int = entry.get("sma_fast", 20)
        self._sma_mid: int = entry.get("sma_mid", 50)
        self._volume_sma_period: int = entry.get("volume_sma_period", 20)
        self._volume_mult: float = entry.get("volume_mult", 1.0)

        # Exit params
        self._rsi_overbought: float = exit_.get("rsi_overbought", 70)
        self._atr_stop_mult: float = exit_.get("atr_stop_mult", 1.5)
        self._time_stop: int = exit_.get("time_stop_days", 15)

        self._atr_period: int = indicator_settings.atr_period

        # Validate indicator compatibility
        for sma in (self._sma_long, self._sma_fast, self._sma_mid):
            if sma not in indicator_settings.sma_periods:
                raise StrategyError(
                    f"SMA period {sma} not in indicator settings {indicator_settings.sma_periods}"
                )
        if self._rsi_period != indicator_settings.rsi_period:
            raise StrategyError(
                f"RSI period {self._rsi_period} != "
                f"indicator setting {indicator_settings.rsi_period}"
            )
        if self._volume_sma_period != indicator_settings.volume_sma_period:
            raise StrategyError(
                f"Volume SMA period {self._volume_sma_period} != "
                f"indicator setting {indicator_settings.volume_sma_period}"
            )
        if self._volume_mult < 1.0:
            raise StrategyError(f"volume_mult must be >= 1.0, got {self._volume_mult}")

        # Column names
        self._col_sma_long = f"sma_{self._sma_long}"
        self._col_sma_fast = f"sma_{self._sma_fast}"
        self._col_sma_mid = f"sma_{self._sma_mid}"
        self._col_rsi = f"rsi_{self._rsi_period}"
        self._col_atr = f"atr_{self._atr_period}"
        self._col_vol_sma = f"volume_sma_{self._volume_sma_period}"

        self._required_cols = {
            self._col_sma_long, self._col_sma_fast, self._col_sma_mid,
            self._col_rsi, self._col_atr, self._col_vol_sma,
            "close", "volume",
        }

    @property
    def name(self) -> str:
        return "etf_momentum"

    @property
    def universe(self) -> list[str]:
        return list(self._universe)

    @property
    def min_history_days(self) -> int:
        return max(
            self._sma_long, self._rsi_period,
            self._volume_sma_period, self._atr_period,
        )

    @property
    def time_stop_days(self) -> int:
        return self._time_stop

    def evaluate(self, universe: dict[str, pd.DataFrame]) -> list[Signal]:
        """Evaluate strategy on enriched DataFrames."""
        return self._evaluate_universe(universe, self._evaluate_symbol)

    def _evaluate_symbol(self, symbol: str, df: pd.DataFrame) -> Signal | None:
        """Evaluate a single symbol. Returns at most one signal."""
        last = df.iloc[-1]
        close = float(last["close"])
        rsi = float(last[self._col_rsi])
        sma_fast = float(last[self._col_sma_fast])
        sma_mid = float(last[self._col_sma_mid])
        sma_long = float(last[self._col_sma_long])
        atr = float(last[self._col_atr])
        volume = float(last["volume"])
        vol_sma = float(last[self._col_vol_sma])

        stop_loss_pct = self._atr_stop_mult * atr / close
        if stop_loss_pct <= 0:
            raise StrategyError(f"Non-positive stop_loss_pct for {symbol}: {stop_loss_pct}")

        now = datetime.now(UTC)

        # Check EXIT conditions first
        if rsi > self._rsi_overbought:
            return Signal(
                symbol=symbol,
                direction=SignalDirection.SELL,
                strength=1.0,
                stop_loss_pct=stop_loss_pct,
                strategy_name=self.name,
                reason=f"SELL: rsi={rsi:.1f} > {self._rsi_overbought} overbought",
                timestamp=now,
            )

        if sma_fast < sma_mid:
            return Signal(
                symbol=symbol,
                direction=SignalDirection.SELL,
                strength=1.0,
                stop_loss_pct=stop_loss_pct,
                strategy_name=self.name,
                reason=(
                    f"SELL: sma{self._sma_fast}={sma_fast:.2f} < "
                    f"sma{self._sma_mid}={sma_mid:.2f} trend reversal"
                ),
                timestamp=now,
            )

        # Check all ENTRY conditions
        # 1. Price above long-term SMA
        if close <= sma_long:
            return None

        # 2. RSI bounce: current above threshold AND at least one of previous 3 below
        if rsi <= self._rsi_oversold:
            return None
        if len(df) < 2:
            return None
        lookback = df.iloc[-4:-1] if len(df) >= 4 else df.iloc[:-1]
        rsi_bounce = (lookback[self._col_rsi] < self._rsi_oversold).any()
        if not rsi_bounce:
            return None

        # 3. SMA fast > SMA mid (strictly greater — the SELL branch covers <, this rejects ==)
        if sma_fast <= sma_mid:
            return None

        # 4. Volume confirmation
        if volume <= self._volume_mult * vol_sma:
            return None

        # All entry conditions met — BUY
        vol_ratio = volume / vol_sma if vol_sma > 0 else 0.0
        strength = max(0.0, min(1.0, vol_ratio - 1.0))

        return Signal(
            symbol=symbol,
            direction=SignalDirection.BUY,
            strength=strength,
            stop_loss_pct=stop_loss_pct,
            strategy_name=self.name,
            reason=(
                f"BUY: close={close:.2f} > sma{self._sma_long}={sma_long:.2f}, "
                f"rsi={rsi:.1f} bounce, "
                f"sma{self._sma_fast}={sma_fast:.2f} > sma{self._sma_mid}={sma_mid:.2f}, "
                f"vol_ratio={vol_ratio:.1f}x"
            ),
            timestamp=now,
        )
