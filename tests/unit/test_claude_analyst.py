"""Tests for Claude analyst strategy."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from bread.ai.models import StrategyAnalysis, StrategyRecommendation
from bread.core.config import IndicatorSettings
from bread.core.exceptions import ClaudeTimeoutError
from bread.core.models import SignalDirection
from bread.strategy.claude_analyst import ClaudeAnalyst


@pytest.fixture()
def indicator_settings() -> IndicatorSettings:
    return IndicatorSettings()


@pytest.fixture()
def config_path(tmp_path: Path) -> Path:
    cfg = tmp_path / "claude_analyst.yaml"
    cfg.write_text(
        "universe:\n"
        "  - SPY\n"
        "  - QQQ\n"
        "  - IWM\n"
        "analysis:\n"
        "  atr_stop_mult: 1.5\n"
        "  time_stop_days: 15\n"
    )
    return cfg


def _make_df(
    close: float = 500.0,
    volume: float = 80_000_000.0,
    atr: float = 5.0,
    rsi: float = 55.0,
) -> pd.DataFrame:
    """Create a minimal DataFrame with all required indicator columns."""
    idx = pd.DatetimeIndex(
        [datetime(2024, 1, i + 1, tzinfo=UTC) for i in range(5)],
        name="timestamp",
    )
    data: dict[str, list[float]] = {
        "open": [close - 1] * 5,
        "high": [close + 2] * 5,
        "low": [close - 2] * 5,
        "close": [close * 0.98, close * 0.99, close * 1.0, close * 1.01, close],
        "volume": [volume] * 5,
        "sma_20": [close * 0.99] * 5,
        "sma_50": [close * 0.97] * 5,
        "sma_200": [close * 0.90] * 5,
        "ema_9": [close * 1.0] * 5,
        "ema_21": [close * 0.99] * 5,
        "rsi_14": [rsi] * 5,
        "atr_14": [atr] * 5,
        "macd": [2.5] * 5,
        "macd_signal": [1.8] * 5,
        "macd_hist": [0.7] * 5,
        "bb_lower_20_2": [close * 0.96] * 5,
        "bb_mid_20_2": [close * 0.99] * 5,
        "bb_upper_20_2": [close * 1.02] * 5,
        "volume_sma_20": [volume * 0.9] * 5,
        "return_5d": [0.02] * 5,
        "return_10d": [0.01] * 5,
        "return_20d": [-0.01] * 5,
    }
    return pd.DataFrame(data, index=idx)


def _make_analysis(
    recs: list[StrategyRecommendation] | None = None,
) -> StrategyAnalysis:
    if recs is None:
        recs = [
            StrategyRecommendation("SPY", "BUY", 0.8, "Strong momentum"),
            StrategyRecommendation("QQQ", "SELL", 1.0, "Trend reversal"),
            StrategyRecommendation("IWM", "HOLD", 0.0, "No setup"),
        ]
    return StrategyAnalysis(recommendations=recs, market_assessment="Bullish")


# ------------------------------------------------------------------
# Construction and properties
# ------------------------------------------------------------------


class TestConstruction:
    def test_properties(self, config_path: Path, indicator_settings: IndicatorSettings) -> None:
        s = ClaudeAnalyst(config_path, indicator_settings)
        assert s.name == "claude_analyst"
        assert s.universe == ["SPY", "QQQ", "IWM"]
        assert s.min_history_days == 200
        assert s.time_stop_days == 15

    def test_universe_override(
        self, config_path: Path, indicator_settings: IndicatorSettings
    ) -> None:
        s = ClaudeAnalyst(config_path, indicator_settings, universe=["DIA"])
        assert s.universe == ["DIA"]

    def test_default_config(self, config_path: Path, indicator_settings: IndicatorSettings) -> None:
        """Config defaults when analysis section is missing."""
        minimal = config_path.parent / "minimal.yaml"
        minimal.write_text("universe:\n  - SPY\n")
        s = ClaudeAnalyst(minimal, indicator_settings)
        assert s.time_stop_days == 15  # default


# ------------------------------------------------------------------
# Summary generation
# ------------------------------------------------------------------


class TestSummarizeSymbol:
    def test_contains_key_values(
        self, config_path: Path, indicator_settings: IndicatorSettings
    ) -> None:
        s = ClaudeAnalyst(config_path, indicator_settings)
        df = _make_df(close=500.0, rsi=62.3, atr=5.23, volume=85_000_000.0)
        summary = s._summarize_symbol("SPY", df)

        assert "SPY" in summary
        assert "$500.00" in summary
        assert "62.3" in summary  # RSI
        assert "$5.23" in summary  # ATR
        assert "85.0M" in summary  # Volume
        assert "SMA:" in summary
        assert "EMA:" in summary
        assert "MACD:" in summary

    def test_trend_note_above_all(
        self, config_path: Path, indicator_settings: IndicatorSettings
    ) -> None:
        s = ClaudeAnalyst(config_path, indicator_settings)
        df = _make_df(close=500.0)  # close > all SMAs
        summary = s._summarize_symbol("SPY", df)
        assert "above all SMAs" in summary

    def test_trend_note_mixed(
        self, config_path: Path, indicator_settings: IndicatorSettings
    ) -> None:
        s = ClaudeAnalyst(config_path, indicator_settings)
        df = _make_df(close=500.0)
        # Set SMA200 above close
        df["sma_200"] = 600.0
        summary = s._summarize_symbol("SPY", df)
        assert "mixed" in summary


# ------------------------------------------------------------------
# evaluate() tests
# ------------------------------------------------------------------


class TestEvaluate:
    def test_no_claude_client(
        self, config_path: Path, indicator_settings: IndicatorSettings
    ) -> None:
        s = ClaudeAnalyst(config_path, indicator_settings)
        result = s.evaluate({"SPY": _make_df()})
        assert result == []

    def test_happy_path(self, config_path: Path, indicator_settings: IndicatorSettings) -> None:
        claude = MagicMock()
        claude.analyze_technicals.return_value = _make_analysis()

        s = ClaudeAnalyst(config_path, indicator_settings, claude_client=claude)
        universe = {
            "SPY": _make_df(close=500.0, atr=5.0),
            "QQQ": _make_df(close=400.0, atr=4.0),
            "IWM": _make_df(close=200.0, atr=3.0),
        }
        signals = s.evaluate(universe)

        # BUY for SPY, SELL for QQQ, HOLD for IWM (skipped)
        assert len(signals) == 2
        spy_sig = next(s for s in signals if s.symbol == "SPY")
        qqq_sig = next(s for s in signals if s.symbol == "QQQ")

        assert spy_sig.direction == SignalDirection.BUY
        assert spy_sig.strength == 0.8
        assert spy_sig.strategy_name == "claude_analyst"
        assert spy_sig.reason == "Strong momentum"
        assert spy_sig.stop_loss_pct == pytest.approx(1.5 * 5.0 / 500.0)

        assert qqq_sig.direction == SignalDirection.SELL
        assert qqq_sig.strength == 1.0  # SELL always 1.0
        assert qqq_sig.reason == "Trend reversal"

    def test_claude_error_returns_empty(
        self, config_path: Path, indicator_settings: IndicatorSettings
    ) -> None:
        claude = MagicMock()
        claude.analyze_technicals.side_effect = ClaudeTimeoutError("timeout")

        s = ClaudeAnalyst(config_path, indicator_settings, claude_client=claude)
        signals = s.evaluate({"SPY": _make_df()})
        assert signals == []

    def test_all_hold_returns_empty(
        self, config_path: Path, indicator_settings: IndicatorSettings
    ) -> None:
        claude = MagicMock()
        claude.analyze_technicals.return_value = _make_analysis(
            [StrategyRecommendation("SPY", "HOLD", 0.0, "No setup")]
        )

        s = ClaudeAnalyst(config_path, indicator_settings, claude_client=claude)
        signals = s.evaluate({"SPY": _make_df()})
        assert signals == []

    def test_missing_symbol_in_universe_skipped(
        self, config_path: Path, indicator_settings: IndicatorSettings
    ) -> None:
        """Recommendation for a symbol not in DataFrames is skipped."""
        claude = MagicMock()
        claude.analyze_technicals.return_value = _make_analysis(
            [StrategyRecommendation("XYZ", "BUY", 0.9, "Breakout")]
        )

        s = ClaudeAnalyst(config_path, indicator_settings, claude_client=claude)
        signals = s.evaluate({"SPY": _make_df()})
        assert signals == []

    def test_stop_loss_calculation(
        self, config_path: Path, indicator_settings: IndicatorSettings
    ) -> None:
        claude = MagicMock()
        claude.analyze_technicals.return_value = _make_analysis(
            [StrategyRecommendation("SPY", "BUY", 0.5, "Setup")]
        )

        s = ClaudeAnalyst(config_path, indicator_settings, claude_client=claude)
        signals = s.evaluate({"SPY": _make_df(close=200.0, atr=4.0)})

        assert len(signals) == 1
        # stop_loss_pct = 1.5 * 4.0 / 200.0 = 0.03
        assert signals[0].stop_loss_pct == pytest.approx(0.03)

    def test_empty_universe_data(
        self, config_path: Path, indicator_settings: IndicatorSettings
    ) -> None:
        claude = MagicMock()
        s = ClaudeAnalyst(config_path, indicator_settings, claude_client=claude)
        signals = s.evaluate({})  # no data at all
        assert signals == []
        claude.analyze_technicals.assert_not_called()

    def test_sell_strength_forced_to_one(
        self, config_path: Path, indicator_settings: IndicatorSettings
    ) -> None:
        """SELL signals always get strength=1.0 regardless of Claude's value."""
        claude = MagicMock()
        claude.analyze_technicals.return_value = _make_analysis(
            [StrategyRecommendation("SPY", "SELL", 0.3, "Deteriorating")]
        )

        s = ClaudeAnalyst(config_path, indicator_settings, claude_client=claude)
        signals = s.evaluate({"SPY": _make_df()})

        assert len(signals) == 1
        assert signals[0].strength == 1.0

    def test_prompt_includes_date_and_symbols(
        self, config_path: Path, indicator_settings: IndicatorSettings
    ) -> None:
        claude = MagicMock()
        claude.analyze_technicals.return_value = _make_analysis([])

        s = ClaudeAnalyst(config_path, indicator_settings, claude_client=claude)
        s.evaluate({"SPY": _make_df(), "QQQ": _make_df()})

        prompt = claude.analyze_technicals.call_args[0][0]
        assert "Date:" in prompt
        assert "SPY" in prompt
        assert "QQQ" in prompt
        assert "BUY" in prompt
        assert "SELL" in prompt
        assert "HOLD" in prompt
