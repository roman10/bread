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
    def batch_json_schema(cls) -> dict[str, object]:
        """JSON Schema for batch review (wraps array in object for CLI compatibility)."""
        return {
            "type": "object",
            "properties": {
                "reviews": {
                    "type": "array",
                    "items": cls.json_schema(),
                },
            },
            "required": ["reviews"],
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


_VALID_SEVERITIES = frozenset({"high", "medium", "low", "none"})
_VALID_EVENT_TYPES = frozenset(
    {"earnings", "fda", "analyst", "macro", "sector", "other"}
)


@dataclass(frozen=True)
class EventAlert:
    """A single market-moving event detected by a research scan."""

    symbol: str
    severity: str  # "high" | "medium" | "low" | "none"
    headline: str
    details: str
    event_type: str  # "earnings" | "fda" | "analyst" | "macro" | "sector" | "other"
    source: str

    def __post_init__(self) -> None:
        if self.severity not in _VALID_SEVERITIES:
            raise ValueError(
                f"severity must be one of {sorted(_VALID_SEVERITIES)}, got {self.severity!r}"
            )

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> EventAlert:
        """Construct from parsed JSON with defensive type coercion."""
        severity = str(data.get("severity", "none"))
        if severity not in _VALID_SEVERITIES:
            severity = "none"
        event_type = str(data.get("event_type", "other"))
        if event_type not in _VALID_EVENT_TYPES:
            event_type = "other"
        return cls(
            symbol=str(data.get("symbol", "")),
            severity=severity,
            headline=str(data.get("headline", "")),
            details=str(data.get("details", "")),
            event_type=event_type,
            source=str(data.get("source", "")),
        )


@dataclass(frozen=True)
class MarketResearch:
    """Claude's structured research response for event monitoring."""

    events: list[EventAlert]
    scan_summary: str

    @classmethod
    def json_schema(cls) -> dict[str, object]:
        """JSON Schema for the --json-schema CLI flag."""
        return {
            "type": "object",
            "properties": {
                "events": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "symbol": {"type": "string"},
                            "severity": {
                                "type": "string",
                                "enum": ["high", "medium", "low", "none"],
                            },
                            "headline": {"type": "string"},
                            "details": {"type": "string"},
                            "event_type": {
                                "type": "string",
                                "enum": [
                                    "earnings",
                                    "fda",
                                    "analyst",
                                    "macro",
                                    "sector",
                                    "other",
                                ],
                            },
                            "source": {"type": "string"},
                        },
                        "required": [
                            "symbol",
                            "severity",
                            "headline",
                            "details",
                            "event_type",
                            "source",
                        ],
                    },
                },
                "scan_summary": {"type": "string"},
            },
            "required": ["events", "scan_summary"],
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> MarketResearch:
        """Construct from parsed JSON, skipping malformed events."""
        raw_events = data.get("events", [])
        events: list[EventAlert] = []
        if isinstance(raw_events, list):
            for item in raw_events:
                if isinstance(item, dict):
                    try:
                        events.append(EventAlert.from_dict(item))
                    except (ValueError, KeyError):
                        pass
        return cls(
            events=events,
            scan_summary=str(data.get("scan_summary", "")),
        )
