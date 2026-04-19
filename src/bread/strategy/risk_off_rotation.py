"""Cross-Asset Risk-On/Risk-Off Regime Rotation strategy implementation."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from bread.core.config import IndicatorSettings
from bread.core.exceptions import StrategyError
from bread.core.models import Signal, SignalDirection
from bread.data.indicator_specs import ATR, SMA, ReturnPct
from bread.strategy.base import Strategy, load_strategy_config
from bread.strategy.registry import register

logger = logging.getLogger(__name__)


@register("risk_off_rotation")
class RiskOffRotation(Strategy):
    def __init__(
        self, config_path: Path, indicator_settings: IndicatorSettings,
        *, universe: list[str] | None = None,
    ) -> None:
        """Load strategy-specific config from YAML."""
        cfg = load_strategy_config(config_path)

        self._universe: list[str] = universe if universe is not None else cfg.get("universe", [])
        entry = cfg.get("entry", {})
        exit_ = cfg.get("exit", {})

        # Regime detection params
        self._regime_symbol: str = entry.get("regime_symbol", "SPY")
        self._safe_haven_symbol: str = entry.get("safe_haven_symbol", "TLT")
        self._regime_return_period: int = entry.get("regime_return_period", 20)
        self._momentum_return_period: int = entry.get("momentum_return_period", 10)
        self._sma_trend: int = entry.get("sma_trend", 50)

        # Risk-on vs risk-off universe
        self._risk_on_symbols: list[str] = entry.get(
            "risk_on_symbols", ["SPY", "QQQ", "IWM", "DIA", "XLF", "XLK", "XLE", "XLV"]
        )
        self._risk_off_symbols: list[str] = entry.get(
            "risk_off_symbols", ["TLT", "GLD"]
        )

        # Exit params
        self._atr_stop_mult: float = exit_.get("atr_stop_mult", 2.0)
        self._time_stop: int = exit_.get("time_stop_days", 20)

        self._atr_period: int = indicator_settings.atr_period

        sma_trend = SMA(self._sma_trend)
        atr = ATR(self._atr_period)
        regime_return = ReturnPct(self._regime_return_period)
        momentum_return = ReturnPct(self._momentum_return_period)
        self._declare_indicators(
            indicator_settings, sma_trend, atr, regime_return, momentum_return,
            extras={"close"},
        )

        self._col_sma_trend = sma_trend.column
        self._col_atr = atr.column
        self._col_regime_return = regime_return.column
        self._col_momentum_return = momentum_return.column

    @property
    def name(self) -> str:
        return "risk_off_rotation"

    @property
    def universe(self) -> list[str]:
        return list(self._universe)

    @property
    def min_history_days(self) -> int:
        return max(
            self._sma_trend, self._atr_period,
            self._regime_return_period, self._momentum_return_period,
        )

    @property
    def time_stop_days(self) -> int:
        return self._time_stop

    def _validate_df(self, symbol: str, df: pd.DataFrame) -> None:
        """Validate DataFrame has required columns."""
        if df.empty:
            raise StrategyError(f"Empty DataFrame for {symbol}")
        missing = self._required_cols - set(df.columns)
        if missing:
            raise StrategyError(f"Missing indicator columns for {symbol}: {missing}")

    def _detect_regime(self, universe: dict[str, pd.DataFrame]) -> str | None:
        """Detect risk-on or risk-off regime.

        Returns "risk_on", "risk_off", or None if regime cannot be determined.
        """
        if self._regime_symbol not in universe or self._safe_haven_symbol not in universe:
            return None

        regime_df = universe[self._regime_symbol]
        safe_df = universe[self._safe_haven_symbol]

        self._validate_df(self._regime_symbol, regime_df)
        self._validate_df(self._safe_haven_symbol, safe_df)

        regime_last = regime_df.iloc[-1]
        safe_last = safe_df.iloc[-1]

        regime_return = float(regime_last[self._col_regime_return])
        safe_return = float(safe_last[self._col_regime_return])
        regime_close = float(regime_last["close"])
        regime_sma = float(regime_last[self._col_sma_trend])

        if regime_return > safe_return and regime_close > regime_sma:
            return "risk_on"
        return "risk_off"

    def evaluate(self, universe: dict[str, pd.DataFrame]) -> list[Signal]:
        """Evaluate strategy based on regime detection and momentum ranking."""
        now = datetime.now(UTC)
        signals: list[Signal] = []

        regime = self._detect_regime(universe)
        if regime is None:
            return []

        # Select which symbols to consider based on regime
        if regime == "risk_on":
            active_symbols = self._risk_on_symbols
            inactive_symbols = self._risk_off_symbols
        else:
            active_symbols = self._risk_off_symbols
            inactive_symbols = self._risk_on_symbols

        # Generate SELL signals for inactive regime symbols
        for symbol in inactive_symbols:
            if symbol not in universe:
                continue
            df = universe[symbol]
            self._validate_df(symbol, df)

            last = df.iloc[-1]
            close = float(last["close"])
            atr = float(last[self._col_atr])
            stop_loss_pct = self._atr_stop_mult * atr / close
            if stop_loss_pct <= 0:
                raise StrategyError(f"Non-positive stop_loss_pct for {symbol}: {stop_loss_pct}")

            signals.append(Signal(
                symbol=symbol,
                direction=SignalDirection.SELL,
                strength=1.0,
                stop_loss_pct=stop_loss_pct,
                strategy_name=self.name,
                reason=f"SELL: regime={regime}, {symbol} in inactive group",
                timestamp=now,
            ))

        # Rank active symbols by momentum and generate BUY for the best
        candidates: list[tuple[str, float, float, float, float]] = []
        # (symbol, momentum, close, sma, atr)

        for symbol in active_symbols:
            if symbol not in universe:
                continue
            df = universe[symbol]
            self._validate_df(symbol, df)

            last = df.iloc[-1]
            close = float(last["close"])
            sma = float(last[self._col_sma_trend])
            atr = float(last[self._col_atr])
            momentum = float(last[self._col_momentum_return])

            # Only consider symbols above trend and with positive momentum
            if close > sma and momentum > 0:
                candidates.append((symbol, momentum, close, sma, atr))

        # Sort by momentum descending, take the best one
        candidates.sort(key=lambda x: (-x[1], x[0]))

        if candidates:
            symbol, momentum, close, sma, atr = candidates[0]
            stop_loss_pct = self._atr_stop_mult * atr / close
            if stop_loss_pct <= 0:
                raise StrategyError(f"Non-positive stop_loss_pct for {symbol}: {stop_loss_pct}")

            max_momentum = max(abs(c[1]) for c in candidates) if candidates else 1.0
            strength = max(0.0, min(1.0, momentum / max_momentum)) if max_momentum > 0 else 0.5

            signals.append(Signal(
                symbol=symbol,
                direction=SignalDirection.BUY,
                strength=strength,
                stop_loss_pct=stop_loss_pct,
                strategy_name=self.name,
                reason=(
                    f"BUY: regime={regime}, {symbol} top momentum={momentum:.4f}, "
                    f"close={close:.2f} > sma{self._sma_trend}={sma:.2f}"
                ),
                timestamp=now,
            ))

        return signals
