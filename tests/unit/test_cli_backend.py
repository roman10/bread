"""Tests for Claude Code CLI backend."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from bread.ai.cli_backend import CliBackend, _extract_json_from_text
from bread.core.config import ClaudeSettings
from bread.core.exceptions import (
    ClaudeCliNotFoundError,
    ClaudeParseError,
    ClaudeTimeoutError,
)


def _config(**overrides: object) -> ClaudeSettings:
    defaults: dict[str, object] = {
        "enabled": True,
        "cli_path": "/usr/local/bin/claude",
        "default_model": "sonnet",
        "timeout_seconds": 30,
        "max_turns": 3,
    }
    defaults.update(overrides)
    return ClaudeSettings(**defaults)  # type: ignore[arg-type]


def _make_envelope(
    result: str = "test response",
    is_error: bool = False,
    duration_ms: int = 1000,
    structured_output: dict[str, object] | None = None,
    **extra: object,
) -> dict[str, object]:
    """Build a realistic CLI JSON envelope."""
    envelope: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "is_error": is_error,
        "duration_ms": duration_ms,
        "duration_api_ms": duration_ms - 10,
        "num_turns": 1,
        "result": result,
        "stop_reason": "end_turn",
        "session_id": "test-session-id",
        "total_cost_usd": 0.01,
        "usage": {
            "input_tokens": 100,
            "cache_read_input_tokens": 50,
            "output_tokens": 25,
        },
        "modelUsage": {
            "claude-sonnet-4-20250514": {
                "inputTokens": 100,
                "outputTokens": 25,
            }
        },
        "permission_denials": [],
        "terminal_reason": "completed",
    }
    if structured_output is not None:
        envelope["structured_output"] = structured_output
    envelope.update(extra)
    return envelope


def _mock_proc(
    envelope: dict[str, object],
    returncode: int = 0,
    stderr: str = "",
) -> MagicMock:
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.stdout = json.dumps(envelope)
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


class TestBuildArgs:
    @patch("bread.ai.cli_backend.subprocess.run")
    def test_basic_prompt_args(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_proc(_make_envelope())
        backend = CliBackend(_config())
        backend.query("test prompt")
        args = mock_run.call_args[0][0]
        assert args[:5] == ["/usr/local/bin/claude", "-p", "test prompt", "--output-format", "json"]
        assert "--model" in args
        assert "sonnet" in args

    @patch("bread.ai.cli_backend.subprocess.run")
    def test_json_schema_added(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_proc(_make_envelope(structured_output={"key": "val"}))
        schema = {"type": "object", "properties": {"key": {"type": "string"}}}
        backend = CliBackend(_config())
        backend.query("test", json_schema=schema)
        args = mock_run.call_args[0][0]
        idx = args.index("--json-schema")
        assert json.loads(args[idx + 1]) == schema

    @patch("bread.ai.cli_backend.subprocess.run")
    def test_system_prompt_added(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_proc(_make_envelope())
        backend = CliBackend(_config())
        backend.query("test", system_prompt="Be helpful")
        args = mock_run.call_args[0][0]
        idx = args.index("--append-system-prompt")
        assert args[idx + 1] == "Be helpful"

    @patch("bread.ai.cli_backend.subprocess.run")
    def test_allowed_tools_added(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_proc(_make_envelope())
        backend = CliBackend(_config())
        backend.query("test", allowed_tools=["WebSearch", "WebFetch"])
        args = mock_run.call_args[0][0]
        idx = args.index("--allowedTools")
        assert args[idx + 1] == "WebSearch,WebFetch"

    @patch("bread.ai.cli_backend.subprocess.run")
    def test_model_override(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_proc(_make_envelope())
        backend = CliBackend(_config(default_model="sonnet"))
        backend.query("test", model="opus")
        args = mock_run.call_args[0][0]
        idx = args.index("--model")
        assert args[idx + 1] == "opus"

    @patch("bread.ai.cli_backend.subprocess.run")
    def test_max_turns_override(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_proc(_make_envelope())
        backend = CliBackend(_config(max_turns=3))
        backend.query("test", max_turns=5)
        args = mock_run.call_args[0][0]
        idx = args.index("--max-turns")
        assert args[idx + 1] == "5"


class TestSuccessfulQuery:
    @patch("bread.ai.cli_backend.subprocess.run")
    def test_plain_text_response(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_proc(_make_envelope(result="The answer is 4"))
        backend = CliBackend(_config())
        resp = backend.query("What is 2+2?")
        assert resp.success is True
        assert resp.result == "The answer is 4"
        assert resp.error is None

    @patch("bread.ai.cli_backend.subprocess.run")
    def test_structured_output_response(self, mock_run: MagicMock) -> None:
        data = {"approved": True, "confidence": 0.9}
        mock_run.return_value = _mock_proc(_make_envelope(result="", structured_output=data))
        backend = CliBackend(_config())
        resp = backend.query("review", json_schema={"type": "object"})
        assert resp.success is True
        assert resp.result == data

    @patch("bread.ai.cli_backend.subprocess.run")
    def test_extracts_model_from_usage(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_proc(_make_envelope())
        backend = CliBackend(_config())
        resp = backend.query("test")
        assert resp.model == "claude-sonnet-4-20250514"

    @patch("bread.ai.cli_backend.subprocess.run")
    def test_extracts_token_counts(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_proc(_make_envelope())
        backend = CliBackend(_config())
        resp = backend.query("test")
        assert resp.input_tokens == 150  # 100 + 50 cache_read
        assert resp.output_tokens == 25

    @patch("bread.ai.cli_backend.subprocess.run")
    def test_extracts_cost(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_proc(_make_envelope())
        backend = CliBackend(_config())
        resp = backend.query("test")
        assert resp.cost_usd == pytest.approx(0.01)

    @patch("bread.ai.cli_backend.subprocess.run")
    def test_extracts_duration(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_proc(_make_envelope(duration_ms=5000))
        backend = CliBackend(_config())
        resp = backend.query("test")
        assert resp.duration_ms == 5000


class TestErrorHandling:
    @patch("bread.ai.cli_backend.subprocess.run")
    def test_timeout_raises_claude_timeout_error(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=30)
        backend = CliBackend(_config())
        with pytest.raises(ClaudeTimeoutError, match="timed out after 30s"):
            backend.query("test")

    @patch("bread.ai.cli_backend.subprocess.run")
    def test_binary_not_found_raises_cli_not_found(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = FileNotFoundError("No such file")
        backend = CliBackend(_config())
        with pytest.raises(ClaudeCliNotFoundError, match="not found"):
            backend.query("test")

    @patch("bread.ai.cli_backend.subprocess.run")
    def test_invalid_json_raises_parse_error(self, mock_run: MagicMock) -> None:
        proc = MagicMock(spec=subprocess.CompletedProcess)
        proc.stdout = "not json at all"
        proc.stderr = ""
        proc.returncode = 0
        mock_run.return_value = proc
        backend = CliBackend(_config())
        with pytest.raises(ClaudeParseError, match="Failed to parse"):
            backend.query("test")

    @patch("bread.ai.cli_backend.subprocess.run")
    def test_is_error_true_returns_unsuccessful(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_proc(_make_envelope(result="something failed", is_error=True))
        backend = CliBackend(_config())
        resp = backend.query("test")
        assert resp.success is False
        assert resp.error is not None

    @patch("bread.ai.cli_backend.subprocess.run")
    def test_nonzero_exit_code_returns_unsuccessful(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_proc(_make_envelope(), returncode=1)
        backend = CliBackend(_config())
        resp = backend.query("test")
        assert resp.success is False

    @patch("bread.ai.cli_backend.subprocess.run")
    def test_rate_limit_in_stderr_detected(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_proc(
            _make_envelope(), stderr="Rate limit exceeded for this model"
        )
        backend = CliBackend(_config())
        resp = backend.query("test")
        assert resp.error is not None
        assert "Rate limited" in resp.error


class TestJsonFallback:
    def test_extracts_json_from_text(self) -> None:
        text = 'Here is the result: {"key": "val"} hope that helps'
        result = _extract_json_from_text(text)
        assert result == {"key": "val"}

    def test_extracts_nested_json_from_text(self) -> None:
        text = 'Response: {"outer": {"inner": 42}} done'
        result = _extract_json_from_text(text)
        assert result == {"outer": {"inner": 42}}

    def test_returns_raw_text_when_no_json(self) -> None:
        text = "No JSON here at all"
        result = _extract_json_from_text(text)
        assert result == text

    def test_parses_plain_json_string(self) -> None:
        text = '{"approved": true, "confidence": 0.8}'
        result = _extract_json_from_text(text)
        assert result == {"approved": True, "confidence": 0.8}

    @patch("bread.ai.cli_backend.subprocess.run")
    def test_fallback_used_when_no_structured_output(self, mock_run: MagicMock) -> None:
        """When --json-schema is used but no structured_output field in envelope."""
        envelope = _make_envelope(result='Here: {"approved": true, "confidence": 0.5}')
        # Remove structured_output if present
        envelope.pop("structured_output", None)
        mock_run.return_value = _mock_proc(envelope)
        backend = CliBackend(_config())
        resp = backend.query("test", json_schema={"type": "object"})
        assert isinstance(resp.result, dict)
        assert resp.result["approved"] is True
