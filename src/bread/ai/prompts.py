"""Prompt templates for Claude AI integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bread.ai.models import EventAlert, TradeContext
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


def build_single_review_prompt(
    signal: Signal,
    context: TradeContext,
    event_alerts: list[EventAlert] | None = None,
) -> str:
    """Build prompt for reviewing a single trading signal."""
    event_ctx = format_event_context(event_alerts) if event_alerts else ""
    return (
        f"Review this trading signal:\n{_format_signal(signal)}\n\n"
        f"{_format_context(context)}{event_ctx}"
    )


def build_batch_review_prompt(
    signals: list[Signal],
    context: TradeContext,
    event_alerts: list[EventAlert] | None = None,
) -> str:
    """Build prompt for reviewing multiple trading signals in one call.

    Signals are numbered so Claude returns reviews in the same order.
    """
    event_ctx = format_event_context(event_alerts) if event_alerts else ""
    parts = [f"{_format_context(context)}{event_ctx}\n"]
    parts.append(
        f"Review each of the following {len(signals)} trade signals. "
        "Return one review per signal in the exact same order.\n"
    )
    for i, sig in enumerate(signals, 1):
        parts.append(f"Signal {i}:\n{_format_signal(sig)}\n")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Research prompts (Phase 3)
# ---------------------------------------------------------------------------

RESEARCH_SYSTEM_PROMPT: str = (
    "You are a financial news research analyst for an automated swing trading bot. "
    "Search the web for market-moving events affecting the given symbols. "
    "Focus on: earnings announcements, FDA decisions, analyst upgrades/downgrades, "
    "significant sector news, macro events (Fed decisions, economic data), "
    "and any breaking news that could cause significant price moves. "
    "Only report events from the last 48 hours or upcoming within 7 days. "
    "Be precise about severity — 'high' means likely >2% move, 'medium' means "
    "possible 1-2% move, 'low' means minor news, 'none' means nothing notable found."
)


def build_research_prompt(
    symbols: list[str],
    held_symbols: list[str],
) -> str:
    """Build prompt for event research scan."""
    from datetime import date

    held_str = ", ".join(held_symbols) if held_symbols else "none"
    symbols_str = ", ".join(symbols)
    return (
        f"Today is {date.today().isoformat()}.\n"
        f"Search for market-moving events affecting these symbols: {symbols_str}\n\n"
        f"Currently held positions: {held_str}\n\n"
        "For each symbol, search for:\n"
        "1. Earnings announcements (recent or upcoming)\n"
        "2. FDA or regulatory decisions\n"
        "3. Analyst upgrades, downgrades, or price target changes\n"
        "4. Significant sector/industry news\n"
        "5. Macro events (Fed, economic data) affecting these sectors\n"
        "6. Any breaking news or material events\n\n"
        "For symbols where you find nothing notable, report severity 'none' "
        "with a brief note.\n"
        "Prioritize held positions — flag any risks to current holdings."
    )


def format_event_context(event_alerts: list[EventAlert] | None) -> str:
    """Format active event alerts as context for signal review prompts.

    Returns an empty string if there are no alerts, so callers can
    unconditionally append the result.
    """
    if not event_alerts:
        return ""
    lines = ["\nRecent market events (from automated research):"]
    for alert in event_alerts:
        lines.append(f"- [{alert.severity.upper()}] {alert.symbol}: {alert.headline}")
    return "\n".join(lines) + "\n"
