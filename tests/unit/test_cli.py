"""Unit tests for CLI commands."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock, call, patch

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from bread.__main__ import _start_dashboard_thread, app

runner = CliRunner()


@pytest.fixture()
def _config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Set up minimal config dir and env vars for CLI tests."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "default.yaml").write_text(
        dedent("""\
            mode: paper
            db:
              path: {db_path}
        """).format(db_path=str(tmp_path / "test.db"))
    )
    (config_dir / "paper.yaml").write_text("")
    (config_dir / "live.yaml").write_text("")

    monkeypatch.setenv("ALPACA_PAPER_API_KEY", "pk-test")
    monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "sk-test")
    monkeypatch.delenv("BREAD_MODE", raising=False)

    # Patch the config dir so load_config finds our tmp config
    monkeypatch.setattr("bread.core.config.CONFIG_DIR", config_dir)


def _make_ohlcv(rows: int = 250) -> pd.DataFrame:
    np.random.seed(42)
    dates = pd.bdate_range(start=date(2024, 1, 2), periods=rows, tz="UTC")
    close = 100 + np.cumsum(np.random.randn(rows) * 0.5)
    return pd.DataFrame(
        {
            "open": close - np.random.rand(rows) * 0.5,
            "high": close + np.random.rand(rows) * 1.0,
            "low": close - np.random.rand(rows) * 1.0,
            "close": close,
            "volume": np.random.randint(500_000, 2_000_000, size=rows),
        },
        index=pd.DatetimeIndex(dates[:rows], name="timestamp"),
    )


class TestDbInit:
    @pytest.mark.usefixtures("_config_env")
    def test_creates_database(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["db", "init"])
        assert result.exit_code == 0
        assert "Initialized database at" in result.stdout

    @pytest.mark.usefixtures("_config_env")
    def test_creates_db_file(self, tmp_path: Path) -> None:
        runner.invoke(app, ["db", "init"])
        assert (tmp_path / "test.db").exists()


class TestFetch:
    def test_fetch_requires_symbol(self) -> None:
        result = runner.invoke(app, ["fetch"])
        assert result.exit_code != 0

    @pytest.mark.usefixtures("_config_env")
    def test_fetch_output_format(self) -> None:
        mock_provider = MagicMock()
        mock_provider.get_bars.return_value = _make_ohlcv(250)

        with patch("bread.__main__.AlpacaDataProvider", return_value=mock_provider):
            result = runner.invoke(app, ["fetch", "SPY"])

        assert result.exit_code == 0
        output = result.stdout.strip().split("\n")[-1]
        assert output.startswith("SYMBOL=SPY")
        assert "bars=" in output
        assert "start=" in output
        assert "end=" in output
        assert "indicators=14" in output

    @pytest.mark.usefixtures("_config_env")
    def test_fetch_uppercases_symbol(self) -> None:
        mock_provider = MagicMock()
        mock_provider.get_bars.return_value = _make_ohlcv(250)

        with patch("bread.__main__.AlpacaDataProvider", return_value=mock_provider):
            result = runner.invoke(app, ["fetch", "spy"])

        assert result.exit_code == 0
        assert "SYMBOL=SPY" in result.stdout


class TestJournal:
    @pytest.mark.usefixtures("_config_env")
    def test_journal_empty_db(self) -> None:
        result = runner.invoke(app, ["journal"])
        assert result.exit_code == 0
        assert "Trade Journal" in result.stdout
        assert "No completed trades" in result.stdout

    @pytest.mark.usefixtures("_config_env")
    def test_journal_with_trades(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from bread.db.database import init_db
        from bread.db.models import OrderLog

        # Get the DB path from the config fixture
        db_path = str(tmp_path / "test.db")
        engine = create_engine(f"sqlite:///{db_path}")
        init_db(engine)
        sf = sessionmaker(bind=engine)

        t1 = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
        t2 = datetime(2026, 3, 5, 14, 0, tzinfo=UTC)
        with sf() as session:
            session.add(OrderLog(
                broker_order_id="b1", symbol="SPY", side="BUY", qty=10,
                status="FILLED", filled_price=500.0, strategy_name="etf_momentum",
                reason="rsi bounce", created_at_utc=t1, filled_at_utc=t1,
            ))
            session.add(OrderLog(
                broker_order_id="s1", symbol="SPY", side="SELL", qty=10,
                status="FILLED", filled_price=510.0, strategy_name="etf_momentum",
                reason="overbought", created_at_utc=t2, filled_at_utc=t2,
            ))
            session.commit()
        engine.dispose()

        result = runner.invoke(app, ["journal", "--days", "365"])
        assert result.exit_code == 0
        assert "SPY" in result.stdout
        assert "Summary" in result.stdout

    @pytest.mark.usefixtures("_config_env")
    def test_journal_strategy_filter(self) -> None:
        result = runner.invoke(app, ["journal", "--strategy", "nonexistent"])
        assert result.exit_code == 0
        assert "No completed trades" in result.stdout


class TestStatusEnhanced:
    @pytest.mark.usefixtures("_config_env")
    def test_status_shows_risk_section(self) -> None:
        mock_broker = MagicMock()
        mock_broker.get_account.return_value = MagicMock(
            equity="10000", cash="8000", buying_power="8000",
            last_equity="9900",
        )
        mock_broker.get_positions.return_value = []
        mock_broker.get_orders.return_value = []

        with patch(
            "bread.execution.alpaca_broker.AlpacaBroker", return_value=mock_broker,
        ):
            result = runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert "Risk Status" in result.stdout
        assert "Positions:" in result.stdout

    @pytest.mark.usefixtures("_config_env")
    def test_status_shows_open_orders(self) -> None:
        mock_broker = MagicMock()
        mock_broker.get_account.return_value = MagicMock(
            equity="10000", cash="8000", buying_power="8000",
            last_equity="9900",
        )
        mock_broker.get_positions.return_value = []
        mock_order = MagicMock()
        mock_order.symbol = "SPY"
        mock_order.side = "buy"
        mock_order.qty = 5
        mock_order.status = "accepted"
        mock_broker.get_orders.return_value = [mock_order]

        with patch(
            "bread.execution.alpaca_broker.AlpacaBroker", return_value=mock_broker,
        ):
            result = runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert "Open Orders (1)" in result.stdout
        assert "SPY" in result.stdout


class TestRunCmd:
    def test_invalid_mode_rejected(self) -> None:
        result = runner.invoke(app, ["run", "--mode", "yolo"])
        assert result.exit_code == 1
        assert "must be 'paper' or 'live'" in result.stderr

    @pytest.mark.usefixtures("_config_env")
    def test_dashboard_auto_starts_by_default(self) -> None:
        with (
            patch("bread.__main__._start_dashboard_thread") as mock_dash,
            patch("bread.app.run"),
        ):
            result = runner.invoke(app, ["run", "--mode", "paper"])

        assert result.exit_code == 0
        mock_dash.assert_called_once_with(8050)

    @pytest.mark.usefixtures("_config_env")
    def test_no_dashboard_flag_skips_dashboard(self) -> None:
        with (
            patch("bread.__main__._start_dashboard_thread") as mock_dash,
            patch("bread.app.run"),
        ):
            result = runner.invoke(app, ["run", "--mode", "paper", "--no-dashboard"])

        assert result.exit_code == 0
        mock_dash.assert_not_called()

    @pytest.mark.usefixtures("_config_env")
    def test_custom_dashboard_port(self) -> None:
        with (
            patch("bread.__main__._start_dashboard_thread") as mock_dash,
            patch("bread.app.run"),
        ):
            result = runner.invoke(
                app, ["run", "--mode", "paper", "--dashboard-port", "9000"]
            )

        assert result.exit_code == 0
        mock_dash.assert_called_once_with(9000)


class TestStartDashboardThread:
    @pytest.mark.usefixtures("_config_env")
    def test_prints_url(self) -> None:
        from io import StringIO

        mock_app = MagicMock()
        output = StringIO()
        with (
            patch("bread.dashboard.app.create_app", return_value=mock_app),
            patch("bread.__main__.typer.echo", side_effect=lambda m: output.write(m)),
        ):
            _start_dashboard_thread(8050)

        assert "http://localhost:8050" in output.getvalue()

    @pytest.mark.usefixtures("_config_env")
    def test_starts_daemon_thread(self) -> None:
        mock_app = MagicMock()
        mock_thread = MagicMock()
        with (
            patch("bread.dashboard.app.create_app", return_value=mock_app),
            patch("bread.__main__.typer.echo"),
            patch("threading.Thread", return_value=mock_thread) as mock_thread_cls,
        ):
            _start_dashboard_thread(8050)

        mock_thread_cls.assert_called_once()
        assert mock_thread_cls.call_args == call(
            target=mock_thread_cls.call_args.kwargs["target"],
            daemon=True,
            name="dashboard",
        )
        mock_thread.start.assert_called_once()

    def test_silently_skips_when_dash_not_installed(self) -> None:
        with patch.dict("sys.modules", {"bread.dashboard.app": None}):
            # Should not raise
            _start_dashboard_thread(8050)


class TestRepairOrders:
    """Covers `bread repair-orders` backfill behavior and safety.

    The command queries rows where status='FILLED' AND filled_price IS NULL,
    fetches each by broker_order_id from Alpaca, and populates the fill
    price/time. Dry-run (default) must never commit.
    """

    @pytest.mark.usefixtures("_config_env")
    def test_empty_db_nothing_to_do(self) -> None:
        result = runner.invoke(app, ["repair-orders"])
        assert result.exit_code == 0
        assert "Nothing to do" in result.stdout

    def _seed_unpriced_row(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from bread.db.database import init_db
        from bread.db.models import OrderLog

        engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
        init_db(engine)
        sf = sessionmaker(bind=engine)
        with sf() as session:
            session.add(OrderLog(
                broker_order_id="alpaca-123",
                symbol="SPY", side="BUY", qty=10,
                status="FILLED", filled_price=None,
                strategy_name="etf_momentum", reason="seeded",
                created_at_utc=datetime(2026, 4, 1, tzinfo=UTC),
            ))
            session.commit()
        engine.dispose()

    @pytest.mark.usefixtures("_config_env")
    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        self._seed_unpriced_row(tmp_path)

        mock_broker = MagicMock()
        mock_broker.get_order_by_id.return_value = MagicMock(
            filled_avg_price="500.0",
            filled_at=datetime(2026, 4, 1, 15, 0, tzinfo=UTC),
        )

        with patch(
            "bread.execution.alpaca_broker.AlpacaBroker", return_value=mock_broker,
        ):
            result = runner.invoke(app, ["repair-orders", "--dry-run"])

        import re

        assert result.exit_code == 0, result.stdout
        assert "Found 1 FILLED rows" in result.stdout
        assert re.search(r"repaired:\s+1\b", result.stdout)
        assert "Dry run" in result.stdout

        # Verify DB was NOT mutated
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from bread.db.models import OrderLog

        engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
        sf = sessionmaker(bind=engine)
        with sf() as session:
            row = session.query(OrderLog).first()
            assert row is not None
            assert row.filled_price is None  # unchanged
            assert row.filled_at_utc is None
        engine.dispose()

    @pytest.mark.usefixtures("_config_env")
    def test_real_run_writes_fill_with_paper_cost(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        self._seed_unpriced_row(tmp_path)

        fill_time = datetime(2026, 4, 1, 15, 0, tzinfo=UTC)
        mock_broker = MagicMock()
        mock_broker.get_order_by_id.return_value = MagicMock(
            filled_avg_price="500.0",
            filled_at=fill_time,
        )

        with patch(
            "bread.execution.alpaca_broker.AlpacaBroker", return_value=mock_broker,
        ):
            result = runner.invoke(app, ["repair-orders", "--no-dry-run"])

        assert result.exit_code == 0, result.stdout
        assert "Done" in result.stdout

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from bread.db.models import OrderLog

        engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
        sf = sessionmaker(bind=engine)
        with sf() as session:
            row = session.query(OrderLog).first()
            assert row is not None
            assert row.raw_filled_price == 500.0
            # Paper cost model: BUY adjusted up by slippage_pct (default 0.001)
            assert row.filled_price == pytest.approx(500.0 * 1.001)
            assert row.filled_at_utc is not None
        engine.dispose()

        mock_broker.get_order_by_id.assert_called_once_with("alpaca-123")

    @pytest.mark.usefixtures("_config_env")
    def test_rejects_malformed_since(self) -> None:
        result = runner.invoke(app, ["repair-orders", "--since", "not-a-date"])
        assert result.exit_code == 1
        # Error message is written via typer.echo(err=True) → stderr.
        assert "must be YYYY-MM-DD" in result.stderr
