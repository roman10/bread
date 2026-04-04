"""Tests for Claude AI prompt templates."""

from __future__ import annotations

from datetime import UTC, datetime

from bread.ai.models import TradeContext
from bread.ai.prompts import build_batch_review_prompt, build_single_review_prompt
from bread.core.models import Signal, SignalDirection


def _make_signal(symbol: str = "SPY") -> Signal:
    return Signal(
        symbol=symbol,
        direction=SignalDirection.BUY,
        strength=0.7,
        stop_loss_pct=0.05,
        strategy_name="etf_momentum",
        reason="RSI oversold",
        timestamp=datetime.now(UTC),
    )


def _make_context() -> TradeContext:
    return TradeContext(
        equity=10000.0,
        buying_power=8000.0,
        open_positions=["QQQ"],
        daily_pnl=50.0,
        weekly_pnl=200.0,
        peak_equity=10500.0,
    )


class TestSingleReviewPrompt:
    def test_contains_signal_fields(self) -> None:
        prompt = build_single_review_prompt(_make_signal(), _make_context())
        assert "SPY" in prompt
        assert "BUY" in prompt
        assert "0.70" in prompt  # strength
        assert "5.0%" in prompt  # stop_loss_pct
        assert "etf_momentum" in prompt
        assert "RSI oversold" in prompt

    def test_contains_context_fields(self) -> None:
        prompt = build_single_review_prompt(_make_signal(), _make_context())
        assert "$10,000.00" in prompt  # equity
        assert "$8,000.00" in prompt  # buying_power
        assert "QQQ" in prompt  # open_positions
        assert "$50.00" in prompt  # daily_pnl
        assert "$10,500.00" in prompt  # peak_equity

    def test_empty_positions_shows_none(self) -> None:
        ctx = TradeContext(
            equity=10000.0,
            buying_power=8000.0,
            open_positions=[],
            daily_pnl=0.0,
            weekly_pnl=0.0,
            peak_equity=10000.0,
        )
        prompt = build_single_review_prompt(_make_signal(), ctx)
        assert "none" in prompt


class TestBatchReviewPrompt:
    def test_numbers_signals(self) -> None:
        signals = [_make_signal("SPY"), _make_signal("QQQ"), _make_signal("IWM")]
        prompt = build_batch_review_prompt(signals, _make_context())
        assert "Signal 1:" in prompt
        assert "Signal 2:" in prompt
        assert "Signal 3:" in prompt
        assert "SPY" in prompt
        assert "QQQ" in prompt
        assert "IWM" in prompt

    def test_shared_context_once(self) -> None:
        signals = [_make_signal("SPY"), _make_signal("QQQ")]
        prompt = build_batch_review_prompt(signals, _make_context())
        # Context should appear once, not per signal
        assert prompt.count("Buying Power:") == 1
        assert prompt.count("Portfolio context:") == 1

    def test_ordering_instruction(self) -> None:
        signals = [_make_signal("SPY"), _make_signal("QQQ")]
        prompt = build_batch_review_prompt(signals, _make_context())
        assert "same order" in prompt.lower()

    def test_signal_count_mentioned(self) -> None:
        signals = [_make_signal("SPY"), _make_signal("QQQ")]
        prompt = build_batch_review_prompt(signals, _make_context())
        assert "2 trade signals" in prompt
