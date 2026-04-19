"""Tests for TradingApp orchestrator."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from bread.app import TradingApp


def _make_app(monkeypatch: pytest.MonkeyPatch) -> TradingApp:
    """Construct TradingApp with all components mocked (bypasses _initialize)."""
    monkeypatch.setenv("ALPACA_PAPER_API_KEY", "fake")
    monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "fake")
    from bread.core.config import load_config

    app = TradingApp(load_config())

    # Inject mocks — no API keys or real DB needed
    app._engine = MagicMock()
    app._engine.get_positions.return_value = []
    app._provider = MagicMock()

    mock_sf = MagicMock()
    mock_session = MagicMock()
    mock_sf.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_sf.return_value.__exit__ = MagicMock(return_value=False)
    app._session_factory = mock_sf
    app._strategies = []
    app._alert_manager = None
    return app


class TestTick:
    def test_tick_returns_early_when_not_initialized(self, monkeypatch) -> None:
        """tick() should bail gracefully if components were never set."""
        monkeypatch.setenv("ALPACA_PAPER_API_KEY", "fake")
        monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "fake")
        from bread.core.config import load_config

        app = TradingApp(load_config())
        # _engine is None — tick() should log an error and return, not raise
        app.tick()

    def test_tick_calls_reconcile_and_snapshot(self, monkeypatch) -> None:
        """tick() must reconcile positions and save a snapshot every cycle."""
        app = _make_app(monkeypatch)
        app.tick()
        app._engine.reconcile.assert_called_once()
        app._engine.save_snapshot.assert_called_once()
        app._engine.process_signals.assert_called_once()

    def test_tick_exception_does_not_crash(self, monkeypatch) -> None:
        """A broker failure inside tick() should be caught, not propagated."""
        app = _make_app(monkeypatch)
        app._engine.reconcile.side_effect = RuntimeError("broker down")
        app.tick()  # must not raise

    def test_tick_evaluates_strategies(self, monkeypatch) -> None:
        """tick() should call evaluate() on each active strategy."""
        app = _make_app(monkeypatch)
        mock_strategy = MagicMock()
        mock_strategy.universe = ["SPY"]
        mock_strategy.name = "test"
        mock_strategy.evaluate.return_value = []
        app._strategies = [mock_strategy]

        mock_bars = pd.DataFrame({"close": [500.0]})
        with (
            patch("bread.app.BarCache") as mock_cache_cls,
            patch("bread.app.compute_indicators", return_value=mock_bars),
        ):
            mock_cache_cls.return_value.get_bars_batch.return_value = {"SPY": mock_bars}
            app.tick()

        mock_strategy.evaluate.assert_called_once()

    def test_tick_deduplicates_symbols_across_strategies(self, monkeypatch) -> None:
        """Multiple strategies sharing a symbol trigger only one data fetch."""
        app = _make_app(monkeypatch)
        strat1 = MagicMock()
        strat1.universe = ["SPY", "QQQ"]
        strat1.name = "s1"
        strat1.evaluate.return_value = []
        strat2 = MagicMock()
        strat2.universe = ["SPY", "IWM"]
        strat2.name = "s2"
        strat2.evaluate.return_value = []
        app._strategies = [strat1, strat2]

        mock_bars = pd.DataFrame({"close": [500.0]})
        with (
            patch("bread.app.BarCache") as mock_cache_cls,
            patch("bread.app.compute_indicators", return_value=mock_bars),
        ):
            mock_cache_cls.return_value.get_bars_batch.return_value = {
                "SPY": mock_bars,
                "QQQ": mock_bars,
                "IWM": mock_bars,
            }
            app.tick()

        fetched_symbols = mock_cache_cls.return_value.get_bars_batch.call_args[0][0]
        assert fetched_symbols == ["SPY", "QQQ", "IWM"]  # deduplicated, order preserved
        strat1.evaluate.assert_called_once()
        strat2.evaluate.assert_called_once()


class TestOnJobMissed:
    def _make_event(self, job_id: str) -> MagicMock:
        event = MagicMock()
        event.job_id = job_id
        event.scheduled_run_time = datetime(2025, 1, 8, 10, 0)
        return event

    @patch("bread.data.cache.is_market_open", return_value=True)
    def test_market_open_triggers_recovery(self, mock_market: MagicMock, monkeypatch) -> None:
        app = _make_app(monkeypatch)
        app._scheduler = MagicMock()
        app._on_job_missed(self._make_event("trading_tick"))
        app._scheduler.add_job.assert_called_once_with(
            app.tick, id="recovery_tick", replace_existing=True
        )

    @patch("bread.data.cache.is_market_open", return_value=False)
    def test_market_closed_no_recovery(self, mock_market: MagicMock, monkeypatch) -> None:
        app = _make_app(monkeypatch)
        app._scheduler = MagicMock()
        app._on_job_missed(self._make_event("trading_tick"))
        app._scheduler.add_job.assert_not_called()

    def test_non_tick_job_no_recovery(self, monkeypatch) -> None:
        app = _make_app(monkeypatch)
        app._scheduler = MagicMock()
        app._on_job_missed(self._make_event("daily_summary"))
        app._scheduler.add_job.assert_not_called()


class TestConfirmLiveMode:
    def test_tty_confirm_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bread.app import TradingApp

        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _prompt: "CONFIRM")
        TradingApp._confirm_live_mode()  # should not raise

    def test_tty_wrong_input_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bread.app import TradingApp

        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _prompt: "no")
        with pytest.raises(SystemExit) as exc:
            TradingApp._confirm_live_mode()
        assert exc.value.code == 0

    def test_non_tty_with_env_bypass_proceeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bread.app import TradingApp

        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setenv("BREAD_LIVE_CONFIRM", "I_UNDERSTAND")
        TradingApp._confirm_live_mode()  # should not raise

    def test_non_tty_without_env_exits_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bread.app import TradingApp

        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.delenv("BREAD_LIVE_CONFIRM", raising=False)
        with pytest.raises(SystemExit) as exc:
            TradingApp._confirm_live_mode()
        assert exc.value.code == 1

    def test_non_tty_with_wrong_env_value_exits_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bread.app import TradingApp

        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setenv("BREAD_LIVE_CONFIRM", "yes")
        with pytest.raises(SystemExit) as exc:
            TradingApp._confirm_live_mode()
        assert exc.value.code == 1


class TestLegacyDbWarning:
    def test_warns_when_legacy_alongside_per_mode(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from bread.app import _warn_if_legacy_db

        (tmp_path / "bread.db").touch()
        active = tmp_path / "bread-paper.db"

        with caplog.at_level("WARNING", logger="bread.app"):
            _warn_if_legacy_db(active)

        assert any("Legacy database file detected" in r.message for r in caplog.records)

    def test_silent_when_no_legacy_file(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from bread.app import _warn_if_legacy_db

        active = tmp_path / "bread-paper.db"
        with caplog.at_level("WARNING", logger="bread.app"):
            _warn_if_legacy_db(active)
        assert not any("Legacy database" in r.message for r in caplog.records)

    def test_silent_when_active_path_is_the_legacy_file(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """If user explicitly points BREAD_DB_PATH at data/bread.db, don't nag."""
        from bread.app import _warn_if_legacy_db

        legacy = tmp_path / "bread.db"
        legacy.touch()
        with caplog.at_level("WARNING", logger="bread.app"):
            _warn_if_legacy_db(legacy)
        assert not any("Legacy database" in r.message for r in caplog.records)
