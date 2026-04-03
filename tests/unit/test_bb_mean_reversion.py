"""Unit tests for Bollinger Band Mean Reversion strategy."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml

from bread.core.config import IndicatorSettings
from bread.core.exceptions import StrategyError
from bread.core.models import SignalDirection
from bread.strategy.bb_mean_reversion import BbMeanReversion


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
            "rsi_period": 14,
            "rsi_entry_threshold": 40,
        },
        "exit": {
            "rsi_exit_threshold": 60,
            "atr_stop_mult": 2.0,
            "time_stop_days": 10,
        },
    }
    p = tmp_path / "bb_mean_reversion.yaml"
    p.write_text(yaml.dump(cfg))
    return p


def _make_enriched_df(
    rows: int = 5,
    close: float = 430.0,
    bb_lower: float = 440.0,
    bb_mid: float = 450.0,
    bb_upper: float = 460.0,
    rsi_values: list[float] | None = None,
    atr: float = 5.0,
) -> pd.DataFrame:
    """Build a DataFrame mimicking compute_indicators() output.

    Defaults: close=430 < bb_lower=440 and RSI=35 < 40 — entry conditions met.
    """
    if rsi_values is None:
        rsi_values = [38.0, 36.0, 37.0, 34.0, 35.0]
    if len(rsi_values) < rows:
        rsi_values = [rsi_values[0]] * (rows - len(rsi_values)) + rsi_values

    dates = pd.bdate_range(start="2024-06-01", periods=rows, tz="UTC")

    df = pd.DataFrame(
        {
            "open": [close - 1.0] * rows,
            "high": [close + 2.0] * rows,
            "low": [close - 2.0] * rows,
            "close": [close] * rows,
            "volume": [2_000_000] * rows,
            "sma_200": [450.0] * rows,
            "sma_20": [450.0] * rows,
            "sma_50": [448.0] * rows,
            "rsi_14": rsi_values[:rows],
            "atr_14": [atr] * rows,
            "volume_sma_20": [1_000_000] * rows,
            "ema_9": [close] * rows,
            "ema_21": [close] * rows,
            "macd": [0.0] * rows,
            "macd_signal": [0.0] * rows,
            "macd_hist": [0.0] * rows,
            "bb_lower_20_2": [bb_lower] * rows,
            "bb_mid_20_2": [bb_mid] * rows,
            "bb_upper_20_2": [bb_upper] * rows,
        },
        index=pd.DatetimeIndex(dates, name="timestamp"),
    )
    return df


class TestBbMeanReversionBuy:
    def test_all_entry_conditions_met(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = BbMeanReversion(strategy_config, indicator_settings)
        # close=430 < bb_lower=440, rsi=35 < 40
        df = _make_enriched_df()
        signals = strat.evaluate({"SPY": df})

        assert len(signals) == 1
        sig = signals[0]
        assert sig.direction == SignalDirection.BUY
        assert sig.symbol == "SPY"
        assert sig.strategy_name == "bb_mean_reversion"
        assert 0.0 <= sig.strength <= 1.0
        assert sig.stop_loss_pct > 0
        assert "BUY" in sig.reason

    def test_stop_loss_pct_correct(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = BbMeanReversion(strategy_config, indicator_settings)
        df = _make_enriched_df(close=430.0, atr=5.0)
        signals = strat.evaluate({"SPY": df})

        expected_stop = 2.0 * 5.0 / 430.0
        assert abs(signals[0].stop_loss_pct - expected_stop) < 1e-6

    def test_strength_computation(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = BbMeanReversion(strategy_config, indicator_settings)
        # close=430, bb_lower=440, bb_upper=460 => (440-430)/(460-440) = 0.5
        df = _make_enriched_df(close=430.0, bb_lower=440.0, bb_upper=460.0)
        signals = strat.evaluate({"SPY": df})
        assert abs(signals[0].strength - 0.5) < 1e-6

        # close=420, bb_lower=440, bb_upper=460 => (440-420)/(460-440) = 1.0 (clamped)
        df2 = _make_enriched_df(close=420.0, bb_lower=440.0, bb_upper=460.0)
        signals2 = strat.evaluate({"SPY": df2})
        assert signals2[0].strength == 1.0

    def test_close_above_bb_lower_no_buy(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = BbMeanReversion(strategy_config, indicator_settings)
        # close=445 >= bb_lower=440 => no entry (but also close < bb_mid, rsi < 60 => no exit)
        df = _make_enriched_df(close=445.0, bb_lower=440.0, bb_mid=450.0)
        signals = strat.evaluate({"SPY": df})
        assert len(signals) == 0

    def test_rsi_above_threshold_no_buy(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = BbMeanReversion(strategy_config, indicator_settings)
        # close < bb_lower but rsi=42 >= 40
        df = _make_enriched_df(rsi_values=[42.0, 41.0, 43.0, 42.0, 42.0])
        signals = strat.evaluate({"SPY": df})
        assert len(signals) == 0

    def test_zero_band_width_no_buy(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = BbMeanReversion(strategy_config, indicator_settings)
        # bb_upper == bb_lower => band_width = 0 => no signal
        df = _make_enriched_df(close=430.0, bb_lower=440.0, bb_mid=440.0, bb_upper=440.0)
        signals = strat.evaluate({"SPY": df})
        assert len(signals) == 0


class TestBbMeanReversionSell:
    def test_price_at_middle_band_sell(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = BbMeanReversion(strategy_config, indicator_settings)
        # close=450 >= bb_mid=450 => SELL
        df = _make_enriched_df(
            close=450.0,
            bb_lower=440.0,
            bb_mid=450.0,
            rsi_values=[45.0, 48.0, 50.0, 52.0, 55.0],
        )
        signals = strat.evaluate({"SPY": df})

        assert len(signals) == 1
        assert signals[0].direction == SignalDirection.SELL
        assert "mean reversion target" in signals[0].reason

    def test_rsi_exit_threshold_sell(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = BbMeanReversion(strategy_config, indicator_settings)
        # close < bb_mid but rsi=62 >= 60 => SELL
        df = _make_enriched_df(
            close=445.0,
            bb_lower=440.0,
            bb_mid=450.0,
            rsi_values=[55.0, 58.0, 59.0, 61.0, 62.0],
        )
        signals = strat.evaluate({"SPY": df})

        assert len(signals) == 1
        assert signals[0].direction == SignalDirection.SELL
        assert "exit threshold" in signals[0].reason

    def test_sell_priority_over_buy(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = BbMeanReversion(strategy_config, indicator_settings)
        # close >= bb_mid is checked first, so SELL takes priority
        df = _make_enriched_df(
            close=455.0,
            bb_lower=440.0,
            bb_mid=450.0,
            rsi_values=[35.0, 33.0, 34.0, 36.0, 35.0],
        )
        signals = strat.evaluate({"SPY": df})

        assert len(signals) == 1
        assert signals[0].direction == SignalDirection.SELL


class TestBbMeanReversionErrors:
    def test_missing_indicator_column(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = BbMeanReversion(strategy_config, indicator_settings)
        df = _make_enriched_df()
        df = df.drop(columns=["bb_lower_20_2"])
        with pytest.raises(StrategyError, match="Missing indicator columns"):
            strat.evaluate({"SPY": df})

    def test_empty_dataframe(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = BbMeanReversion(strategy_config, indicator_settings)
        df = pd.DataFrame(
            columns=["close", "bb_lower_20_2", "bb_mid_20_2", "bb_upper_20_2", "rsi_14", "atr_14"]
        )
        with pytest.raises(StrategyError, match="Empty DataFrame"):
            strat.evaluate({"SPY": df})

    def test_missing_symbol_skipped(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = BbMeanReversion(strategy_config, indicator_settings)
        signals = strat.evaluate({})
        assert signals == []

    def test_invalid_bollinger_period_raises(
        self, tmp_path: Path, indicator_settings: IndicatorSettings
    ) -> None:
        cfg = {
            "universe": ["SPY"],
            "entry": {
                "bollinger_period": 30,  # not matching indicator settings (20)
                "bollinger_stddev": 2.0,
                "rsi_period": 14,
                "rsi_entry_threshold": 40,
            },
            "exit": {"rsi_exit_threshold": 60, "atr_stop_mult": 2.0, "time_stop_days": 10},
        }
        p = tmp_path / "bad.yaml"
        p.write_text(yaml.dump(cfg))
        with pytest.raises(StrategyError, match="Bollinger period 30"):
            BbMeanReversion(p, indicator_settings)

    def test_invalid_bollinger_stddev_raises(
        self, tmp_path: Path, indicator_settings: IndicatorSettings
    ) -> None:
        cfg = {
            "universe": ["SPY"],
            "entry": {
                "bollinger_period": 20,
                "bollinger_stddev": 3.0,  # not matching indicator settings (2.0)
                "rsi_period": 14,
                "rsi_entry_threshold": 40,
            },
            "exit": {"rsi_exit_threshold": 60, "atr_stop_mult": 2.0, "time_stop_days": 10},
        }
        p = tmp_path / "bad.yaml"
        p.write_text(yaml.dump(cfg))
        with pytest.raises(StrategyError, match="Bollinger stddev 3.0"):
            BbMeanReversion(p, indicator_settings)


class TestBbMeanReversionProperties:
    def test_name(self, strategy_config: Path, indicator_settings: IndicatorSettings) -> None:
        strat = BbMeanReversion(strategy_config, indicator_settings)
        assert strat.name == "bb_mean_reversion"

    def test_universe(self, strategy_config: Path, indicator_settings: IndicatorSettings) -> None:
        strat = BbMeanReversion(strategy_config, indicator_settings)
        assert strat.universe == ["SPY"]

    def test_min_history_days(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = BbMeanReversion(strategy_config, indicator_settings)
        assert strat.min_history_days == 20  # max(bollinger=20, rsi=14, atr=14)

    def test_time_stop_days(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = BbMeanReversion(strategy_config, indicator_settings)
        assert strat.time_stop_days == 10
