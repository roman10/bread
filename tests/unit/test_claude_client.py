"""Tests for Claude AI client — circuit breaker, usage logging, signal review."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from bread.ai.client import CircuitBreaker, ClaudeClient
from bread.ai.models import CliResponse, SignalReview, TradeContext
from bread.core.config import ClaudeSettings
from bread.core.exceptions import ClaudeParseError, ClaudeTimeoutError, ClaudeUnavailableError
from bread.core.models import Signal, SignalDirection
from bread.db.database import init_db
from bread.db.models import ClaudeUsageLog


def _config(**overrides: object) -> ClaudeSettings:
    defaults: dict[str, object] = {
        "enabled": True,
        "circuit_breaker_max_failures": 3,
        "circuit_breaker_cooldown_seconds": 60,
    }
    defaults.update(overrides)
    return ClaudeSettings(**defaults)  # type: ignore[arg-type]


def _make_signal(symbol: str = "SPY") -> Signal:
    return Signal(
        symbol=symbol,
        direction=SignalDirection.BUY,
        strength=0.7,
        stop_loss_pct=0.05,
        strategy_name="test_strategy",
        reason="RSI oversold, SMA crossover",
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


def _success_response(**overrides: object) -> CliResponse:
    defaults: dict[str, object] = {
        "result": {
            "approved": True,
            "confidence": 0.85,
            "reasoning": "Strong momentum signal with healthy risk/reward",
            "risk_flags": [],
        },
        "raw_output": "{}",
        "model": "claude-sonnet-4-20250514",
        "duration_ms": 2000,
        "success": True,
        "error": None,
        "session_id": "test-session",
        "cost_usd": 0.01,
        "input_tokens": 100,
        "output_tokens": 50,
    }
    defaults.update(overrides)
    return CliResponse(**defaults)  # type: ignore[arg-type]


def _failure_response(**overrides: object) -> CliResponse:
    defaults: dict[str, object] = {
        "result": "error",
        "raw_output": "{}",
        "model": "unknown",
        "duration_ms": 500,
        "success": False,
        "error": "CLI error",
        "session_id": "",
        "cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    defaults.update(overrides)
    return CliResponse(**defaults)  # type: ignore[arg-type]


@pytest.fixture()
def db_session_factory() -> sessionmaker:  # type: ignore[type-arg]
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    return sessionmaker(bind=engine)


# ------------------------------------------------------------------
# CircuitBreaker tests
# ------------------------------------------------------------------


class TestCircuitBreaker:
    def test_starts_closed(self) -> None:
        cb = CircuitBreaker(max_failures=3, cooldown_seconds=10)
        assert cb.state == "closed"
        cb.check()  # should not raise

    def test_opens_after_max_failures(self) -> None:
        cb = CircuitBreaker(max_failures=3, cooldown_seconds=10)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == "open"
        with pytest.raises(ClaudeUnavailableError):
            cb.check()

    def test_stays_closed_below_threshold(self) -> None:
        cb = CircuitBreaker(max_failures=3, cooldown_seconds=10)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "closed"
        cb.check()  # should not raise

    def test_half_open_after_cooldown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cb = CircuitBreaker(max_failures=2, cooldown_seconds=5)
        # Simulate failures with a known start time
        base_time = 1000.0
        monkeypatch.setattr(time, "monotonic", lambda: base_time)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "open"

        # After cooldown
        monkeypatch.setattr(time, "monotonic", lambda: base_time + 6.0)
        assert cb.state == "half_open"
        cb.check()  # should not raise in half_open

    def test_closes_on_success_from_half_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cb = CircuitBreaker(max_failures=2, cooldown_seconds=5)
        base_time = 1000.0
        monkeypatch.setattr(time, "monotonic", lambda: base_time)
        cb.record_failure()
        cb.record_failure()

        monkeypatch.setattr(time, "monotonic", lambda: base_time + 6.0)
        assert cb.state == "half_open"
        cb.record_success()
        assert cb.state == "closed"

    def test_reopens_on_failure_from_half_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cb = CircuitBreaker(max_failures=2, cooldown_seconds=5)
        base_time = 1000.0
        monkeypatch.setattr(time, "monotonic", lambda: base_time)
        cb.record_failure()
        cb.record_failure()

        monkeypatch.setattr(time, "monotonic", lambda: base_time + 6.0)
        assert cb.state == "half_open"

        monkeypatch.setattr(time, "monotonic", lambda: base_time + 7.0)
        cb.record_failure()
        assert cb.state == "open"

    def test_success_resets_failure_count(self) -> None:
        cb = CircuitBreaker(max_failures=3, cooldown_seconds=10)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        # Two more failures should not open (count reset to 0)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "closed"


# ------------------------------------------------------------------
# ClaudeClient.review_signal tests
# ------------------------------------------------------------------


class TestReviewSignal:
    @patch("bread.ai.client.CliBackend")
    def test_successful_review(
        self,
        mock_backend_cls: MagicMock,
        db_session_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        mock_backend = mock_backend_cls.return_value
        mock_backend.query.return_value = _success_response()

        client = ClaudeClient(_config(), db_session_factory)
        review = client.review_signal(_make_signal(), _make_context())

        assert isinstance(review, SignalReview)
        assert review.approved is True
        assert review.confidence == pytest.approx(0.85)
        assert "momentum" in review.reasoning

    @patch("bread.ai.client.CliBackend")
    def test_review_passes_correct_schema(
        self,
        mock_backend_cls: MagicMock,
        db_session_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        mock_backend = mock_backend_cls.return_value
        mock_backend.query.return_value = _success_response()

        client = ClaudeClient(_config(), db_session_factory)
        client.review_signal(_make_signal(), _make_context())

        call_kwargs = mock_backend.query.call_args[1]
        assert call_kwargs["json_schema"] == SignalReview.json_schema()

    @patch("bread.ai.client.CliBackend")
    def test_review_uses_review_model(
        self,
        mock_backend_cls: MagicMock,
        db_session_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        mock_backend = mock_backend_cls.return_value
        mock_backend.query.return_value = _success_response()

        client = ClaudeClient(_config(review_model="opus"), db_session_factory)
        client.review_signal(_make_signal(), _make_context())

        call_kwargs = mock_backend.query.call_args[1]
        assert call_kwargs["model"] == "opus"

    @patch("bread.ai.client.CliBackend")
    def test_non_dict_result_raises_parse_error(
        self,
        mock_backend_cls: MagicMock,
        db_session_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        mock_backend = mock_backend_cls.return_value
        mock_backend.query.return_value = _success_response(result="not a dict")

        client = ClaudeClient(_config(), db_session_factory)
        with pytest.raises(ClaudeParseError, match="Expected structured dict"):
            client.review_signal(_make_signal(), _make_context())


# ------------------------------------------------------------------
# Usage logging tests
# ------------------------------------------------------------------


class TestUsageLogging:
    @patch("bread.ai.client.CliBackend")
    def test_successful_call_logged(
        self,
        mock_backend_cls: MagicMock,
        db_session_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        mock_backend = mock_backend_cls.return_value
        mock_backend.query.return_value = _success_response()

        client = ClaudeClient(_config(), db_session_factory)
        client.review_signal(_make_signal(), _make_context())

        with db_session_factory() as session:
            logs = session.execute(select(ClaudeUsageLog)).scalars().all()
            assert len(logs) == 1
            log = logs[0]
            assert log.use_case == "signal_review"
            assert log.success is True
            assert log.model == "claude-sonnet-4-20250514"
            assert log.duration_ms == 2000
            assert log.error is None

    @patch("bread.ai.client.CliBackend")
    def test_failed_call_logged(
        self,
        mock_backend_cls: MagicMock,
        db_session_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        mock_backend = mock_backend_cls.return_value
        mock_backend.query.return_value = _failure_response()

        client = ClaudeClient(_config(), db_session_factory)
        # review_signal will raise ClaudeParseError for non-dict result,
        # but _call still logs usage before the parse error
        with pytest.raises(ClaudeParseError):
            client.review_signal(_make_signal(), _make_context())

        with db_session_factory() as session:
            logs = session.execute(select(ClaudeUsageLog)).scalars().all()
            assert len(logs) == 1
            assert logs[0].success is False
            assert logs[0].error == "CLI error"

    @patch("bread.ai.client.CliBackend")
    def test_exception_logged(
        self,
        mock_backend_cls: MagicMock,
        db_session_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        mock_backend = mock_backend_cls.return_value
        mock_backend.query.side_effect = ClaudeTimeoutError("timed out")

        client = ClaudeClient(_config(), db_session_factory)
        with pytest.raises(ClaudeTimeoutError):
            client.review_signal(_make_signal(), _make_context())

        with db_session_factory() as session:
            logs = session.execute(select(ClaudeUsageLog)).scalars().all()
            assert len(logs) == 1
            assert logs[0].success is False
            assert logs[0].error == "exception before response"

    @patch("bread.ai.client.CliBackend")
    def test_logging_failure_does_not_raise(self, mock_backend_cls: MagicMock) -> None:
        mock_backend = mock_backend_cls.return_value
        mock_backend.query.return_value = _success_response()

        # Use a broken session factory that raises on commit
        broken_factory = MagicMock()
        broken_session = MagicMock()
        broken_session.__enter__ = MagicMock(return_value=broken_session)
        broken_session.__exit__ = MagicMock(return_value=False)
        broken_session.commit.side_effect = RuntimeError("DB down")
        broken_factory.return_value = broken_session

        client = ClaudeClient(_config(), broken_factory)
        # Should not raise despite DB failure
        review = client.review_signal(_make_signal(), _make_context())
        assert review.approved is True


# ------------------------------------------------------------------
# Circuit breaker integration tests
# ------------------------------------------------------------------


class TestCircuitBreakerIntegration:
    @patch("bread.ai.client.CliBackend")
    def test_calls_blocked_when_circuit_open(
        self,
        mock_backend_cls: MagicMock,
        db_session_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        mock_backend = mock_backend_cls.return_value
        mock_backend.query.side_effect = ClaudeTimeoutError("timeout")

        client = ClaudeClient(_config(circuit_breaker_max_failures=2), db_session_factory)

        # Two failures to open circuit
        for _ in range(2):
            with pytest.raises(ClaudeTimeoutError):
                client.review_signal(_make_signal(), _make_context())

        # Third call should be blocked by circuit breaker (not timeout)
        with pytest.raises(ClaudeUnavailableError, match="Circuit breaker open"):
            client.review_signal(_make_signal(), _make_context())

        # Backend should only have been called twice
        assert mock_backend.query.call_count == 2

    @patch("bread.ai.client.CliBackend")
    def test_call_allowed_after_cooldown(
        self,
        mock_backend_cls: MagicMock,
        db_session_factory: sessionmaker,  # type: ignore[type-arg]
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mock_backend = mock_backend_cls.return_value
        call_count = 0

        def side_effect(*args: object, **kwargs: object) -> CliResponse:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise ClaudeTimeoutError("timeout")
            return _success_response()

        mock_backend.query.side_effect = side_effect

        base_time = 1000.0
        monkeypatch.setattr(time, "monotonic", lambda: base_time)

        client = ClaudeClient(
            _config(circuit_breaker_max_failures=2, circuit_breaker_cooldown_seconds=60),
            db_session_factory,
        )

        # Two failures to open circuit
        for _ in range(2):
            with pytest.raises(ClaudeTimeoutError):
                client.review_signal(_make_signal(), _make_context())

        # After cooldown, call should succeed
        monkeypatch.setattr(time, "monotonic", lambda: base_time + 61.0)
        review = client.review_signal(_make_signal(), _make_context())
        assert review.approved is True
