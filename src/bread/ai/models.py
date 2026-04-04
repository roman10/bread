"""Response dataclasses and JSON schemas for Claude AI integration."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CliResponse:
    """Parsed response from the Claude Code CLI JSON envelope."""

    result: dict[str, object] | str
    raw_output: str
    model: str
    duration_ms: int
    success: bool
    error: str | None = None
    session_id: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class TradeContext:
    """Portfolio state snapshot passed to Claude for signal review."""

    equity: float
    buying_power: float
    open_positions: list[str]
    daily_pnl: float
    weekly_pnl: float
    peak_equity: float


@dataclass(frozen=True)
class SignalReview:
    """Claude's structured review of a trading signal."""

    approved: bool
    confidence: float
    reasoning: str
    risk_flags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0.0, 1.0], got {self.confidence}")

    @classmethod
    def json_schema(cls) -> dict[str, object]:
        """JSON Schema for the --json-schema CLI flag."""
        return {
            "type": "object",
            "properties": {
                "approved": {"type": "boolean"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reasoning": {"type": "string"},
                "risk_flags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["approved", "confidence", "reasoning", "risk_flags"],
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> SignalReview:
        """Construct from a parsed JSON dict with defensive type coercion."""
        raw_flags = data.get("risk_flags", [])
        flags = [str(f) for f in raw_flags] if isinstance(raw_flags, list) else []
        return cls(
            approved=bool(data.get("approved", False)),
            confidence=float(str(data.get("confidence", 0.0))),
            reasoning=str(data.get("reasoning", "")),
            risk_flags=flags,
        )
