"""Unit tests for MACD Divergence strategy."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml

from bread.core.config import IndicatorSettings
from bread.core.exceptions import StrategyError
from bread.core.models import SignalDirection
from bread.strategy.macd_divergence import MacdDivergence


@pytest.fixture()
def indicator_settings() -> IndicatorSettings:
    return IndicatorSettings()


@pytest.fixture()
def strategy_config(tmp_path: Path) -> Path:
    cfg = {
        "universe": ["SPY"],
        "entry": {
            "divergence_lookback": 20,
            "rsi_period": 14,
            "rsi_entry_max": 45,
            "swing_low_window": 2,  # smaller for testing
        },
        "exit": {
            "rsi_exit": 65,
            "atr_stop_mult": 2.0,
            "time_stop_days": 12,
        },
    }
    p = tmp_path / "macd_divergence.yaml"
    p.write_text(yaml.dump(cfg))
    return p


def _make_divergence_df(
    rsi: float = 40.0,
    atr: float = 5.0,
    bb_upper: float = 460.0,
) -> pd.DataFrame:
    """Build a DataFrame with a bullish divergence pattern.

    Creates 20 bars where:
    - Price makes two swing lows, with the second lower than the first
    - MACD histogram makes two lows, with the second higher than the first
    - Last bar has MACD histogram turning up
    """
    rows = 20
    dates = pd.bdate_range(start="2024-06-01", periods=rows, tz="UTC")

    # Price pattern: dip, recover, lower dip, start recovering
    # Swing low 1 around bar 6, swing low 2 around bar 14
    closes = [
        450, 448, 446, 445, 444, 442, 440,  # bars 0-6: first dip (low at 6)
        442, 445, 447, 448, 446, 444, 443,  # bars 7-13: recovery then second dip
        438, 439, 440, 441, 442, 443,        # bars 14-19: lower low at 14, recovering
    ]

    # MACD hist pattern: negative at first low, LESS negative at second low (higher)
    macd_hists = [
        0.1, 0.0, -0.1, -0.2, -0.3, -0.4, -0.5,  # bars 0-6: deep negative
        -0.3, -0.1, 0.0, 0.1, 0.0, -0.1, -0.2,    # bars 7-13
        -0.3, -0.25, -0.2, -0.15, -0.1, 0.05,      # bars 14-19: less negative (higher low)
    ]

    df = pd.DataFrame(
        {
            "open": [c - 1.0 for c in closes],
            "high": [c + 2.0 for c in closes],
            "low": [c - 2.0 for c in closes],
            "close": [float(c) for c in closes],
            "volume": [2_000_000] * rows,
            "sma_200": [400.0] * rows,
            "sma_20": [445.0] * rows,
            "sma_50": [440.0] * rows,
            "rsi_14": [rsi] * rows,
            "atr_14": [atr] * rows,
            "volume_sma_20": [1_000_000] * rows,
            "ema_9": [float(c) for c in closes],
            "ema_21": [float(c) for c in closes],
            "macd": [h + 0.1 for h in macd_hists],
            "macd_signal": [0.1] * rows,
            "macd_hist": [float(h) for h in macd_hists],
            "bb_lower_20_2": [430.0] * rows,
            "bb_mid_20_2": [445.0] * rows,
            "bb_upper_20_2": [bb_upper] * rows,
        },
        index=pd.DatetimeIndex(dates, name="timestamp"),
    )
    return df


class TestMacdDivergenceBuy:
    def test_bullish_divergence_buy(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = MacdDivergence(strategy_config, indicator_settings)
        df = _make_divergence_df()
        signals = strat.evaluate({"SPY": df})

        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        assert len(buy_signals) == 1
        assert buy_signals[0].strategy_name == "macd_divergence"
        assert "bullish divergence" in buy_signals[0].reason

    def test_rsi_too_high_no_buy(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = MacdDivergence(strategy_config, indicator_settings)
        df = _make_divergence_df(rsi=50.0)
        signals = strat.evaluate({"SPY": df})
        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        assert len(buy_signals) == 0


class TestMacdDivergenceSell:
    def test_rsi_exit_sell(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = MacdDivergence(strategy_config, indicator_settings)
        df = _make_divergence_df(rsi=70.0)
        signals = strat.evaluate({"SPY": df})

        assert len(signals) == 1
        assert signals[0].direction == SignalDirection.SELL
        assert "exit threshold" in signals[0].reason

    def test_above_bb_upper_sell(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = MacdDivergence(strategy_config, indicator_settings)
        # Close at 443 > bb_upper at 440
        df = _make_divergence_df(bb_upper=440.0)
        signals = strat.evaluate({"SPY": df})

        sell_signals = [s for s in signals if s.direction == SignalDirection.SELL]
        assert any("target reached" in s.reason for s in sell_signals)


class TestMacdDivergenceErrors:
    def test_missing_indicator_column(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = MacdDivergence(strategy_config, indicator_settings)
        df = _make_divergence_df()
        df = df.drop(columns=["macd_hist"])
        with pytest.raises(StrategyError, match="Missing indicator columns"):
            strat.evaluate({"SPY": df})

    def test_empty_dataframe(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = MacdDivergence(strategy_config, indicator_settings)
        df = pd.DataFrame(
            columns=["close", "rsi_14", "atr_14", "bb_upper_20_2", "macd_hist"]
        )
        with pytest.raises(StrategyError, match="Empty DataFrame"):
            strat.evaluate({"SPY": df})

    def test_missing_symbol_skipped(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = MacdDivergence(strategy_config, indicator_settings)
        signals = strat.evaluate({})
        assert signals == []


class TestMacdDivergenceProperties:
    def test_name(self, strategy_config: Path, indicator_settings: IndicatorSettings) -> None:
        strat = MacdDivergence(strategy_config, indicator_settings)
        assert strat.name == "macd_divergence"

    def test_time_stop_days(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = MacdDivergence(strategy_config, indicator_settings)
        assert strat.time_stop_days == 12
