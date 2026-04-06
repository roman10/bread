"""Tests for execution.alpaca_broker."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from requests.exceptions import ConnectionError

from bread.core.config import AppConfig
from bread.core.exceptions import ExecutionError, OrderError
from bread.execution.alpaca_broker import AlpacaBroker


def _make_config(monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    """Create a paper config with fake API keys."""
    monkeypatch.setenv("ALPACA_PAPER_API_KEY", "fake-key")
    monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "fake-secret")
    from bread.core.config import load_config

    return load_config()


class TestAlpacaBroker:
    @patch("bread.execution.alpaca_broker.TradingClient")
    def test_get_account(self, mock_client_cls: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(monkeypatch)
        mock_client = mock_client_cls.return_value
        mock_account = SimpleNamespace(equity="10000", buying_power="8000")
        mock_client.get_account.return_value = mock_account

        broker = AlpacaBroker(config)
        account = broker.get_account()

        assert account.equity == "10000"
        mock_client.get_account.assert_called_once()

    @patch("bread.execution.alpaca_broker._read_retrier.wait", new=lambda *a, **kw: 0)
    @patch("bread.execution.alpaca_broker.TradingClient")
    def test_get_account_retries_on_connection_error(
        self, mock_client_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _make_config(monkeypatch)
        mock_client = mock_client_cls.return_value
        mock_account = SimpleNamespace(equity="10000", buying_power="8000")
        mock_client.get_account.side_effect = [ConnectionError("reset"), mock_account]

        broker = AlpacaBroker(config)
        account = broker.get_account()

        assert account.equity == "10000"
        assert mock_client.get_account.call_count == 2

    @patch("bread.execution.alpaca_broker.TradingClient")
    def test_get_account_error(
        self, mock_client_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _make_config(monkeypatch)
        mock_client = mock_client_cls.return_value
        mock_client.get_account.side_effect = Exception("connection failed")

        broker = AlpacaBroker(config)
        with pytest.raises(ExecutionError, match="Failed to get account"):
            broker.get_account()

    @patch("bread.execution.alpaca_broker.TradingClient")
    def test_submit_bracket_order(
        self, mock_client_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _make_config(monkeypatch)
        mock_client = mock_client_cls.return_value
        mock_order = SimpleNamespace(id="order-abc-123")
        mock_client.submit_order.return_value = mock_order

        broker = AlpacaBroker(config)
        order_id = broker.submit_bracket_order("SPY", 10, 475.0, 525.0)

        assert order_id == "order-abc-123"
        call_args = mock_client.submit_order.call_args[0][0]
        assert call_args.symbol == "SPY"
        assert call_args.qty == 10

    @patch("bread.execution.alpaca_broker.TradingClient")
    def test_submit_bracket_order_error(
        self, mock_client_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _make_config(monkeypatch)
        mock_client = mock_client_cls.return_value
        mock_client.submit_order.side_effect = Exception("insufficient funds")

        broker = AlpacaBroker(config)
        with pytest.raises(OrderError, match="Failed to submit bracket order"):
            broker.submit_bracket_order("SPY", 10, 475.0, 525.0)

    @patch("bread.execution.alpaca_broker.TradingClient")
    def test_close_position(
        self, mock_client_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _make_config(monkeypatch)
        mock_client = mock_client_cls.return_value
        mock_client.get_orders.return_value = []
        mock_order = SimpleNamespace(id="close-order-456")
        mock_client.close_position.return_value = mock_order

        broker = AlpacaBroker(config)
        order_id = broker.close_position("SPY")

        assert order_id == "close-order-456"

    @patch("bread.execution.alpaca_broker.TradingClient")
    def test_close_position_not_found(
        self, mock_client_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _make_config(monkeypatch)
        mock_client = mock_client_cls.return_value
        mock_client.get_orders.return_value = []
        mock_client.close_position.side_effect = Exception("position not found")

        broker = AlpacaBroker(config)
        result = broker.close_position("SPY")

        assert result is None

    @patch("bread.execution.alpaca_broker.TradingClient")
    def test_close_position_cancels_open_orders_first(
        self, mock_client_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bracket OCO legs hold shares; they must be cancelled before close."""
        config = _make_config(monkeypatch)
        mock_client = mock_client_cls.return_value
        mock_client.get_orders.return_value = [
            SimpleNamespace(id="leg-1", symbol="XLE"),
            SimpleNamespace(id="leg-2", symbol="XLE"),
            SimpleNamespace(id="leg-3", symbol="SPY"),  # different symbol, not cancelled
        ]
        mock_client.close_position.return_value = SimpleNamespace(id="close-order-789")

        broker = AlpacaBroker(config)
        order_id = broker.close_position("XLE")

        assert order_id == "close-order-789"
        assert mock_client.cancel_order_by_id.call_count == 2
        cancelled_ids = [call.args[0] for call in mock_client.cancel_order_by_id.call_args_list]
        assert "leg-1" in cancelled_ids
        assert "leg-2" in cancelled_ids

    @patch("bread.execution.alpaca_broker.TradingClient")
    def test_close_position_cancel_failure_does_not_block_close(
        self, mock_client_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If cancelling an order fails, the close attempt should still proceed."""
        config = _make_config(monkeypatch)
        mock_client = mock_client_cls.return_value
        mock_client.get_orders.return_value = [
            SimpleNamespace(id="leg-1", symbol="XLE"),
        ]
        mock_client.cancel_order_by_id.side_effect = Exception("cancel failed")
        mock_client.close_position.return_value = SimpleNamespace(id="close-order-789")

        broker = AlpacaBroker(config)
        order_id = broker.close_position("XLE")

        assert order_id == "close-order-789"
        mock_client.close_position.assert_called_once()

    @patch("bread.execution.alpaca_broker.TradingClient")
    def test_get_positions(
        self, mock_client_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _make_config(monkeypatch)
        mock_client = mock_client_cls.return_value
        mock_client.get_all_positions.return_value = [
            SimpleNamespace(symbol="SPY", qty="10"),
        ]

        broker = AlpacaBroker(config)
        positions = broker.get_positions()

        assert len(positions) == 1
        assert positions[0].symbol == "SPY"
