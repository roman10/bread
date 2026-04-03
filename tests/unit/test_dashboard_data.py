"""Tests for dashboard.data — DashboardData class."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bread.db.database import init_db
from bread.db.models import OrderLog, PortfolioSnapshot


def _make_config():
    """Build a minimal AppConfig for testing (paper mode, in-memory DB)."""
    from bread.core.config import AppConfig

    return AppConfig.model_validate({
        "mode": "paper",
        "db": {"path": ":memory:"},
        "alpaca": {
            "paper_api_key": "test-key",
            "paper_secret_key": "test-secret",
        },
        "strategies": [{"name": "etf_momentum", "config_path": "strategies/etf_momentum.yaml"}],
    })


def _make_sf():
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    return engine, sessionmaker(bind=engine)


def _snap(sf, ts: datetime, equity: float, cash: float = 0, positions: int = 0):
    with sf() as session:
        session.add(PortfolioSnapshot(
            timestamp_utc=ts,
            equity=equity,
            cash=cash or equity,
            positions_value=equity - (cash or equity),
            open_positions=positions,
            daily_pnl=0.0,
        ))
        session.commit()


def _fill(sf, symbol, side, qty, price, filled_at, strategy="etf_momentum", reason="test"):
    with sf() as session:
        session.add(OrderLog(
            broker_order_id=f"{side}-{symbol}-{filled_at.timestamp()}",
            symbol=symbol,
            side=side,
            qty=qty,
            status="FILLED",
            filled_price=price,
            strategy_name=strategy,
            reason=reason,
            created_at_utc=filled_at,
            filled_at_utc=filled_at,
        ))
        session.commit()


class TestDashboardDataBrokerUnavailable:
    """Dashboard should work without broker (API keys missing, network down)."""

    @patch("bread.dashboard.data.AlpacaBroker", side_effect=Exception("no keys"))
    @patch("bread.dashboard.data.get_engine")
    @patch("bread.dashboard.data.init_db")
    @patch("bread.dashboard.data.get_session_factory")
    def test_broker_unavailable_flag(self, mock_sf, mock_init, mock_eng, _mock_broker):
        from bread.dashboard.data import DashboardData

        config = _make_config()
        data = DashboardData(config)

        assert data.broker_available is False

    @patch("bread.dashboard.data.AlpacaBroker", side_effect=Exception("no keys"))
    @patch("bread.dashboard.data.get_engine")
    @patch("bread.dashboard.data.init_db")
    @patch("bread.dashboard.data.get_session_factory")
    def test_account_summary_defaults(self, mock_sf, mock_init, mock_eng, _mock_broker):
        from bread.dashboard.data import DashboardData

        config = _make_config()
        data = DashboardData(config)

        summary = data.get_account_summary()
        assert summary["equity"] == 0.0
        assert summary["daily_pnl"] == 0.0

    @patch("bread.dashboard.data.AlpacaBroker", side_effect=Exception("no keys"))
    @patch("bread.dashboard.data.get_engine")
    @patch("bread.dashboard.data.init_db")
    @patch("bread.dashboard.data.get_session_factory")
    def test_positions_empty(self, mock_sf, mock_init, mock_eng, _mock_broker):
        from bread.dashboard.data import DashboardData

        config = _make_config()
        data = DashboardData(config)

        assert data.get_positions() == []
        assert data.get_open_orders() == []


class TestDashboardDataProperties:
    @patch("bread.dashboard.data.AlpacaBroker", side_effect=Exception("no keys"))
    @patch("bread.dashboard.data.get_engine")
    @patch("bread.dashboard.data.init_db")
    @patch("bread.dashboard.data.get_session_factory")
    def test_mode_and_strategy_names(self, mock_sf, mock_init, mock_eng, _mock_broker):
        from bread.dashboard.data import DashboardData

        config = _make_config()
        data = DashboardData(config)

        assert data.mode == "paper"
        assert data.strategy_names == ["etf_momentum"]


class TestDashboardDataHistorical:
    """Test historical data methods using in-memory SQLite."""

    def test_equity_curve_empty(self):
        from bread.dashboard.data import DashboardData

        engine, sf = _make_sf()
        config = _make_config()

        with patch("bread.dashboard.data.AlpacaBroker", side_effect=Exception("no keys")), \
             patch("bread.dashboard.data.get_engine", return_value=engine), \
             patch("bread.dashboard.data.init_db"), \
             patch("bread.dashboard.data.get_session_factory", return_value=sf):
            data = DashboardData(config)

        assert data.get_equity_curve() == []

    def test_equity_curve_with_data(self):
        from bread.dashboard.data import DashboardData

        engine, sf = _make_sf()
        today = date.today()
        _snap(sf, datetime(today.year, today.month, today.day, 10, 0, tzinfo=UTC), 10_000.0)

        config = _make_config()
        with patch("bread.dashboard.data.AlpacaBroker", side_effect=Exception("no keys")), \
             patch("bread.dashboard.data.get_engine", return_value=engine), \
             patch("bread.dashboard.data.init_db"), \
             patch("bread.dashboard.data.get_session_factory", return_value=sf):
            data = DashboardData(config)

        summaries = data.get_equity_curve(days=7)
        assert len(summaries) == 1
        assert summaries[0].close_equity == 10_000.0

    def test_drawdown_series(self):
        from bread.dashboard.data import DashboardData

        engine, sf = _make_sf()
        _snap(sf, datetime(2026, 3, 1, 10, 0, tzinfo=UTC), 10_000.0)
        _snap(sf, datetime(2026, 3, 2, 10, 0, tzinfo=UTC), 9_500.0)

        config = _make_config()
        with patch("bread.dashboard.data.AlpacaBroker", side_effect=Exception("no keys")), \
             patch("bread.dashboard.data.get_engine", return_value=engine), \
             patch("bread.dashboard.data.init_db"), \
             patch("bread.dashboard.data.get_session_factory", return_value=sf):
            data = DashboardData(config)

        series = data.get_drawdown_series()
        assert len(series) == 2
        assert series[0][1] == 0.0
        assert abs(series[1][1] - 5.0) < 0.01

    def test_period_pnl(self):
        from bread.dashboard.data import DashboardData

        engine, sf = _make_sf()
        today = date.today()
        for i in range(3):
            d = today - timedelta(days=i)
            _snap(sf, datetime(d.year, d.month, d.day, 10, 0, tzinfo=UTC), 10_000.0 + i * 100)

        config = _make_config()
        with patch("bread.dashboard.data.AlpacaBroker", side_effect=Exception("no keys")), \
             patch("bread.dashboard.data.get_engine", return_value=engine), \
             patch("bread.dashboard.data.init_db"), \
             patch("bread.dashboard.data.get_session_factory", return_value=sf):
            data = DashboardData(config)

        result = data.get_period_pnl("daily")
        assert len(result) == 3

    def test_journal_empty(self):
        from bread.dashboard.data import DashboardData

        engine, sf = _make_sf()
        config = _make_config()

        with patch("bread.dashboard.data.AlpacaBroker", side_effect=Exception("no keys")), \
             patch("bread.dashboard.data.get_engine", return_value=engine), \
             patch("bread.dashboard.data.init_db"), \
             patch("bread.dashboard.data.get_session_factory", return_value=sf):
            data = DashboardData(config)

        assert data.get_journal() == []
        summary = data.get_journal_summary([])
        assert summary["total_trades"] == 0

    def test_journal_with_trades(self):
        from bread.dashboard.data import DashboardData

        engine, sf = _make_sf()
        buy_time = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
        sell_time = datetime(2026, 3, 5, 10, 0, tzinfo=UTC)
        _fill(sf, "SPY", "BUY", 10, 450.0, buy_time)
        _fill(sf, "SPY", "SELL", 10, 460.0, sell_time)

        config = _make_config()
        with patch("bread.dashboard.data.AlpacaBroker", side_effect=Exception("no keys")), \
             patch("bread.dashboard.data.get_engine", return_value=engine), \
             patch("bread.dashboard.data.init_db"), \
             patch("bread.dashboard.data.get_session_factory", return_value=sf):
            data = DashboardData(config)

        entries = data.get_journal()
        assert len(entries) == 1
        assert entries[0].symbol == "SPY"
        assert entries[0].pnl == 100.0

        summary = data.get_journal_summary(entries)
        assert summary["total_trades"] == 1
        assert summary["win_rate_pct"] == 100.0


class TestDashboardDataLive:
    """Test live data methods with mocked broker."""

    def test_get_positions_maps_fields(self):
        from bread.dashboard.data import DashboardData

        mock_broker = MagicMock()
        mock_pos = MagicMock()
        mock_pos.symbol = "SPY"
        mock_pos.qty = "10"
        mock_pos.avg_entry_price = "450.00"
        mock_pos.current_price = "455.00"
        mock_pos.unrealized_pl = "50.00"
        mock_pos.unrealized_plpc = "0.0111"
        mock_pos.market_value = "4550.00"
        mock_broker.get_positions.return_value = [mock_pos]

        config = _make_config()
        with patch("bread.dashboard.data.AlpacaBroker", return_value=mock_broker), \
             patch("bread.dashboard.data.get_engine"), \
             patch("bread.dashboard.data.init_db"), \
             patch("bread.dashboard.data.get_session_factory"):
            data = DashboardData(config)

        positions = data.get_positions()
        assert len(positions) == 1
        assert positions[0]["symbol"] == "SPY"
        assert positions[0]["qty"] == 10
        assert positions[0]["unrealized_pnl"] == 50.0

    def test_get_open_orders_maps_fields(self):
        from bread.dashboard.data import DashboardData

        mock_broker = MagicMock()
        mock_order = MagicMock()
        mock_order.symbol = "QQQ"
        mock_order.side = "buy"
        mock_order.qty = "5"
        mock_order.status = "accepted"
        mock_order.type = "market"
        mock_order.submitted_at = "2026-03-10T10:00:00Z"
        mock_broker.get_orders.return_value = [mock_order]

        config = _make_config()
        with patch("bread.dashboard.data.AlpacaBroker", return_value=mock_broker), \
             patch("bread.dashboard.data.get_engine"), \
             patch("bread.dashboard.data.init_db"), \
             patch("bread.dashboard.data.get_session_factory"):
            data = DashboardData(config)

        orders = data.get_open_orders()
        assert len(orders) == 1
        assert orders[0]["symbol"] == "QQQ"
        assert orders[0]["side"] == "BUY"
