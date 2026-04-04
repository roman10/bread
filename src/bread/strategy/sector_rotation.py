"""Relative Strength Sector Rotation strategy implementation."""

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


@register("sector_rotation")
class SectorRotation(Strategy):
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
        self._top_n: int = entry.get("top_n", 3)
        self._sma_trend: int = entry.get("sma_trend", 50)
        self._return_weights: dict[int, float] = {}
        for rw in entry.get("return_weights", [{"period": 20, "weight": 0.5},
                                                 {"period": 10, "weight": 0.3},
                                                 {"period": 5, "weight": 0.2}]):
            self._return_weights[rw["period"]] = rw["weight"]

        # Exit params
        self._exit_rank: int = exit_.get("exit_rank", 5)
        self._atr_stop_mult: float = exit_.get("atr_stop_mult", 2.0)
        self._time_stop: int = exit_.get("time_stop_days", 20)

        self._atr_period: int = indicator_settings.atr_period

        # Validate indicator compatibility
        if self._sma_trend not in indicator_settings.sma_periods:
            raise StrategyError(
                f"SMA period {self._sma_trend} not in "
                f"indicator settings {indicator_settings.sma_periods}"
            )
        for period in self._return_weights:
            if period not in indicator_settings.return_periods:
                raise StrategyError(
                    f"Return period {period} not in "
                    f"indicator settings {indicator_settings.return_periods}"
                )

        # Column names
        self._col_sma_trend = f"sma_{self._sma_trend}"
        self._col_atr = f"atr_{self._atr_period}"
        self._return_cols = {p: f"return_{p}d" for p in self._return_weights}

        self._required_cols = {
            "close", self._col_sma_trend, self._col_atr,
        } | set(self._return_cols.values())

    @property
    def name(self) -> str:
        return "sector_rotation"

    @property
    def universe(self) -> list[str]:
        return list(self._universe)

    @property
    def min_history_days(self) -> int:
        return max(
            self._sma_trend,
            self._atr_period,
            max(self._return_weights.keys()) if self._return_weights else 20,
        )

    @property
    def time_stop_days(self) -> int:
        return self._time_stop

    def evaluate(self, universe: dict[str, pd.DataFrame]) -> list[Signal]:
        """Evaluate strategy by ranking all symbols by composite momentum."""
        now = datetime.now(UTC)

        # Compute composite scores for all symbols
        scores: dict[str, tuple[float, float, float, float]] = {}
        # symbol -> (composite_score, close, sma_trend, atr)

        for symbol in self._universe:
            if symbol not in universe:
                continue

            df = universe[symbol]
            if df.empty:
                raise StrategyError(f"Empty DataFrame for {symbol}")

            missing = self._required_cols - set(df.columns)
            if missing:
                raise StrategyError(
                    f"Missing indicator columns for {symbol}: {missing}"
                )

            last = df.iloc[-1]
            close = float(last["close"])
            sma_trend = float(last[self._col_sma_trend])
            atr = float(last[self._col_atr])

            # Compute weighted composite score
            score = 0.0
            for period, weight in self._return_weights.items():
                ret = float(last[self._return_cols[period]])
                score += weight * ret

            scores[symbol] = (score, close, sma_trend, atr)

        if not scores:
            return []

        # Rank by composite score descending
        ranked = sorted(scores.items(), key=lambda x: (-x[1][0], x[0]))

        # Normalize scores for strength calculation
        max_score = max(abs(s[0]) for s in scores.values()) if scores else 1.0
        if max_score == 0:
            max_score = 1.0

        signals: list[Signal] = []

        for rank_idx, (symbol, (score, close, sma_trend, atr)) in enumerate(ranked):
            rank = rank_idx + 1  # 1-based

            stop_loss_pct = self._atr_stop_mult * atr / close
            if stop_loss_pct <= 0:
                raise StrategyError(f"Non-positive stop_loss_pct for {symbol}: {stop_loss_pct}")

            # EXIT: symbol falls out of top exit_rank OR below SMA trend OR negative score
            if rank > self._exit_rank or close < sma_trend or score < 0:
                signals.append(Signal(
                    symbol=symbol,
                    direction=SignalDirection.SELL,
                    strength=1.0,
                    stop_loss_pct=stop_loss_pct,
                    strategy_name=self.name,
                    reason=(
                        f"SELL: rank={rank}, score={score:.4f}, "
                        f"close={close:.2f}, sma{self._sma_trend}={sma_trend:.2f}"
                    ),
                    timestamp=now,
                ))
                continue

            # ENTRY: symbol in top_n AND above SMA trend AND positive score
            if rank <= self._top_n and close > sma_trend and score > 0:
                strength = max(0.0, min(1.0, score / max_score))
                signals.append(Signal(
                    symbol=symbol,
                    direction=SignalDirection.BUY,
                    strength=strength,
                    stop_loss_pct=stop_loss_pct,
                    strategy_name=self.name,
                    reason=(
                        f"BUY: rank={rank}/{len(ranked)}, score={score:.4f}, "
                        f"close={close:.2f} > sma{self._sma_trend}={sma_trend:.2f}"
                    ),
                    timestamp=now,
                ))

        return signals
