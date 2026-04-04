"""Tests for Claude AI prompt templates."""

from __future__ import annotations

from datetime import UTC, datetime

from bread.ai.models import EventAlert, TradeContext
from bread.ai.prompts import (
    build_batch_review_prompt,
    build_research_prompt,
    build_single_review_prompt,
    format_event_context,
)
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


# ------------------------------------------------------------------
# Research prompts (Phase 3)
# ------------------------------------------------------------------


def _make_event(symbol: str = "SPY", severity: str = "high") -> EventAlert:
    return EventAlert(
        symbol=symbol,
        severity=severity,
        headline=f"{symbol} event",
        details="Details",
        event_type="macro",
        source="test.com",
    )


class TestResearchPrompt:
    def test_includes_all_symbols(self) -> None:
        prompt = build_research_prompt(["SPY", "QQQ", "IWM"], ["SPY"])
        assert "SPY" in prompt
        assert "QQQ" in prompt
        assert "IWM" in prompt

    def test_shows_held_positions(self) -> None:
        prompt = build_research_prompt(["SPY", "QQQ"], ["SPY"])
        assert "SPY" in prompt
        assert "held" in prompt.lower()

    def test_no_held_shows_none(self) -> None:
        prompt = build_research_prompt(["SPY"], [])
        assert "none" in prompt


class TestEventContext:
    def test_formats_alerts(self) -> None:
        alerts = [_make_event("SPY", "high"), _make_event("QQQ", "medium")]
        ctx = format_event_context(alerts)
        assert "[HIGH] SPY" in ctx
        assert "[MEDIUM] QQQ" in ctx
        assert "Recent market events" in ctx

    def test_empty_returns_empty_string(self) -> None:
        assert format_event_context(None) == ""
        assert format_event_context([]) == ""

    def test_single_review_with_events(self) -> None:
        alerts = [_make_event("SPY")]
        prompt = build_single_review_prompt(_make_signal(), _make_context(), event_alerts=alerts)
        assert "[HIGH] SPY" in prompt
        assert "Review this trading signal" in prompt

    def test_batch_review_with_events(self) -> None:
        alerts = [_make_event("SPY")]
        signals = [_make_signal("SPY"), _make_signal("QQQ")]
        prompt = build_batch_review_prompt(signals, _make_context(), event_alerts=alerts)
        assert "[HIGH] SPY" in prompt
        assert "Signal 1:" in prompt

    def test_review_without_events_unchanged(self) -> None:
        prompt_no_events = build_single_review_prompt(_make_signal(), _make_context())
        prompt_none = build_single_review_prompt(
            _make_signal(), _make_context(), event_alerts=None
        )
        assert prompt_no_events == prompt_none
        assert "Recent market events" not in prompt_no_events
