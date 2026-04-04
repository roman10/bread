"""Prompt templates for Claude AI integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bread.ai.models import TradeContext
    from bread.core.models import Signal

REVIEW_SYSTEM_PROMPT: str = (
    "You are a risk-aware trading assistant for an automated swing trading bot. "
    "Review the proposed trade signal and provide your assessment. "
    "Be conservative \u2014 when in doubt, reject. Focus on risk/reward, "
    "current market conditions, and portfolio concentration."
)

BATCH_REVIEW_SYSTEM_PROMPT: str = (
    "You are a risk-aware trading assistant for an automated swing trading bot. "
    "Review each proposed trade signal and provide your assessment for every one. "
    "Be conservative \u2014 when in doubt, reject. Focus on risk/reward, "
    "current market conditions, and portfolio concentration. "
    "Consider the combined effect of all proposed trades on the portfolio."
)


def _format_context(context: TradeContext) -> str:
    """Format portfolio context block shared by single and batch prompts."""
    positions_str = ", ".join(context.open_positions) or "none"
    return (
        f"Portfolio context:\n"
        f"Equity: ${context.equity:,.2f}\n"
        f"Buying Power: ${context.buying_power:,.2f}\n"
        f"Open Positions: {positions_str}\n"
        f"Daily P&L: ${context.daily_pnl:,.2f}\n"
        f"Weekly P&L: ${context.weekly_pnl:,.2f}\n"
        f"Peak Equity: ${context.peak_equity:,.2f}\n"
    )


def _format_signal(signal: Signal) -> str:
    """Format a single signal block."""
    return (
        f"Symbol: {signal.symbol}\n"
        f"Direction: {signal.direction.value}\n"
        f"Strength: {signal.strength:.2f}\n"
        f"Stop Loss: {signal.stop_loss_pct:.1%}\n"
        f"Strategy: {signal.strategy_name}\n"
        f"Reason: {signal.reason}"
    )


def build_single_review_prompt(signal: Signal, context: TradeContext) -> str:
    """Build prompt for reviewing a single trading signal."""
    return f"Review this trading signal:\n{_format_signal(signal)}\n\n{_format_context(context)}"


def build_batch_review_prompt(signals: list[Signal], context: TradeContext) -> str:
    """Build prompt for reviewing multiple trading signals in one call.

    Signals are numbered so Claude returns reviews in the same order.
    """
    parts = [f"{_format_context(context)}\n"]
    parts.append(
        f"Review each of the following {len(signals)} trade signals. "
        "Return one review per signal in the exact same order.\n"
    )
    for i, sig in enumerate(signals, 1):
        parts.append(f"Signal {i}:\n{_format_signal(sig)}\n")
    return "\n".join(parts)
