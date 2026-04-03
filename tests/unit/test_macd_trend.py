"""Unit tests for MACD Trend Following strategy."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml

from bread.core.config import IndicatorSettings
from bread.core.exceptions import StrategyError
from bread.core.models import SignalDirection
from bread.strategy.macd_trend import MacdTrend


@pytest.fixture()
def indicator_settings() -> IndicatorSettings:
    return IndicatorSettings()


@pytest.fixture()
def strategy_config(tmp_path: Path) -> Path:
    cfg = {
        "universe": ["SPY"],
        "entry": {
            "ema_trend_filter": 21,
            "volume_sma_period": 20,
            "volume_mult": 1.0,
            "require_macd_above_signal": True,
        },
        "exit": {
            "atr_stop_mult": 1.5,
            "time_stop_days": 12,
        },
    }
    p = tmp_path / "macd_trend.yaml"
    p.write_text(yaml.dump(cfg))
    return p


def _make_enriched_df(
    rows: int = 5,
    close: float = 450.0,
    ema_21: float = 440.0,
    macd_values: list[float] | None = None,
    macd_signal_values: list[float] | None = None,
    macd_hist_values: list[float] | None = None,
    volume: float = 2_000_000,
    volume_sma: float = 1_000_000,
    atr: float = 5.0,
) -> pd.DataFrame:
    """Build a DataFrame mimicking compute_indicators() output.

    Defaults: histogram crosses from negative to positive on last bar,
    MACD above signal, close above EMA_21, volume above average.
    """
    if macd_values is None:
        macd_values = [0.5, 0.6, 0.4, 0.3, 0.8]
    if macd_signal_values is None:
        macd_signal_values = [0.4, 0.5, 0.5, 0.5, 0.5]
    if macd_hist_values is None:
        # Prev bar: -0.2 (negative), current: 0.3 (positive) => crossover
        macd_hist_values = [0.1, 0.1, -0.1, -0.2, 0.3]

    for lst in (macd_values, macd_signal_values, macd_hist_values):
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
            "ema_21": [ema_21] * rows,
            "macd": macd_values[:rows],
            "macd_signal": macd_signal_values[:rows],
            "macd_hist": macd_hist_values[:rows],
            "bb_lower_20_2": [close - 10] * rows,
            "bb_mid_20_2": [close] * rows,
            "bb_upper_20_2": [close + 10] * rows,
        },
        index=pd.DatetimeIndex(dates, name="timestamp"),
    )
    return df


class TestMacdTrendBuy:
    def test_all_entry_conditions_met(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = MacdTrend(strategy_config, indicator_settings)
        df = _make_enriched_df()
        signals = strat.evaluate({"SPY": df})

        assert len(signals) == 1
        sig = signals[0]
        assert sig.direction == SignalDirection.BUY
        assert sig.symbol == "SPY"
        assert sig.strategy_name == "macd_trend"
        assert 0.0 <= sig.strength <= 1.0
        assert sig.stop_loss_pct > 0
        assert "BUY" in sig.reason

    def test_stop_loss_pct_correct(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = MacdTrend(strategy_config, indicator_settings)
        df = _make_enriched_df(close=450.0, atr=5.0)
        signals = strat.evaluate({"SPY": df})

        expected_stop = 1.5 * 5.0 / 450.0
        assert abs(signals[0].stop_loss_pct - expected_stop) < 1e-6

    def test_strength_computation(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = MacdTrend(strategy_config, indicator_settings)
        # hist=0.3, atr=5.0 => strength = 0.3/5.0 = 0.06
        df = _make_enriched_df(atr=5.0, macd_hist_values=[0.1, -0.1, -0.2, -0.1, 0.3])
        signals = strat.evaluate({"SPY": df})
        assert abs(signals[0].strength - 0.06) < 1e-6

    def test_no_histogram_crossover_no_buy(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = MacdTrend(strategy_config, indicator_settings)
        # Both bars positive => no crossover
        df = _make_enriched_df(macd_hist_values=[0.1, 0.2, 0.1, 0.2, 0.3])
        signals = strat.evaluate({"SPY": df})
        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        assert len(buy_signals) == 0

    def test_price_below_ema_no_buy(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = MacdTrend(strategy_config, indicator_settings)
        df = _make_enriched_df(close=430.0, ema_21=440.0)
        signals = strat.evaluate({"SPY": df})
        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        assert len(buy_signals) == 0

    def test_macd_below_signal_no_buy(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = MacdTrend(strategy_config, indicator_settings)
        # MACD line below signal line
        df = _make_enriched_df(
            macd_values=[0.3, 0.2, 0.1, 0.2, 0.3],
            macd_signal_values=[0.5, 0.5, 0.5, 0.5, 0.5],
            macd_hist_values=[0.1, -0.1, -0.2, -0.1, 0.3],
        )
        signals = strat.evaluate({"SPY": df})
        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        assert len(buy_signals) == 0

    def test_volume_below_threshold_no_buy(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = MacdTrend(strategy_config, indicator_settings)
        df = _make_enriched_df(volume=500_000, volume_sma=1_000_000)
        signals = strat.evaluate({"SPY": df})
        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        assert len(buy_signals) == 0


class TestMacdTrendSell:
    def test_histogram_negative_crossover_sell(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = MacdTrend(strategy_config, indicator_settings)
        # Histogram crosses from positive to negative
        df = _make_enriched_df(macd_hist_values=[0.3, 0.2, 0.1, 0.05, -0.1])
        signals = strat.evaluate({"SPY": df})

        assert len(signals) == 1
        assert signals[0].direction == SignalDirection.SELL
        assert "crossed negative" in signals[0].reason

    def test_macd_crosses_below_signal_sell(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = MacdTrend(strategy_config, indicator_settings)
        # MACD line crosses below signal (prev: MACD >= signal, now: MACD < signal)
        # Histogram stays positive to avoid triggering hist crossover first
        df = _make_enriched_df(
            macd_values=[0.6, 0.5, 0.4, 0.5, 0.3],
            macd_signal_values=[0.4, 0.4, 0.4, 0.4, 0.4],
            macd_hist_values=[0.2, 0.1, 0.0, 0.1, 0.1],
        )
        signals = strat.evaluate({"SPY": df})

        assert len(signals) == 1
        assert signals[0].direction == SignalDirection.SELL
        assert "crossed below" in signals[0].reason


class TestMacdTrendErrors:
    def test_missing_indicator_column(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = MacdTrend(strategy_config, indicator_settings)
        df = _make_enriched_df()
        df = df.drop(columns=["macd_hist"])
        with pytest.raises(StrategyError, match="Missing indicator columns"):
            strat.evaluate({"SPY": df})

    def test_empty_dataframe(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = MacdTrend(strategy_config, indicator_settings)
        df = pd.DataFrame(
            columns=["close", "volume", "ema_21", "atr_14", "volume_sma_20",
                      "macd", "macd_signal", "macd_hist"]
        )
        with pytest.raises(StrategyError, match="Empty DataFrame"):
            strat.evaluate({"SPY": df})

    def test_missing_symbol_skipped(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = MacdTrend(strategy_config, indicator_settings)
        signals = strat.evaluate({})
        assert signals == []

    def test_invalid_ema_period_raises(
        self, tmp_path: Path, indicator_settings: IndicatorSettings
    ) -> None:
        cfg = {
            "universe": ["SPY"],
            "entry": {
                "ema_trend_filter": 50,  # not in default EMA periods [9, 21]
                "volume_sma_period": 20,
                "volume_mult": 1.0,
            },
            "exit": {"atr_stop_mult": 1.5, "time_stop_days": 12},
        }
        p = tmp_path / "bad.yaml"
        p.write_text(yaml.dump(cfg))
        with pytest.raises(StrategyError, match="EMA period 50"):
            MacdTrend(p, indicator_settings)


class TestMacdTrendProperties:
    def test_name(self, strategy_config: Path, indicator_settings: IndicatorSettings) -> None:
        strat = MacdTrend(strategy_config, indicator_settings)
        assert strat.name == "macd_trend"

    def test_universe(self, strategy_config: Path, indicator_settings: IndicatorSettings) -> None:
        strat = MacdTrend(strategy_config, indicator_settings)
        assert strat.universe == ["SPY"]

    def test_min_history_days(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = MacdTrend(strategy_config, indicator_settings)
        # max(ema=21, atr=14, vol_sma=20, macd_warmup=26+9=35)
        assert strat.min_history_days == 35

    def test_time_stop_days(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = MacdTrend(strategy_config, indicator_settings)
        assert strat.time_stop_days == 12
