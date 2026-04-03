"""Unit tests for Sector Rotation strategy."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml

from bread.core.config import IndicatorSettings
from bread.core.exceptions import StrategyError
from bread.core.models import SignalDirection
from bread.strategy.sector_rotation import SectorRotation


@pytest.fixture()
def indicator_settings() -> IndicatorSettings:
    return IndicatorSettings(return_periods=[5, 10, 20])


@pytest.fixture()
def strategy_config(tmp_path: Path) -> Path:
    cfg = {
        "universe": ["SPY", "QQQ", "XLK"],
        "entry": {
            "top_n": 2,
            "sma_trend": 50,
            "return_weights": [
                {"period": 20, "weight": 0.5},
                {"period": 10, "weight": 0.3},
                {"period": 5, "weight": 0.2},
            ],
        },
        "exit": {
            "exit_rank": 3,
            "atr_stop_mult": 2.0,
            "time_stop_days": 20,
        },
    }
    p = tmp_path / "sector_rotation.yaml"
    p.write_text(yaml.dump(cfg))
    return p


def _make_enriched_df(
    rows: int = 5,
    close: float = 450.0,
    sma_50: float = 430.0,
    atr: float = 5.0,
    return_5d: float = 0.02,
    return_10d: float = 0.04,
    return_20d: float = 0.06,
) -> pd.DataFrame:
    """Build a DataFrame with return indicators."""
    dates = pd.bdate_range(start="2024-06-01", periods=rows, tz="UTC")

    df = pd.DataFrame(
        {
            "open": [close - 1.0] * rows,
            "high": [close + 2.0] * rows,
            "low": [close - 2.0] * rows,
            "close": [close] * rows,
            "volume": [2_000_000] * rows,
            "sma_200": [400.0] * rows,
            "sma_20": [440.0] * rows,
            "sma_50": [sma_50] * rows,
            "rsi_14": [50.0] * rows,
            "atr_14": [atr] * rows,
            "volume_sma_20": [1_000_000] * rows,
            "ema_9": [close] * rows,
            "ema_21": [close] * rows,
            "macd": [0.0] * rows,
            "macd_signal": [0.0] * rows,
            "macd_hist": [0.0] * rows,
            "bb_lower_20_2": [close - 10] * rows,
            "bb_mid_20_2": [close] * rows,
            "bb_upper_20_2": [close + 10] * rows,
            "return_5d": [return_5d] * rows,
            "return_10d": [return_10d] * rows,
            "return_20d": [return_20d] * rows,
        },
        index=pd.DatetimeIndex(dates, name="timestamp"),
    )
    return df


class TestSectorRotationBuy:
    def test_top_ranked_symbols_get_buy(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = SectorRotation(strategy_config, indicator_settings)
        # SPY: highest score, QQQ: mid, XLK: lowest but still positive
        universe = {
            "SPY": _make_enriched_df(return_5d=0.03, return_10d=0.05, return_20d=0.08),
            "QQQ": _make_enriched_df(return_5d=0.02, return_10d=0.03, return_20d=0.05),
            "XLK": _make_enriched_df(return_5d=0.01, return_10d=0.02, return_20d=0.03),
        }
        signals = strat.evaluate(universe)

        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        assert len(buy_signals) == 2  # top_n=2
        # SPY should be first (highest score)
        assert buy_signals[0].symbol == "SPY"
        assert buy_signals[1].symbol == "QQQ"

    def test_positive_score_required(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = SectorRotation(strategy_config, indicator_settings)
        # All negative returns => no buys
        universe = {
            "SPY": _make_enriched_df(return_5d=-0.02, return_10d=-0.03, return_20d=-0.05),
            "QQQ": _make_enriched_df(return_5d=-0.01, return_10d=-0.02, return_20d=-0.04),
            "XLK": _make_enriched_df(return_5d=-0.03, return_10d=-0.04, return_20d=-0.06),
        }
        signals = strat.evaluate(universe)
        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        assert len(buy_signals) == 0

    def test_above_sma_required(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = SectorRotation(strategy_config, indicator_settings)
        # Price below SMA => no buy even with positive score
        universe = {
            "SPY": _make_enriched_df(close=420.0, sma_50=430.0),
            "QQQ": _make_enriched_df(close=420.0, sma_50=430.0),
            "XLK": _make_enriched_df(close=420.0, sma_50=430.0),
        }
        signals = strat.evaluate(universe)
        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        assert len(buy_signals) == 0


class TestSectorRotationSell:
    def test_low_ranked_symbols_get_sell(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = SectorRotation(strategy_config, indicator_settings)
        # XLK has negative score => gets SELL
        universe = {
            "SPY": _make_enriched_df(return_5d=0.03, return_10d=0.05, return_20d=0.08),
            "QQQ": _make_enriched_df(return_5d=0.02, return_10d=0.03, return_20d=0.05),
            "XLK": _make_enriched_df(return_5d=-0.01, return_10d=-0.02, return_20d=-0.03),
        }
        signals = strat.evaluate(universe)
        sell_signals = [s for s in signals if s.direction == SignalDirection.SELL]
        assert any(s.symbol == "XLK" for s in sell_signals)

    def test_below_sma_gets_sell(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = SectorRotation(strategy_config, indicator_settings)
        # SPY below SMA with positive score => still gets SELL
        universe = {
            "SPY": _make_enriched_df(close=420.0, sma_50=430.0,
                                      return_5d=0.03, return_10d=0.05, return_20d=0.08),
            "QQQ": _make_enriched_df(return_5d=0.02, return_10d=0.03, return_20d=0.05),
            "XLK": _make_enriched_df(return_5d=0.01, return_10d=0.02, return_20d=0.03),
        }
        signals = strat.evaluate(universe)
        sell_signals = [s for s in signals if s.direction == SignalDirection.SELL]
        assert any(s.symbol == "SPY" for s in sell_signals)


class TestSectorRotationErrors:
    def test_missing_indicator_column(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = SectorRotation(strategy_config, indicator_settings)
        df = _make_enriched_df()
        df = df.drop(columns=["return_20d"])
        with pytest.raises(StrategyError, match="Missing indicator columns"):
            strat.evaluate({"SPY": df})

    def test_empty_dataframe(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = SectorRotation(strategy_config, indicator_settings)
        df = pd.DataFrame(
            columns=["close", "sma_50", "atr_14", "return_5d", "return_10d", "return_20d"]
        )
        with pytest.raises(StrategyError, match="Empty DataFrame"):
            strat.evaluate({"SPY": df})

    def test_missing_symbol_skipped(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = SectorRotation(strategy_config, indicator_settings)
        signals = strat.evaluate({})
        assert signals == []

    def test_invalid_return_period_raises(
        self, tmp_path: Path, indicator_settings: IndicatorSettings
    ) -> None:
        cfg = {
            "universe": ["SPY"],
            "entry": {
                "top_n": 2,
                "sma_trend": 50,
                "return_weights": [{"period": 30, "weight": 1.0}],
            },
            "exit": {"exit_rank": 3, "atr_stop_mult": 2.0, "time_stop_days": 20},
        }
        p = tmp_path / "bad.yaml"
        p.write_text(yaml.dump(cfg))
        with pytest.raises(StrategyError, match="Return period 30"):
            SectorRotation(p, indicator_settings)


class TestSectorRotationProperties:
    def test_name(self, strategy_config: Path, indicator_settings: IndicatorSettings) -> None:
        strat = SectorRotation(strategy_config, indicator_settings)
        assert strat.name == "sector_rotation"

    def test_universe(self, strategy_config: Path, indicator_settings: IndicatorSettings) -> None:
        strat = SectorRotation(strategy_config, indicator_settings)
        assert strat.universe == ["SPY", "QQQ", "XLK"]

    def test_min_history_days(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = SectorRotation(strategy_config, indicator_settings)
        # max(sma_trend=50, atr=14, max_return_period=20)
        assert strat.min_history_days == 50

    def test_time_stop_days(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = SectorRotation(strategy_config, indicator_settings)
        assert strat.time_stop_days == 20
