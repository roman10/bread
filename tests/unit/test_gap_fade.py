"""Unit tests for Gap Fade strategy."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml

from bread.core.config import IndicatorSettings
from bread.core.exceptions import StrategyError
from bread.core.models import SignalDirection
from bread.strategy.gap_fade import GapFade


@pytest.fixture()
def indicator_settings() -> IndicatorSettings:
    return IndicatorSettings()


@pytest.fixture()
def strategy_config(tmp_path: Path) -> Path:
    cfg = {
        "universe": ["SPY"],
        "entry": {
            "gap_threshold": 0.015,
            "rsi_period": 14,
            "rsi_entry_max": 40,
            "sma_trend": 200,
        },
        "exit": {
            "rsi_exit": 60,
            "atr_stop_mult": 2.5,
            "time_stop_days": 7,
        },
    }
    p = tmp_path / "gap_fade.yaml"
    p.write_text(yaml.dump(cfg))
    return p


def _make_enriched_df(
    rows: int = 5,
    prev_close: float = 450.0,
    open_price: float = 443.0,
    close: float = 445.0,
    sma_200: float = 430.0,
    rsi_values: list[float] | None = None,
    atr: float = 5.0,
) -> pd.DataFrame:
    """Build a DataFrame for gap fade testing.

    Defaults: prev bar close=450, current open=443 (gap down ~1.6%),
    current close=445 (> open, buying pressure), above SMA200, RSI=35.
    """
    if rsi_values is None:
        rsi_values = [50.0, 48.0, 42.0, 38.0, 35.0]
    if len(rsi_values) < rows:
        rsi_values = [rsi_values[0]] * (rows - len(rsi_values)) + rsi_values

    dates = pd.bdate_range(start="2024-06-01", periods=rows, tz="UTC")

    # Previous bars have close at prev_close, last bar has the gap
    closes = [prev_close] * (rows - 1) + [close]
    opens = [prev_close - 1.0] * (rows - 1) + [open_price]

    df = pd.DataFrame(
        {
            "open": opens,
            "high": [max(o, c) + 2.0 for o, c in zip(opens, closes)],
            "low": [min(o, c) - 2.0 for o, c in zip(opens, closes)],
            "close": closes,
            "volume": [2_000_000] * rows,
            "sma_200": [sma_200] * rows,
            "sma_20": [445.0] * rows,
            "sma_50": [440.0] * rows,
            "rsi_14": rsi_values[:rows],
            "atr_14": [atr] * rows,
            "volume_sma_20": [1_000_000] * rows,
            "ema_9": closes,
            "ema_21": closes,
            "macd": [0.0] * rows,
            "macd_signal": [0.0] * rows,
            "macd_hist": [0.0] * rows,
            "bb_lower_20_2": [c - 10 for c in closes],
            "bb_mid_20_2": closes,
            "bb_upper_20_2": [c + 10 for c in closes],
        },
        index=pd.DatetimeIndex(dates, name="timestamp"),
    )
    return df


class TestGapFadeBuy:
    def test_all_entry_conditions_met(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = GapFade(strategy_config, indicator_settings)
        # Gap down 1.6%, close > open, RSI < 40, close > SMA200
        df = _make_enriched_df()
        signals = strat.evaluate({"SPY": df})

        assert len(signals) == 1
        sig = signals[0]
        assert sig.direction == SignalDirection.BUY
        assert sig.strategy_name == "gap_fade"
        assert "gap_down" in sig.reason
        assert 0.0 <= sig.strength <= 1.0
        assert sig.stop_loss_pct > 0

    def test_stop_loss_pct_correct(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = GapFade(strategy_config, indicator_settings)
        df = _make_enriched_df(close=445.0, atr=5.0)
        signals = strat.evaluate({"SPY": df})

        expected_stop = 2.5 * 5.0 / 445.0
        assert abs(signals[0].stop_loss_pct - expected_stop) < 1e-6

    def test_small_gap_no_buy(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = GapFade(strategy_config, indicator_settings)
        # Gap is only 0.4% < 1.5% threshold
        df = _make_enriched_df(prev_close=450.0, open_price=448.0, close=449.0)
        signals = strat.evaluate({"SPY": df})
        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        assert len(buy_signals) == 0

    def test_close_below_open_no_buy(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = GapFade(strategy_config, indicator_settings)
        # Close <= open means no buying pressure
        df = _make_enriched_df(open_price=443.0, close=442.0)
        signals = strat.evaluate({"SPY": df})
        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        assert len(buy_signals) == 0

    def test_rsi_too_high_no_buy(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = GapFade(strategy_config, indicator_settings)
        df = _make_enriched_df(rsi_values=[50.0, 52.0, 48.0, 45.0, 42.0])
        signals = strat.evaluate({"SPY": df})
        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        assert len(buy_signals) == 0

    def test_below_sma200_no_buy(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = GapFade(strategy_config, indicator_settings)
        df = _make_enriched_df(close=425.0, sma_200=430.0)
        signals = strat.evaluate({"SPY": df})
        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        assert len(buy_signals) == 0


class TestGapFadeSell:
    def test_rsi_exit_sell(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = GapFade(strategy_config, indicator_settings)
        # RSI > 60, close not near prev_close (no gap fill)
        df = _make_enriched_df(
            prev_close=450.0, close=445.0,
            rsi_values=[55.0, 58.0, 60.0, 62.0, 65.0],
        )
        signals = strat.evaluate({"SPY": df})

        sell_signals = [s for s in signals if s.direction == SignalDirection.SELL]
        assert any("exit threshold" in s.reason for s in sell_signals)


class TestGapFadeErrors:
    def test_missing_indicator_column(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = GapFade(strategy_config, indicator_settings)
        df = _make_enriched_df()
        df = df.drop(columns=["sma_200"])
        with pytest.raises(StrategyError, match="Missing indicator columns"):
            strat.evaluate({"SPY": df})

    def test_empty_dataframe(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = GapFade(strategy_config, indicator_settings)
        df = pd.DataFrame(
            columns=["open", "close", "rsi_14", "atr_14", "sma_200"]
        )
        with pytest.raises(StrategyError, match="Empty DataFrame"):
            strat.evaluate({"SPY": df})

    def test_missing_symbol_skipped(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = GapFade(strategy_config, indicator_settings)
        signals = strat.evaluate({})
        assert signals == []

    def test_invalid_sma_period_raises(
        self, tmp_path: Path, indicator_settings: IndicatorSettings
    ) -> None:
        cfg = {
            "universe": ["SPY"],
            "entry": {
                "sma_trend": 100,  # not in default SMA periods
                "rsi_period": 14,
            },
            "exit": {"rsi_exit": 60, "atr_stop_mult": 2.5, "time_stop_days": 7},
        }
        p = tmp_path / "bad.yaml"
        p.write_text(yaml.dump(cfg))
        with pytest.raises(StrategyError, match="SMA period 100"):
            GapFade(p, indicator_settings)


class TestGapFadeProperties:
    def test_name(self, strategy_config: Path, indicator_settings: IndicatorSettings) -> None:
        strat = GapFade(strategy_config, indicator_settings)
        assert strat.name == "gap_fade"

    def test_time_stop_days(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = GapFade(strategy_config, indicator_settings)
        assert strat.time_stop_days == 7

    def test_min_history_days(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = GapFade(strategy_config, indicator_settings)
        assert strat.min_history_days == 200
