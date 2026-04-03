"""Unit tests for ETF Momentum strategy."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml

from bread.core.config import IndicatorSettings
from bread.core.exceptions import StrategyError
from bread.core.models import SignalDirection
from bread.strategy.etf_momentum import EtfMomentum


@pytest.fixture()
def indicator_settings() -> IndicatorSettings:
    return IndicatorSettings()


@pytest.fixture()
def strategy_config(tmp_path: Path) -> Path:
    cfg = {
        "universe": ["SPY"],
        "entry": {
            "sma_long": 200,
            "rsi_period": 14,
            "rsi_oversold": 30,
            "sma_fast": 20,
            "sma_mid": 50,
            "volume_sma_period": 20,
            "volume_mult": 1.0,
        },
        "exit": {
            "rsi_overbought": 70,
            "atr_stop_mult": 1.5,
            "time_stop_days": 15,
        },
    }
    p = tmp_path / "etf_momentum.yaml"
    p.write_text(yaml.dump(cfg))
    return p


def _make_enriched_df(
    rows: int = 5,
    close: float = 450.0,
    sma_200: float = 400.0,
    sma_20: float = 420.0,
    sma_50: float = 410.0,
    rsi_values: list[float] | None = None,
    volume: float = 2_000_000,
    volume_sma: float = 1_000_000,
    atr: float = 5.0,
    low: float | None = None,
) -> pd.DataFrame:
    """Build a DataFrame mimicking compute_indicators() output."""
    if rsi_values is None:
        # Default: RSI bounce scenario — prev bars below 30, current above
        rsi_values = [25.0, 28.0, 26.0, 29.0, 35.0]
    if len(rsi_values) < rows:
        rsi_values = [rsi_values[0]] * (rows - len(rsi_values)) + rsi_values

    dates = pd.bdate_range(start="2024-06-01", periods=rows, tz="UTC")
    if low is None:
        low = close - 2.0

    df = pd.DataFrame(
        {
            "open": [close - 1.0] * rows,
            "high": [close + 2.0] * rows,
            "low": [low] * rows,
            "close": [close] * rows,
            "volume": [volume] * rows,
            "sma_200": [sma_200] * rows,
            "sma_20": [sma_20] * rows,
            "sma_50": [sma_50] * rows,
            "rsi_14": rsi_values[:rows],
            "atr_14": [atr] * rows,
            "volume_sma_20": [volume_sma] * rows,
            # Extra indicator columns (not used by strategy but present in real data)
            "ema_9": [close] * rows,
            "ema_21": [close] * rows,
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


class TestEtfMomentumBuy:
    def test_all_entry_conditions_met(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = EtfMomentum(strategy_config, indicator_settings)
        df = _make_enriched_df()
        signals = strat.evaluate({"SPY": df})

        assert len(signals) == 1
        sig = signals[0]
        assert sig.direction == SignalDirection.BUY
        assert sig.symbol == "SPY"
        assert sig.strategy_name == "etf_momentum"
        assert 0.0 <= sig.strength <= 1.0
        assert sig.stop_loss_pct > 0
        assert "BUY" in sig.reason

    def test_stop_loss_pct_correct(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = EtfMomentum(strategy_config, indicator_settings)
        df = _make_enriched_df(close=450.0, atr=5.0)
        signals = strat.evaluate({"SPY": df})

        expected_stop = 1.5 * 5.0 / 450.0
        assert abs(signals[0].stop_loss_pct - expected_stop) < 1e-6

    def test_strength_computation(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        # volume = 2M, volume_sma = 1M => ratio = 2.0 => strength = 1.0 (clamped)
        strat = EtfMomentum(strategy_config, indicator_settings)
        df = _make_enriched_df(volume=2_000_000, volume_sma=1_000_000)
        signals = strat.evaluate({"SPY": df})
        assert signals[0].strength == 1.0

        # volume = 1.5M, volume_sma = 1M => ratio = 1.5 => strength = 0.5
        df2 = _make_enriched_df(volume=1_500_000, volume_sma=1_000_000)
        signals2 = strat.evaluate({"SPY": df2})
        assert abs(signals2[0].strength - 0.5) < 1e-6

    def test_rsi_never_below_30_no_buy(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = EtfMomentum(strategy_config, indicator_settings)
        # All RSI values above 30 but below 70 — no bounce
        df = _make_enriched_df(rsi_values=[35.0, 40.0, 38.0, 42.0, 35.0])
        signals = strat.evaluate({"SPY": df})
        # Should get SELL (sma_20 > sma_50, so no trend reversal; rsi < 70)
        # Actually no sell either — all conditions for sell not met
        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        assert len(buy_signals) == 0

    def test_price_below_sma200_no_buy(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = EtfMomentum(strategy_config, indicator_settings)
        df = _make_enriched_df(close=390.0, sma_200=400.0)
        signals = strat.evaluate({"SPY": df})
        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        assert len(buy_signals) == 0

    def test_sma20_below_sma50_no_buy(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = EtfMomentum(strategy_config, indicator_settings)
        df = _make_enriched_df(sma_20=405.0, sma_50=410.0)
        signals = strat.evaluate({"SPY": df})
        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        assert len(buy_signals) == 0

    def test_volume_below_threshold_no_buy(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = EtfMomentum(strategy_config, indicator_settings)
        df = _make_enriched_df(volume=500_000, volume_sma=1_000_000)
        signals = strat.evaluate({"SPY": df})
        buy_signals = [s for s in signals if s.direction == SignalDirection.BUY]
        assert len(buy_signals) == 0


class TestEtfMomentumSell:
    def test_rsi_overbought_sell(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = EtfMomentum(strategy_config, indicator_settings)
        df = _make_enriched_df(rsi_values=[65.0, 68.0, 72.0, 73.0, 75.0])
        signals = strat.evaluate({"SPY": df})

        assert len(signals) == 1
        assert signals[0].direction == SignalDirection.SELL
        assert "overbought" in signals[0].reason

    def test_trend_reversal_sell(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = EtfMomentum(strategy_config, indicator_settings)
        # SMA fast < SMA mid => trend reversal, RSI not overbought
        df = _make_enriched_df(
            sma_20=405.0, sma_50=410.0,
            rsi_values=[40.0, 45.0, 42.0, 44.0, 50.0],
        )
        signals = strat.evaluate({"SPY": df})

        assert len(signals) == 1
        assert signals[0].direction == SignalDirection.SELL
        assert "trend reversal" in signals[0].reason


class TestEtfMomentumErrors:
    def test_missing_indicator_column(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = EtfMomentum(strategy_config, indicator_settings)
        df = _make_enriched_df()
        df = df.drop(columns=["sma_200"])
        with pytest.raises(StrategyError, match="Missing indicator columns"):
            strat.evaluate({"SPY": df})

    def test_empty_dataframe(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = EtfMomentum(strategy_config, indicator_settings)
        df = pd.DataFrame(columns=["close", "volume", "sma_200", "sma_20", "sma_50",
                                    "rsi_14", "atr_14", "volume_sma_20"])
        with pytest.raises(StrategyError, match="Empty DataFrame"):
            strat.evaluate({"SPY": df})

    def test_missing_symbol_skipped(
        self, strategy_config: Path, indicator_settings: IndicatorSettings
    ) -> None:
        strat = EtfMomentum(strategy_config, indicator_settings)
        # Pass empty universe — strategy's universe has SPY but it's not in data
        signals = strat.evaluate({})
        assert signals == []

    def test_invalid_sma_period_raises(
        self, tmp_path: Path, indicator_settings: IndicatorSettings
    ) -> None:
        cfg = {
            "universe": ["SPY"],
            "entry": {
                "sma_long": 300,  # not in default indicator settings
                "rsi_period": 14,
                "rsi_oversold": 30,
                "sma_fast": 20,
                "sma_mid": 50,
                "volume_sma_period": 20,
                "volume_mult": 1.0,
            },
            "exit": {"rsi_overbought": 70, "atr_stop_mult": 1.5, "time_stop_days": 15},
        }
        p = tmp_path / "bad.yaml"
        p.write_text(yaml.dump(cfg))
        with pytest.raises(StrategyError, match="SMA period 300"):
            EtfMomentum(p, indicator_settings)
