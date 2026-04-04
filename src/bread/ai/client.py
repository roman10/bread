"""Claude AI client — orchestrator with circuit breaker and usage tracking."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from bread.ai.cli_backend import CliBackend
from bread.ai.models import CliResponse, SignalReview, TradeContext
from bread.core.exceptions import ClaudeError, ClaudeParseError, ClaudeUnavailableError
from bread.db.models import ClaudeUsageLog

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from bread.core.config import ClaudeSettings
    from bread.core.models import Signal

logger = logging.getLogger(__name__)

_REVIEW_SYSTEM_PROMPT = (
    "You are a risk-aware trading assistant for an automated swing trading bot. "
    "Review the proposed trade signal and provide your assessment. "
    "Be conservative — when in doubt, reject. Focus on risk/reward, "
    "current market conditions, and portfolio concentration."
)


class CircuitBreaker:
    """Three-state circuit breaker: CLOSED -> OPEN -> HALF_OPEN -> CLOSED."""

    def __init__(self, max_failures: int, cooldown_seconds: int) -> None:
        self._max_failures = max_failures
        self._cooldown = cooldown_seconds
        self._failure_count = 0
        self._last_failure_time: float = 0.0
        self._state = "closed"  # "closed" | "open" | "half_open"

    @property
    def state(self) -> str:
        if self._state == "open":
            if time.monotonic() - self._last_failure_time >= self._cooldown:
                self._state = "half_open"
        return self._state

    def check(self) -> None:
        """Raise :class:`ClaudeUnavailableError` if the circuit is open."""
        if self.state == "open":
            raise ClaudeUnavailableError(
                f"Circuit breaker open after {self._max_failures} consecutive failures. "
                f"Cooldown: {self._cooldown}s"
            )

    def record_success(self) -> None:
        self._failure_count = 0
        self._state = "closed"

    def record_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._failure_count >= self._max_failures:
            self._state = "open"


class ClaudeClient:
    """High-level Claude AI client for bread trading bot.

    Wraps :class:`CliBackend` with circuit-breaker protection, usage
    logging, and domain-specific methods.
    """

    def __init__(
        self,
        config: ClaudeSettings,
        session_factory: sessionmaker[Session],
    ) -> None:
        self._backend = CliBackend(config)
        self._config = config
        self._session_factory = session_factory
        self._circuit_breaker = CircuitBreaker(
            max_failures=config.circuit_breaker_max_failures,
            cooldown_seconds=config.circuit_breaker_cooldown_seconds,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def review_signal(self, signal: Signal, context: TradeContext) -> SignalReview:
        """Ask Claude to review a trading signal. Returns approve/reject."""
        prompt = self._build_review_prompt(signal, context)
        response = self._call(
            prompt=prompt,
            json_schema=SignalReview.json_schema(),
            system_prompt=_REVIEW_SYSTEM_PROMPT,
            model=self._config.review_model,
            use_case="signal_review",
        )
        if isinstance(response.result, dict):
            return SignalReview.from_dict(response.result)
        raise ClaudeParseError(
            f"Expected structured dict, got {type(response.result).__name__}: "
            f"{str(response.result)[:200]}"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _call(
        self,
        prompt: str,
        *,
        json_schema: dict[str, object] | None = None,
        system_prompt: str | None = None,
        model: str | None = None,
        allowed_tools: list[str] | None = None,
        max_turns: int | None = None,
        timeout: int | None = None,
        use_case: str = "unknown",
    ) -> CliResponse:
        """Execute a CLI call with circuit-breaker protection and usage logging."""
        self._circuit_breaker.check()

        try:
            response = self._backend.query(
                prompt,
                json_schema=json_schema,
                system_prompt=system_prompt,
                model=model,
                allowed_tools=allowed_tools,
                max_turns=max_turns,
                timeout=timeout,
            )
        except ClaudeError:
            self._circuit_breaker.record_failure()
            self._log_usage(use_case, prompt, None)
            raise

        if response.success:
            self._circuit_breaker.record_success()
        else:
            self._circuit_breaker.record_failure()

        self._log_usage(use_case, prompt, response)
        return response

    def _build_review_prompt(self, signal: Signal, context: TradeContext) -> str:
        positions_str = ", ".join(context.open_positions) or "none"
        return (
            f"Review this trading signal:\n"
            f"Symbol: {signal.symbol}\n"
            f"Direction: {signal.direction.value}\n"
            f"Strength: {signal.strength:.2f}\n"
            f"Stop Loss: {signal.stop_loss_pct:.1%}\n"
            f"Strategy: {signal.strategy_name}\n"
            f"Reason: {signal.reason}\n\n"
            f"Portfolio context:\n"
            f"Equity: ${context.equity:,.2f}\n"
            f"Buying Power: ${context.buying_power:,.2f}\n"
            f"Open Positions: {positions_str}\n"
            f"Daily P&L: ${context.daily_pnl:,.2f}\n"
            f"Weekly P&L: ${context.weekly_pnl:,.2f}\n"
            f"Peak Equity: ${context.peak_equity:,.2f}\n"
        )

    def _log_usage(
        self,
        use_case: str,
        prompt: str,
        response: CliResponse | None,
    ) -> None:
        """Log Claude usage to database. Never raises."""
        try:
            with self._session_factory() as session:
                session.add(
                    ClaudeUsageLog(
                        model=response.model if response else "unknown",
                        use_case=use_case,
                        prompt_length=len(prompt),
                        duration_ms=response.duration_ms if response else 0,
                        success=response.success if response else False,
                        error=response.error if response else "exception before response",
                        cost_usd=response.cost_usd if response else 0.0,
                        input_tokens=response.input_tokens if response else 0,
                        output_tokens=response.output_tokens if response else 0,
                    )
                )
                session.commit()
        except Exception:
            logger.exception("Failed to log Claude usage")
