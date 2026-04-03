"""Unit tests for EMA Crossover strategy."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml

from bread.core.config import IndicatorSettings
from bread.core.exceptions import StrategyError
from bread.core.models import SignalDirection
from bread.strategy.ema_crossover import EmaCrossover


@pytest.fixture()
def indicator_settings() -> IndicatorSettings:
    return IndicatorSettings()


@pytest.fixture()
def strategy_config(tmp_path: Path) -> Path:
    cfg = {
        "universe": ["SPY"],
        "entry": {
            "ema_fast": 9,
            "ema_slow": 21,
            "sma_trend": 200,
            "max_atr_pct": 0.03,
            "rsi_period": 14,
            "rsi_min": 40,
            "rsi_max": 65,
        },
        "exit": {
            "rsi_exit": 75,
            "atr_stop_mult": 1.5,
            "time_stop_days": 8,
        },
    }
    p = tmp_path / "ema_crossover.yaml"
    p.write_text(yaml.dump(cfg))
    return p


def _make_enriched_df(
    rows: int = 5,
    close: float = 450.0,
    ema_9_values: list[float] | None = None,
    ema_21_values: list[float] | None = None,
    sma_200: float = 400.0,
    rsi_values: list[float] | None = None,
    atr: float = 5.0,
) -> pd.DataFrame:
    """Build a DataFrame mimicking compute_indicators() output.

    Defaults: EMA_9 crosses above EMA_21 on last bar, close > SMA_200,
    ATR/close < 0.03, RSI between 40-65.
    """
    if ema_9_values is None:
        # Prev bar: 439 <= 440, current: 441 > 440 => crossover
        ema_9_values = [438.0, 437.0, 438.0, 439.0, 441.0]
    if ema_21_values is None:
        ema_21_values = [440.0, 440.0, 440.0, 440.0, 440.0]
    if rsi_values is None:
        rsi_values = [50.0, 52.0, 48.0, 51.0, 55.0]

    for lst in (ema_9_values, ema_21_values, rsi_values):
        if len(lst) < rows:
            lst[:] = [lst[0]] * (rows - len(lst)) + lst

    dates = pd.bdate_range(start="2024-06-01", periods=rows, tz="UTC")

    df = pd.DataFrame(
        {
            "open": [close - 1.0] * rows,
            "high": [close + 2.0] * rows,
            "low": [close - 2.0] * rows,
            "close": [close] * rows,
            "volume": [2_000_000] * rows,
            "sma_200": [sma_200] * rows,
            "sma_20": [440.0] * rows,
            "sma_50": [430.0] * rows,
            "rsi_14": rsi_values[:rows],
            "atr_14": [atr] * rows,
            "volume_sma_20": [1_000_000] * rows,
            "ema_9": ema_9_values[:rows],
            "ema_21": ema_21_values[:rows],
            "macd": [0.0] * rows,
            "macd_signal": [0.0] * rows,
            "macd_hist": [0.0] * rows,
            "bb_lower_20_2": [close - 10] * rows,
            "bb_mid_20_2": [close] * rows,
            "bb_upper_20_2": [close + 10] * rows,
        },
        index=pd.DatetimeIndex(dates, name="timestamp"),
    )
    return df


class TestEmaCrossoverBuy:
    def test_all_entry_conditions_met(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = EmaCrossover(strategy_config, indicator_settings)
        df = _make_enriched_df()
        signals = strat.evaluate({"SPY": df})

        assert len(signals) == 1
        sig = signals[0]
        assert sig.direction == SignalDirection.BUY
        assert sig.symbol == "SPY"
        assert sig.strategy_name == "ema_crossover"
        assert 0.0 <= sig.strength <= 1.0
        assert sig.stop_loss_pct > 0
        assert "BUY" in sig.reason

    def test_stop_loss_pct_correct(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = EmaCrossover(strategy_config, indicator_settings)
        df = _make_enriched_df(close=450.0, atr=5.0)
        signals = strat.evaluate({"SPY": df})

        expected_stop = 1.5 * 5.0 / 450.0
        assert abs(signals[0].stop_loss_pct - expected_stop) < 1e-6

    def test_strength_computation(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = EmaCrossover(strategy_config, indicator_settings)
        # ema_9=441, ema_21=440, atr=5 => spread = (441-440)/5 = 0.2
        df = _make_enriched_df(atr=5.0)
        signals = strat.evaluate({"SPY": df})
        assert abs(signals[0].strength - 0.2) < 1e-6

    def test_no_crossover_no_buy(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = EmaCrossover(strategy_config, indicator_settings)
        # EMA_9 already above EMA_21 on both bars (no crossover)
        df = _make_enriched_df(
            ema_9_values=[442.0, 442.0, 442.0, 442.0, 443.0],
            ema_21_values=[440.0, 440.0, 440.0, 440.0, 440.0],
        )
        signals = strat.evaluate({"SPY": df})
        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        assert len(buy_signals) == 0

    def test_price_below_sma200_no_buy(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = EmaCrossover(strategy_config, indicator_settings)
        df = _make_enriched_df(close=390.0, sma_200=400.0)
        signals = strat.evaluate({"SPY": df})
        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        assert len(buy_signals) == 0

    def test_high_volatility_no_buy(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = EmaCrossover(strategy_config, indicator_settings)
        # ATR/close = 15/450 = 0.033 >= 0.03 threshold
        df = _make_enriched_df(close=450.0, atr=15.0)
        signals = strat.evaluate({"SPY": df})
        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        assert len(buy_signals) == 0

    def test_rsi_too_low_no_buy(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = EmaCrossover(strategy_config, indicator_settings)
        df = _make_enriched_df(rsi_values=[30.0, 32.0, 28.0, 35.0, 35.0])
        signals = strat.evaluate({"SPY": df})
        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        assert len(buy_signals) == 0

    def test_rsi_too_high_no_buy(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = EmaCrossover(strategy_config, indicator_settings)
        df = _make_enriched_df(rsi_values=[60.0, 62.0, 64.0, 63.0, 68.0])
        signals = strat.evaluate({"SPY": df})
        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        assert len(buy_signals) == 0


class TestEmaCrossoverSell:
    def test_ema_bearish_crossover_sell(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = EmaCrossover(strategy_config, indicator_settings)
        # EMA_9 crosses below EMA_21
        df = _make_enriched_df(
            ema_9_values=[442.0, 441.0, 440.5, 440.0, 439.0],
            ema_21_values=[440.0, 440.0, 440.0, 440.0, 440.0],
        )
        signals = strat.evaluate({"SPY": df})

        assert len(signals) == 1
        assert signals[0].direction == SignalDirection.SELL
        assert "crossed below" in signals[0].reason

    def test_rsi_overbought_sell(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = EmaCrossover(strategy_config, indicator_settings)
        # RSI > 75, no bearish crossover
        df = _make_enriched_df(
            ema_9_values=[442.0, 442.0, 442.0, 442.0, 443.0],
            ema_21_values=[440.0, 440.0, 440.0, 440.0, 440.0],
            rsi_values=[70.0, 72.0, 73.0, 74.0, 78.0],
        )
        signals = strat.evaluate({"SPY": df})

        assert len(signals) == 1
        assert signals[0].direction == SignalDirection.SELL
        assert "take-profit" in signals[0].reason


class TestEmaCrossoverErrors:
    def test_missing_indicator_column(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = EmaCrossover(strategy_config, indicator_settings)
        df = _make_enriched_df()
        df = df.drop(columns=["ema_9"])
        with pytest.raises(StrategyError, match="Missing indicator columns"):
            strat.evaluate({"SPY": df})

    def test_empty_dataframe(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = EmaCrossover(strategy_config, indicator_settings)
        df = pd.DataFrame(
            columns=["close", "ema_9", "ema_21", "sma_200", "rsi_14", "atr_14"]
        )
        with pytest.raises(StrategyError, match="Empty DataFrame"):
            strat.evaluate({"SPY": df})

    def test_missing_symbol_skipped(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = EmaCrossover(strategy_config, indicator_settings)
        signals = strat.evaluate({})
        assert signals == []

    def test_invalid_ema_period_raises(
        self, tmp_path: Path, indicator_settings: IndicatorSettings
    ) -> None:
        cfg = {
            "universe": ["SPY"],
            "entry": {
                "ema_fast": 15,  # not in default EMA periods [9, 21]
                "ema_slow": 21,
                "sma_trend": 200,
                "rsi_period": 14,
            },
            "exit": {"rsi_exit": 75, "atr_stop_mult": 1.5, "time_stop_days": 8},
        }
        p = tmp_path / "bad.yaml"
        p.write_text(yaml.dump(cfg))
        with pytest.raises(StrategyError, match="EMA period 15"):
            EmaCrossover(p, indicator_settings)


class TestEmaCrossoverProperties:
    def test_name(self, strategy_config: Path, indicator_settings: IndicatorSettings) -> None:
        strat = EmaCrossover(strategy_config, indicator_settings)
        assert strat.name == "ema_crossover"

    def test_universe(self, strategy_config: Path, indicator_settings: IndicatorSettings) -> None:
        strat = EmaCrossover(strategy_config, indicator_settings)
        assert strat.universe == ["SPY"]

    def test_min_history_days(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = EmaCrossover(strategy_config, indicator_settings)
        assert strat.min_history_days == 200  # max(200, 21, 14, 14)

    def test_time_stop_days(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = EmaCrossover(strategy_config, indicator_settings)
        assert strat.time_stop_days == 8
