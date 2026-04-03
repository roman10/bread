"""Unit tests for Risk-Off Rotation strategy."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml

from bread.core.config import IndicatorSettings
from bread.core.exceptions import StrategyError
from bread.core.models import SignalDirection
from bread.strategy.risk_off_rotation import RiskOffRotation


@pytest.fixture()
def indicator_settings() -> IndicatorSettings:
    return IndicatorSettings(return_periods=[5, 10, 20])


@pytest.fixture()
def strategy_config(tmp_path: Path) -> Path:
    cfg = {
        "universe": ["SPY", "QQQ", "TLT", "GLD"],
        "entry": {
            "regime_symbol": "SPY",
            "safe_haven_symbol": "TLT",
            "regime_return_period": 20,
            "momentum_return_period": 10,
            "sma_trend": 50,
            "risk_on_symbols": ["SPY", "QQQ"],
            "risk_off_symbols": ["TLT", "GLD"],
        },
        "exit": {
            "atr_stop_mult": 2.0,
            "time_stop_days": 20,
        },
    }
    p = tmp_path / "risk_off_rotation.yaml"
    p.write_text(yaml.dump(cfg))
    return p


def _make_enriched_df(
    rows: int = 5,
    close: float = 450.0,
    sma_50: float = 430.0,
    atr: float = 5.0,
    return_10d: float = 0.03,
    return_20d: float = 0.05,
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
            "return_5d": [0.01] * rows,
            "return_10d": [return_10d] * rows,
            "return_20d": [return_20d] * rows,
        },
        index=pd.DatetimeIndex(dates, name="timestamp"),
    )
    return df


class TestRiskOnRegime:
    def test_risk_on_buys_equity(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = RiskOffRotation(strategy_config, indicator_settings)
        # SPY return_20d=0.05 > TLT return_20d=0.01, SPY > SMA => risk-on
        universe = {
            "SPY": _make_enriched_df(return_20d=0.05, return_10d=0.04),
            "QQQ": _make_enriched_df(return_20d=0.04, return_10d=0.06),
            "TLT": _make_enriched_df(return_20d=0.01, return_10d=0.005),
            "GLD": _make_enriched_df(return_20d=0.02, return_10d=0.01),
        }
        signals = strat.evaluate(universe)

        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        sell_signals = [s for s in signals if s.direction == SignalDirection.SELL]

        assert len(buy_signals) == 1
        # QQQ has higher 10d momentum
        assert buy_signals[0].symbol == "QQQ"
        assert "risk_on" in buy_signals[0].reason

        # TLT and GLD should get SELL signals
        sell_symbols = {s.symbol for s in sell_signals}
        assert "TLT" in sell_symbols
        assert "GLD" in sell_symbols

    def test_risk_on_no_buy_if_below_sma(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = RiskOffRotation(strategy_config, indicator_settings)
        # Risk-on regime but all equity symbols below SMA
        universe = {
            "SPY": _make_enriched_df(close=450.0, sma_50=430.0, return_20d=0.05),
            "QQQ": _make_enriched_df(close=420.0, sma_50=430.0, return_10d=0.06),
            "TLT": _make_enriched_df(return_20d=0.01),
            "GLD": _make_enriched_df(return_20d=0.02),
        }
        signals = strat.evaluate(universe)
        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        # SPY is above SMA but QQQ is not; SPY gets the buy
        assert len(buy_signals) == 1
        assert buy_signals[0].symbol == "SPY"


class TestRiskOffRegime:
    def test_risk_off_buys_safe_haven(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = RiskOffRotation(strategy_config, indicator_settings)
        # SPY return_20d=-0.05 < TLT return_20d=0.03 => risk-off
        universe = {
            "SPY": _make_enriched_df(close=420.0, sma_50=430.0, return_20d=-0.05, return_10d=-0.03),
            "QQQ": _make_enriched_df(return_20d=-0.04, return_10d=-0.02),
            "TLT": _make_enriched_df(return_20d=0.03, return_10d=0.02),
            "GLD": _make_enriched_df(return_20d=0.02, return_10d=0.03),
        }
        signals = strat.evaluate(universe)

        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        sell_signals = [s for s in signals if s.direction == SignalDirection.SELL]

        assert len(buy_signals) == 1
        # GLD has higher 10d momentum
        assert buy_signals[0].symbol == "GLD"
        assert "risk_off" in buy_signals[0].reason

        # SPY and QQQ should get SELL signals
        sell_symbols = {s.symbol for s in sell_signals}
        assert "SPY" in sell_symbols
        assert "QQQ" in sell_symbols


class TestRiskOffRotationErrors:
    def test_missing_regime_symbol(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = RiskOffRotation(strategy_config, indicator_settings)
        # Missing SPY (regime symbol) => no signals
        universe = {
            "QQQ": _make_enriched_df(),
            "TLT": _make_enriched_df(),
        }
        signals = strat.evaluate(universe)
        assert signals == []

    def test_missing_indicator_column(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = RiskOffRotation(strategy_config, indicator_settings)
        df = _make_enriched_df()
        df = df.drop(columns=["return_20d"])
        with pytest.raises(StrategyError, match="Missing indicator columns"):
            strat.evaluate({"SPY": df, "TLT": _make_enriched_df()})

    def test_empty_universe(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = RiskOffRotation(strategy_config, indicator_settings)
        signals = strat.evaluate({})
        assert signals == []

    def test_invalid_return_period_raises(
        self, tmp_path: Path, indicator_settings: IndicatorSettings
    ) -> None:
        cfg = {
            "universe": ["SPY", "TLT"],
            "entry": {
                "regime_symbol": "SPY",
                "safe_haven_symbol": "TLT",
                "regime_return_period": 30,  # not in return_periods
                "momentum_return_period": 10,
                "sma_trend": 50,
            },
            "exit": {"atr_stop_mult": 2.0, "time_stop_days": 20},
        }
        p = tmp_path / "bad.yaml"
        p.write_text(yaml.dump(cfg))
        with pytest.raises(StrategyError, match="Return period 30"):
            RiskOffRotation(p, indicator_settings)


class TestRiskOffRotationProperties:
    def test_name(self, strategy_config: Path, indicator_settings: IndicatorSettings) -> None:
        strat = RiskOffRotation(strategy_config, indicator_settings)
        assert strat.name == "risk_off_rotation"

    def test_universe(self, strategy_config: Path, indicator_settings: IndicatorSettings) -> None:
        strat = RiskOffRotation(strategy_config, indicator_settings)
        assert strat.universe == ["SPY", "QQQ", "TLT", "GLD"]

    def test_time_stop_days(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = RiskOffRotation(strategy_config, indicator_settings)
        assert strat.time_stop_days == 20
