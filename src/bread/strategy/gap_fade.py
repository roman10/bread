"""Opening Gap Fade strategy implementation."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from bread.core.config import IndicatorSettings
from bread.core.exceptions import StrategyError
from bread.core.models import Signal, SignalDirection
from bread.data.indicator_specs import ATR, RSI, SMA
from bread.strategy.base import Strategy, load_strategy_config
from bread.strategy.registry import register

logger = logging.getLogger(__name__)


@register("gap_fade")
class GapFade(Strategy):
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
        self._gap_threshold: float = entry.get("gap_threshold", 0.015)
        self._rsi_period: int = entry.get("rsi_period", 14)
        self._rsi_entry_max: float = entry.get("rsi_entry_max", 40)
        self._sma_trend: int = entry.get("sma_trend", 200)

        # Exit params
        self._rsi_exit: float = exit_.get("rsi_exit", 60)
        self._atr_stop_mult: float = exit_.get("atr_stop_mult", 2.5)
        self._time_stop: int = exit_.get("time_stop_days", 7)

        self._atr_period: int = indicator_settings.atr_period

        rsi = RSI(self._rsi_period)
        atr = ATR(self._atr_period)
        sma_trend = SMA(self._sma_trend)
        self._declare_indicators(
            indicator_settings, rsi, atr, sma_trend,
            extras={"open", "close"},
        )

        self._col_rsi = rsi.column
        self._col_atr = atr.column
        self._col_sma_trend = sma_trend.column

    @property
    def name(self) -> str:
        return "gap_fade"

    @property
    def universe(self) -> list[str]:
        return list(self._universe)

    @property
    def min_history_days(self) -> int:
        return max(self._sma_trend, self._rsi_period, self._atr_period)

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
        open_price = float(last["open"])
        close = float(last["close"])
        prev_close = float(prev["close"])
        rsi = float(last[self._col_rsi])
        atr = float(last[self._col_atr])
        sma_trend = float(last[self._col_sma_trend])

        stop_loss_pct = self._atr_stop_mult * atr / close
        if stop_loss_pct <= 0:
            raise StrategyError(f"Non-positive stop_loss_pct for {symbol}: {stop_loss_pct}")

        now = datetime.now(UTC)

        # Check EXIT conditions first
        # RSI exit
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

        # Check ENTRY conditions
        if prev_close <= 0:
            return None

        # 1. Gap down: open is below prev close by threshold
        gap_pct = (prev_close - open_price) / prev_close
        if gap_pct < self._gap_threshold:
            return None

        # 2. Close > open: buying pressure during the day (gap partially filling)
        if close <= open_price:
            return None

        # 3. RSI below entry max
        if rsi >= self._rsi_entry_max:
            return None

        # 4. Price above long-term trend (only fade gaps in uptrends)
        if close <= sma_trend:
            return None

        # All entry conditions met -- BUY
        # Strength: size of the gap relative to ATR
        gap_size = abs(gap_pct)
        strength = max(0.0, min(1.0, gap_size * close / atr)) if atr > 0 else 0.5

        return Signal(
            symbol=symbol,
            direction=SignalDirection.BUY,
            strength=strength,
            stop_loss_pct=stop_loss_pct,
            strategy_name=self.name,
            reason=(
                f"BUY: gap_down={gap_pct*100:.1f}%, "
                f"close={close:.2f} > open={open_price:.2f} (buying pressure), "
                f"rsi={rsi:.1f}, close > sma{self._sma_trend}={sma_trend:.2f}"
            ),
            timestamp=now,
        )
