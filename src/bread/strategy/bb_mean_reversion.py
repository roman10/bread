"""Bollinger Band Mean Reversion strategy implementation."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import yaml

from bread.core.config import IndicatorSettings
from bread.core.exceptions import StrategyError
from bread.core.models import Signal, SignalDirection
from bread.strategy.base import Strategy
from bread.strategy.registry import register

logger = logging.getLogger(__name__)


def _fmt_stddev(v: float) -> str:
    return str(int(v)) if v == int(v) else str(v)


@register("bb_mean_reversion")
class BbMeanReversion(Strategy):
    def __init__(self, config_path: Path, indicator_settings: IndicatorSettings) -> None:
        """Load strategy-specific config from YAML."""
        try:
            with open(config_path) as f:
                cfg = yaml.safe_load(f)
        except Exception as exc:
            raise StrategyError(f"Failed to load strategy config: {config_path}: {exc}") from exc

        if not isinstance(cfg, dict):
            raise StrategyError(f"Invalid strategy config format in {config_path}")

        self._universe: list[str] = cfg.get("universe", [])
        entry = cfg.get("entry", {})
        exit_ = cfg.get("exit", {})

        # Entry params
        self._bollinger_period: int = entry.get("bollinger_period", 20)
        self._bollinger_stddev: float = entry.get("bollinger_stddev", 2.0)
        self._rsi_period: int = entry.get("rsi_period", 14)
        self._rsi_entry_threshold: float = entry.get("rsi_entry_threshold", 40)

        # Exit params
        self._rsi_exit_threshold: float = exit_.get("rsi_exit_threshold", 60)
        self._atr_stop_mult: float = exit_.get("atr_stop_mult", 2.0)
        self._time_stop: int = exit_.get("time_stop_days", 10)

        self._atr_period: int = indicator_settings.atr_period

        # Validate indicator compatibility
        if self._bollinger_period != indicator_settings.bollinger_period:
            raise StrategyError(
                f"Bollinger period {self._bollinger_period} != "
                f"indicator setting {indicator_settings.bollinger_period}"
            )
        if self._bollinger_stddev != indicator_settings.bollinger_stddev:
            raise StrategyError(
                f"Bollinger stddev {self._bollinger_stddev} != "
                f"indicator setting {indicator_settings.bollinger_stddev}"
            )
        if self._rsi_period != indicator_settings.rsi_period:
            raise StrategyError(
                f"RSI period {self._rsi_period} != "
                f"indicator setting {indicator_settings.rsi_period}"
            )

        # Column names
        sdv = _fmt_stddev(self._bollinger_stddev)
        bp = self._bollinger_period
        self._col_bb_lower = f"bb_lower_{bp}_{sdv}"
        self._col_bb_mid = f"bb_mid_{bp}_{sdv}"
        self._col_bb_upper = f"bb_upper_{bp}_{sdv}"
        self._col_rsi = f"rsi_{self._rsi_period}"
        self._col_atr = f"atr_{self._atr_period}"

        self._required_cols = {
            self._col_bb_lower,
            self._col_bb_mid,
            self._col_bb_upper,
            self._col_rsi,
            self._col_atr,
            "close",
        }

    @property
    def name(self) -> str:
        return "bb_mean_reversion"

    @property
    def universe(self) -> list[str]:
        return list(self._universe)

    @property
    def min_history_days(self) -> int:
        return max(self._bollinger_period, self._rsi_period, self._atr_period)

    @property
    def time_stop_days(self) -> int:
        return self._time_stop

    def evaluate(self, universe: dict[str, pd.DataFrame]) -> list[Signal]:
        """Evaluate strategy on enriched DataFrames."""
        signals: list[Signal] = []

        for symbol in self._universe:
            if symbol not in universe:
                continue

            df = universe[symbol]
            if df.empty:
                raise StrategyError(f"Empty DataFrame for {symbol}")

            missing = self._required_cols - set(df.columns)
            if missing:
                raise StrategyError(f"Missing indicator columns for {symbol}: {missing}")

            signal = self._evaluate_symbol(symbol, df)
            if signal is not None:
                signals.append(signal)

        return signals

    def _evaluate_symbol(self, symbol: str, df: pd.DataFrame) -> Signal | None:
        """Evaluate a single symbol. Returns at most one signal."""
        last = df.iloc[-1]
        close = float(last["close"])
        rsi = float(last[self._col_rsi])
        bb_lower = float(last[self._col_bb_lower])
        bb_mid = float(last[self._col_bb_mid])
        bb_upper = float(last[self._col_bb_upper])
        atr = float(last[self._col_atr])

        stop_loss_pct = self._atr_stop_mult * atr / close
        if stop_loss_pct <= 0:
            raise StrategyError(f"Non-positive stop_loss_pct for {symbol}: {stop_loss_pct}")

        now = datetime.now(UTC)

        # Check EXIT conditions first
        if close >= bb_mid:
            return Signal(
                symbol=symbol,
                direction=SignalDirection.SELL,
                strength=1.0,
                stop_loss_pct=stop_loss_pct,
                strategy_name=self.name,
                reason=(
                    f"SELL: close={close:.2f} >= bb_mid={bb_mid:.2f} mean reversion target reached"
                ),
                timestamp=now,
            )

        if rsi >= self._rsi_exit_threshold:
            return Signal(
                symbol=symbol,
                direction=SignalDirection.SELL,
                strength=1.0,
                stop_loss_pct=stop_loss_pct,
                strategy_name=self.name,
                reason=(f"SELL: rsi={rsi:.1f} >= {self._rsi_exit_threshold} exit threshold"),
                timestamp=now,
            )

        # Check ENTRY conditions
        # 1. Close below lower Bollinger Band
        if close >= bb_lower:
            return None

        # 2. RSI below entry threshold
        if rsi >= self._rsi_entry_threshold:
            return None

        # Compute strength: how far below the lower band relative to band width
        band_width = bb_upper - bb_lower
        if band_width <= 0:
            return None

        strength = max(0.0, min(1.0, (bb_lower - close) / band_width))

        return Signal(
            symbol=symbol,
            direction=SignalDirection.BUY,
            strength=strength,
            stop_loss_pct=stop_loss_pct,
            strategy_name=self.name,
            reason=(
                f"BUY: close={close:.2f} < bb_lower={bb_lower:.2f}, "
                f"rsi={rsi:.1f} < {self._rsi_entry_threshold}, "
                f"band_depth={strength:.2f}"
            ),
            timestamp=now,
        )
