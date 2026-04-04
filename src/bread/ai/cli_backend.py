"""Subprocess wrapper for the Claude Code CLI."""

from __future__ import annotations

import json
import subprocess
import time
from typing import TYPE_CHECKING, Any

from bread.ai.models import CliResponse
from bread.core.exceptions import (
    ClaudeCliNotFoundError,
    ClaudeParseError,
    ClaudeTimeoutError,
)

if TYPE_CHECKING:
    from bread.core.config import ClaudeSettings


class CliBackend:
    """Calls Claude Code CLI via subprocess, uses Max Plan."""

    def __init__(self, config: ClaudeSettings) -> None:
        self._cli_path = config.cli_path
        self._default_model = config.default_model
        self._timeout = config.timeout_seconds
        self._max_turns = config.max_turns

    def query(
        self,
        prompt: str,
        *,
        json_schema: dict[str, object] | None = None,
        system_prompt: str | None = None,
        model: str | None = None,
        allowed_tools: list[str] | None = None,
        max_turns: int | None = None,
        timeout: int | None = None,
    ) -> CliResponse:
        """Run ``claude -p`` and return parsed response."""
        args = self._build_args(
            prompt,
            json_schema=json_schema,
            system_prompt=system_prompt,
            model=model,
            allowed_tools=allowed_tools,
            max_turns=max_turns,
        )
        effective_timeout = timeout or self._timeout

        start = time.monotonic()
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=effective_timeout)
        except FileNotFoundError as exc:
            raise ClaudeCliNotFoundError(
                f"Claude CLI not found at '{self._cli_path}'. "
                "Ensure 'claude' is installed and on PATH."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ClaudeTimeoutError(f"Claude CLI timed out after {effective_timeout}s") from exc
        elapsed_ms = int((time.monotonic() - start) * 1000)

        return self._parse_envelope(proc, elapsed_ms, json_schema is not None)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_args(
        self,
        prompt: str,
        *,
        json_schema: dict[str, object] | None,
        system_prompt: str | None,
        model: str | None,
        allowed_tools: list[str] | None,
        max_turns: int | None,
    ) -> list[str]:
        args = [self._cli_path, "-p", prompt, "--output-format", "json"]

        effective_model = model or self._default_model
        if effective_model:
            args.extend(["--model", effective_model])

        if json_schema is not None:
            args.extend(["--json-schema", json.dumps(json_schema)])

        if system_prompt:
            args.extend(["--append-system-prompt", system_prompt])

        if allowed_tools:
            args.extend(["--allowedTools", ",".join(allowed_tools)])

        effective_max_turns = max_turns or self._max_turns
        args.extend(["--max-turns", str(effective_max_turns)])

        return args

    def _parse_envelope(
        self,
        proc: subprocess.CompletedProcess[str],
        elapsed_ms: int,
        has_schema: bool,
    ) -> CliResponse:
        """Parse the JSON envelope returned by ``claude -p --output-format json``."""
        try:
            envelope: dict[str, Any] = json.loads(proc.stdout)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ClaudeParseError(f"Failed to parse CLI JSON output: {proc.stdout[:500]}") from exc

        is_error = bool(envelope.get("is_error", False))

        # Extract result — structured_output when --json-schema, else result text
        result: dict[str, object] | str
        if has_schema and "structured_output" in envelope:
            result = envelope["structured_output"]
        elif has_schema:
            # Fallback: try to extract JSON from the text result
            result = _extract_json_from_text(str(envelope.get("result", "")))
        else:
            result = str(envelope.get("result", ""))

        # Model name from modelUsage keys
        model_used = ""
        model_usage: Any = envelope.get("modelUsage", {})
        if isinstance(model_usage, dict) and model_usage:
            model_used = str(next(iter(model_usage)))

        # Token counts
        usage: Any = envelope.get("usage", {})
        input_tokens = 0
        output_tokens = 0
        if isinstance(usage, dict):
            input_tokens = int(usage.get("input_tokens", 0))
            input_tokens += int(usage.get("cache_read_input_tokens", 0))
            output_tokens = int(usage.get("output_tokens", 0))

        # Error detection
        error_msg: str | None = None
        if is_error or proc.returncode != 0:
            error_msg = (
                str(envelope.get("result", "")).strip() or proc.stderr or "Unknown CLI error"
            )
        if proc.stderr and "rate" in proc.stderr.lower():
            error_msg = f"Rate limited: {proc.stderr[:200]}"

        return CliResponse(
            result=result,
            raw_output=proc.stdout,
            model=model_used,
            duration_ms=int(envelope.get("duration_ms", elapsed_ms)),
            success=not is_error and proc.returncode == 0,
            error=error_msg,
            session_id=str(envelope.get("session_id", "")),
            cost_usd=float(envelope.get("total_cost_usd", 0.0)),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


def _extract_json_from_text(text: str) -> dict[str, object] | str:
    """Attempt to extract a JSON object from free-form text.

    Tries ``json.loads`` on the full text first, then falls back to
    extracting the substring between the first ``{`` and last ``}``.
    Returns the original text if no valid JSON is found.
    """
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass

    # Find first { to last } — handles arbitrary nesting
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        try:
            parsed = json.loads(text[first : last + 1])
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass

    return text
