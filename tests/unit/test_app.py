"""Tests for app orchestrator."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import bread.app as app_module


class TestTick:
    @patch.object(app_module, "_engine")
    @patch.object(app_module, "_config")
    @patch.object(app_module, "_provider")
    @patch.object(app_module, "_session_factory")
    @patch.object(app_module, "_strategies", [])
    def test_tick_calls_reconcile_and_snapshot(
        self,
        mock_sf: MagicMock,
        mock_provider: MagicMock,
        mock_config: MagicMock,
        mock_engine: MagicMock,
    ) -> None:
        """Tick should call reconcile, save_snapshot, then process_signals."""
        mock_engine.get_positions.return_value = []

        app_module.tick()

        mock_engine.reconcile.assert_called_once()
        mock_engine.save_snapshot.assert_called_once()
        mock_engine.process_signals.assert_called_once()

    @patch.object(app_module, "_engine")
    @patch.object(app_module, "_config")
    @patch.object(app_module, "_provider")
    @patch.object(app_module, "_session_factory")
    @patch.object(app_module, "_strategies", [])
    def test_tick_exception_does_not_crash(
        self,
        mock_sf: MagicMock,
        mock_provider: MagicMock,
        mock_config: MagicMock,
        mock_engine: MagicMock,
    ) -> None:
        """Tick should catch exceptions and not propagate."""
        mock_engine.reconcile.side_effect = RuntimeError("broker down")

        # Should not raise
        app_module.tick()

    @patch.object(app_module, "_engine")
    @patch.object(app_module, "_config")
    @patch.object(app_module, "_provider")
    @patch.object(app_module, "_session_factory")
    def test_tick_evaluates_strategies(
        self,
        mock_sf: MagicMock,
        mock_provider: MagicMock,
        mock_config: MagicMock,
        mock_engine: MagicMock,
    ) -> None:
        """Tick should call evaluate on each strategy."""
        mock_strategy = MagicMock()
        mock_strategy.universe = ["SPY"]
        mock_strategy.name = "test"
        mock_strategy.evaluate.return_value = []
        app_module._strategies = [mock_strategy]

        # Mock the session factory context manager and BarCache
        mock_session = MagicMock()
        mock_sf.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_sf.return_value.__exit__ = MagicMock(return_value=False)

        mock_engine.get_positions.return_value = []

        with patch("bread.app.BarCache") as mock_cache_cls, \
             patch("bread.app.compute_indicators") as mock_compute:
            import pandas as pd
            mock_bars = pd.DataFrame({"close": [500.0]})
            mock_cache_inst = mock_cache_cls.return_value
            mock_cache_inst.get_bars.return_value = mock_bars
            mock_compute.return_value = mock_bars

            app_module.tick()

        mock_strategy.evaluate.assert_called_once()
        # Restore
        app_module._strategies = []
