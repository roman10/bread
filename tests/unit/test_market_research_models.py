"""Tests for EventAlert and MarketResearch dataclasses."""

from __future__ import annotations

import pytest

from bread.ai.models import EventAlert, MarketResearch


class TestEventAlert:
    def test_from_dict_valid(self) -> None:
        data = {
            "symbol": "SPY",
            "severity": "high",
            "headline": "Fed raises rates",
            "details": "Federal Reserve raised rates by 25bps.",
            "event_type": "macro",
            "source": "https://example.com",
        }
        alert = EventAlert.from_dict(data)
        assert alert.symbol == "SPY"
        assert alert.severity == "high"
        assert alert.headline == "Fed raises rates"
        assert alert.event_type == "macro"
        assert alert.source == "https://example.com"

    def test_from_dict_coerces_types(self) -> None:
        data = {"symbol": 123, "severity": "low", "headline": 456}
        alert = EventAlert.from_dict(data)
        assert alert.symbol == "123"
        assert alert.headline == "456"

    def test_from_dict_invalid_severity_defaults_to_none(self) -> None:
        alert = EventAlert.from_dict({"severity": "critical"})
        assert alert.severity == "none"

    def test_from_dict_invalid_event_type_defaults_to_other(self) -> None:
        alert = EventAlert.from_dict({"event_type": "unknown_type"})
        assert alert.event_type == "other"

    def test_from_dict_missing_fields(self) -> None:
        alert = EventAlert.from_dict({})
        assert alert.symbol == ""
        assert alert.severity == "none"
        assert alert.headline == ""
        assert alert.details == ""
        assert alert.event_type == "other"
        assert alert.source == ""

    def test_constructor_validates_severity(self) -> None:
        with pytest.raises(ValueError, match="severity"):
            EventAlert(
                symbol="SPY",
                severity="critical",
                headline="x",
                details="x",
                event_type="other",
                source="",
            )

    def test_frozen(self) -> None:
        alert = EventAlert.from_dict({"severity": "high"})
        with pytest.raises(AttributeError):
            alert.symbol = "QQQ"  # type: ignore[misc]


class TestMarketResearch:
    def test_from_dict_valid(self) -> None:
        data = {
            "events": [
                {
                    "symbol": "SPY",
                    "severity": "high",
                    "headline": "Rate hike",
                    "details": "Details here",
                    "event_type": "macro",
                    "source": "reuters.com",
                },
                {
                    "symbol": "QQQ",
                    "severity": "none",
                    "headline": "Nothing notable",
                    "details": "",
                    "event_type": "other",
                    "source": "",
                },
            ],
            "scan_summary": "1 notable event found",
        }
        research = MarketResearch.from_dict(data)
        assert len(research.events) == 2
        assert research.events[0].symbol == "SPY"
        assert research.events[1].severity == "none"
        assert research.scan_summary == "1 notable event found"

    def test_from_dict_skips_malformed_events(self) -> None:
        data = {
            "events": [
                {"symbol": "SPY", "severity": "high", "headline": "Good"},
                "not a dict",
                42,
            ],
            "scan_summary": "partial",
        }
        research = MarketResearch.from_dict(data)
        assert len(research.events) == 1
        assert research.events[0].symbol == "SPY"

    def test_from_dict_empty_events(self) -> None:
        research = MarketResearch.from_dict({"events": [], "scan_summary": "nothing"})
        assert research.events == []

    def test_from_dict_missing_events_key(self) -> None:
        research = MarketResearch.from_dict({"scan_summary": "no events key"})
        assert research.events == []

    def test_from_dict_non_list_events(self) -> None:
        research = MarketResearch.from_dict({"events": "not a list"})
        assert research.events == []

    def test_json_schema_structure(self) -> None:
        schema = MarketResearch.json_schema()
        assert schema["type"] == "object"
        assert "events" in schema["properties"]
        assert "scan_summary" in schema["properties"]
        assert set(schema["required"]) == {"events", "scan_summary"}
        items = schema["properties"]["events"]["items"]
        assert "symbol" in items["properties"]
        assert items["properties"]["severity"]["enum"] == [
            "high", "medium", "low", "none"
        ]
