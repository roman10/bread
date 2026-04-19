"""EMA 9/21 Crossover with Volatility Filter strategy implementation."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from bread.core.config import IndicatorSettings
from bread.core.exceptions import StrategyError
from bread.core.models import Signal, SignalDirection
from bread.data.indicator_specs import ATR, EMA, RSI, SMA
from bread.strategy.base import Strategy, load_strategy_config
from bread.strategy.registry import register

logger = logging.getLogger(__name__)


@register("ema_crossover")
class EmaCrossover(Strategy):
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
        self._ema_fast: int = entry.get("ema_fast", 9)
        self._ema_slow: int = entry.get("ema_slow", 21)
        self._sma_trend: int = entry.get("sma_trend", 200)
        self._max_atr_pct: float = entry.get("max_atr_pct", 0.03)
        self._rsi_period: int = entry.get("rsi_period", 14)
        self._rsi_min: float = entry.get("rsi_min", 40)
        self._rsi_max: float = entry.get("rsi_max", 65)

        # Exit params
        self._rsi_exit: float = exit_.get("rsi_exit", 75)
        self._atr_stop_mult: float = exit_.get("atr_stop_mult", 1.5)
        self._time_stop: int = exit_.get("time_stop_days", 8)

        self._atr_period: int = indicator_settings.atr_period

        ema_fast = EMA(self._ema_fast)
        ema_slow = EMA(self._ema_slow)
        sma_trend = SMA(self._sma_trend)
        rsi = RSI(self._rsi_period)
        atr = ATR(self._atr_period)
        self._declare_indicators(
            indicator_settings, ema_fast, ema_slow, sma_trend, rsi, atr,
            extras={"close"},
        )

        self._col_ema_fast = ema_fast.column
        self._col_ema_slow = ema_slow.column
        self._col_sma_trend = sma_trend.column
        self._col_rsi = rsi.column
        self._col_atr = atr.column

    @property
    def name(self) -> str:
        return "ema_crossover"

    @property
    def universe(self) -> list[str]:
        return list(self._universe)

    @property
    def min_history_days(self) -> int:
        return max(self._sma_trend, self._ema_slow, self._rsi_period, self._atr_period)

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
        ema_fast = float(last[self._col_ema_fast])
        ema_slow = float(last[self._col_ema_slow])
        ema_fast_prev = float(prev[self._col_ema_fast])
        ema_slow_prev = float(prev[self._col_ema_slow])
        sma_trend = float(last[self._col_sma_trend])
        rsi = float(last[self._col_rsi])
        atr = float(last[self._col_atr])

        stop_loss_pct = self._atr_stop_mult * atr / close
        if stop_loss_pct <= 0:
            raise StrategyError(f"Non-positive stop_loss_pct for {symbol}: {stop_loss_pct}")

        now = datetime.now(UTC)

        # Check EXIT conditions first
        # EMA fast crosses below EMA slow
        if ema_fast < ema_slow and ema_fast_prev >= ema_slow_prev:
            return Signal(
                symbol=symbol,
                direction=SignalDirection.SELL,
                strength=1.0,
                stop_loss_pct=stop_loss_pct,
                strategy_name=self.name,
                reason=(
                    f"SELL: ema{self._ema_fast}={ema_fast:.2f} crossed below "
                    f"ema{self._ema_slow}={ema_slow:.2f}"
                ),
                timestamp=now,
            )

        # RSI overbought exit
        if rsi > self._rsi_exit:
            return Signal(
                symbol=symbol,
                direction=SignalDirection.SELL,
                strength=1.0,
                stop_loss_pct=stop_loss_pct,
                strategy_name=self.name,
                reason=f"SELL: rsi={rsi:.1f} > {self._rsi_exit} take-profit exit",
                timestamp=now,
            )

        # Check ENTRY conditions
        # 1. EMA fast crosses above EMA slow
        if not (ema_fast > ema_slow and ema_fast_prev <= ema_slow_prev):
            return None

        # 2. Price above long-term SMA trend filter
        if close <= sma_trend:
            return None

        # 3. Volatility regime filter: skip high-volatility environments
        atr_pct = atr / close if close > 0 else 0.0
        if atr_pct >= self._max_atr_pct:
            return None

        # 4. RSI in acceptable range (not overbought, not deeply oversold)
        if rsi < self._rsi_min or rsi > self._rsi_max:
            return None

        # All entry conditions met -- BUY
        # Strength: distance of EMA fast above EMA slow, normalized by ATR
        ema_spread = (ema_fast - ema_slow) / atr if atr > 0 else 0.0
        strength = max(0.0, min(1.0, ema_spread))

        return Signal(
            symbol=symbol,
            direction=SignalDirection.BUY,
            strength=strength,
            stop_loss_pct=stop_loss_pct,
            strategy_name=self.name,
            reason=(
                f"BUY: ema{self._ema_fast}={ema_fast:.2f} crossed above "
                f"ema{self._ema_slow}={ema_slow:.2f}, "
                f"close={close:.2f} > sma{self._sma_trend}={sma_trend:.2f}, "
                f"atr_pct={atr_pct:.3f}, rsi={rsi:.1f}"
            ),
            timestamp=now,
        )
