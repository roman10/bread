"""Unit tests for Bollinger Band Squeeze Breakout strategy."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml

from bread.core.config import IndicatorSettings
from bread.core.exceptions import StrategyError
from bread.core.models import SignalDirection
from bread.strategy.breakout_squeeze import BreakoutSqueeze


@pytest.fixture()
def indicator_settings() -> IndicatorSettings:
    return IndicatorSettings()


@pytest.fixture()
def strategy_config(tmp_path: Path) -> Path:
    cfg = {
        "universe": ["SPY"],
        "entry": {
            "bollinger_period": 20,
            "bollinger_stddev": 2.0,
            "squeeze_lookback": 10,  # smaller for testing
            "squeeze_percentile": 20.0,
            "volume_sma_period": 20,
            "volume_mult": 1.2,
        },
        "exit": {
            "atr_stop_mult": 1.5,
            "time_stop_days": 10,
        },
    }
    p = tmp_path / "breakout_squeeze.yaml"
    p.write_text(yaml.dump(cfg))
    return p


def _make_enriched_df(
    rows: int = 15,
    close: float = 465.0,
    bb_lower_values: list[float] | None = None,
    bb_mid_values: list[float] | None = None,
    bb_upper_values: list[float] | None = None,
    volume: float = 2_500_000,
    volume_sma: float = 1_500_000,
    atr: float = 5.0,
    macd_hist: float = 0.5,
) -> pd.DataFrame:
    """Build a DataFrame for squeeze breakout testing.

    Defaults: tight bands (squeeze) on most bars with current bar breaking above upper.
    """
    if bb_lower_values is None:
        # Most bars have tight bands (squeeze), gradually widening
        bb_lower_values = [448.0] * (rows - 1) + [448.0]
    if bb_mid_values is None:
        bb_mid_values = [450.0] * rows
    if bb_upper_values is None:
        # Tight bands for most bars, current bar's close > upper
        bb_upper_values = [452.0] * (rows - 1) + [460.0]

    for lst in (bb_lower_values, bb_mid_values, bb_upper_values):
        if len(lst) < rows:
            lst[:] = [lst[0]] * (rows - len(lst)) + lst

    dates = pd.bdate_range(start="2024-06-01", periods=rows, tz="UTC")

    df = pd.DataFrame(
        {
            "open": [close - 1.0] * rows,
            "high": [close + 2.0] * rows,
            "low": [close - 2.0] * rows,
            "close": [close] * rows,
            "volume": [volume] * rows,
            "sma_200": [400.0] * rows,
            "sma_20": [440.0] * rows,
            "sma_50": [430.0] * rows,
            "rsi_14": [50.0] * rows,
            "atr_14": [atr] * rows,
            "volume_sma_20": [volume_sma] * rows,
            "ema_9": [close] * rows,
            "ema_21": [close] * rows,
            "macd": [0.5] * rows,
            "macd_signal": [0.3] * rows,
            "macd_hist": [macd_hist] * rows,
            "bb_lower_20_2": bb_lower_values[:rows],
            "bb_mid_20_2": bb_mid_values[:rows],
            "bb_upper_20_2": bb_upper_values[:rows],
        },
        index=pd.DatetimeIndex(dates, name="timestamp"),
    )
    return df


class TestBreakoutSqueezeBuy:
    def test_all_entry_conditions_met(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = BreakoutSqueeze(strategy_config, indicator_settings)
        # Historical bars: wide bands; current bar: tight bands (squeeze) with close > upper
        # Wide width = (470-430)/450 = 0.089; tight width = (454-446)/450 = 0.018
        df = _make_enriched_df(
            close=455.0,
            bb_upper_values=[470.0] * 14 + [454.0],
            bb_lower_values=[430.0] * 14 + [446.0],
            bb_mid_values=[450.0] * 15,
        )
        signals = strat.evaluate({"SPY": df})

        assert len(signals) == 1
        sig = signals[0]
        assert sig.direction == SignalDirection.BUY
        assert sig.strategy_name == "breakout_squeeze"
        assert 0.0 <= sig.strength <= 1.0
        assert "squeeze" in sig.reason

    def test_no_squeeze_no_buy(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = BreakoutSqueeze(strategy_config, indicator_settings)
        # Historical bars: tight bands; current bar: WIDER bands (not a squeeze)
        # Tight width = (454-446)/450 = 0.018; wide width = (470-430)/450 = 0.089
        df = _make_enriched_df(
            close=475.0,
            bb_upper_values=[454.0] * 14 + [470.0],
            bb_lower_values=[446.0] * 14 + [430.0],
            bb_mid_values=[450.0] * 15,
        )
        signals = strat.evaluate({"SPY": df})
        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        assert len(buy_signals) == 0

    def test_close_below_upper_no_buy(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = BreakoutSqueeze(strategy_config, indicator_settings)
        # Squeeze active but close is below upper band
        df = _make_enriched_df(close=451.0)
        signals = strat.evaluate({"SPY": df})
        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        assert len(buy_signals) == 0

    def test_low_volume_no_buy(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = BreakoutSqueeze(strategy_config, indicator_settings)
        df = _make_enriched_df(
            close=465.0, volume=1_000_000, volume_sma=1_500_000,
            bb_upper_values=[452.0] * 14 + [460.0],
            bb_lower_values=[448.0] * 15,
        )
        signals = strat.evaluate({"SPY": df})
        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        assert len(buy_signals) == 0

    def test_negative_macd_hist_no_buy(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = BreakoutSqueeze(strategy_config, indicator_settings)
        df = _make_enriched_df(
            close=465.0, macd_hist=-0.5,
            bb_upper_values=[452.0] * 14 + [460.0],
            bb_lower_values=[448.0] * 15,
        )
        signals = strat.evaluate({"SPY": df})
        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        assert len(buy_signals) == 0


class TestBreakoutSqueezeSell:
    def test_below_mid_band_sell(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = BreakoutSqueeze(strategy_config, indicator_settings)
        # Close below mid band
        df = _make_enriched_df(close=445.0)
        signals = strat.evaluate({"SPY": df})

        assert len(signals) == 1
        assert signals[0].direction == SignalDirection.SELL
        assert "breakout failed" in signals[0].reason

    def test_negative_macd_hist_sell(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = BreakoutSqueeze(strategy_config, indicator_settings)
        # Close above mid but MACD histogram negative
        df = _make_enriched_df(close=455.0, macd_hist=-0.3)
        signals = strat.evaluate({"SPY": df})

        assert len(signals) == 1
        assert signals[0].direction == SignalDirection.SELL
        assert "momentum fading" in signals[0].reason


class TestBreakoutSqueezeErrors:
    def test_missing_indicator_column(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = BreakoutSqueeze(strategy_config, indicator_settings)
        df = _make_enriched_df()
        df = df.drop(columns=["bb_upper_20_2"])
        with pytest.raises(StrategyError, match="Missing indicator columns"):
            strat.evaluate({"SPY": df})

    def test_empty_dataframe(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = BreakoutSqueeze(strategy_config, indicator_settings)
        df = pd.DataFrame(
            columns=["close", "volume", "bb_lower_20_2", "bb_mid_20_2",
                      "bb_upper_20_2", "atr_14", "volume_sma_20", "macd_hist"]
        )
        with pytest.raises(StrategyError, match="Empty DataFrame"):
            strat.evaluate({"SPY": df})

    def test_missing_symbol_skipped(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = BreakoutSqueeze(strategy_config, indicator_settings)
        signals = strat.evaluate({})
        assert signals == []


class TestBreakoutSqueezeProperties:
    def test_name(self, strategy_config: Path, indicator_settings: IndicatorSettings) -> None:
        strat = BreakoutSqueeze(strategy_config, indicator_settings)
        assert strat.name == "breakout_squeeze"

    def test_time_stop_days(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = BreakoutSqueeze(strategy_config, indicator_settings)
        assert strat.time_stop_days == 10
