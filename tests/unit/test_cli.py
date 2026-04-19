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

from bread.cli import app
from bread.cli._helpers import start_dashboard_thread

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


class TestReset:
    @pytest.mark.usefixtures("_config_env")
    def test_blocks_in_live_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BREAD_MODE", "live")
        monkeypatch.setenv("ALPACA_LIVE_API_KEY", "pk-live")
        monkeypatch.setenv("ALPACA_LIVE_SECRET_KEY", "sk-live")

        result = runner.invoke(app, ["reset", "--yes", "--skip-broker"])
        assert result.exit_code == 1
        assert "paper-only" in result.stderr

    @pytest.mark.usefixtures("_config_env")
    def test_reset_clears_trade_tables(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from bread.db.database import init_db
        from bread.db.models import OrderLog

        db_path = str(tmp_path / "test.db")
        engine = create_engine(f"sqlite:///{db_path}")
        init_db(engine)
        sf = sessionmaker(bind=engine)
        now = datetime(2026, 4, 1, 15, 0, tzinfo=UTC)
        with sf() as session:
            session.add(OrderLog(
                broker_order_id="b1", symbol="SPY", side="BUY", qty=10,
                status="FILLED", filled_price=500.0, strategy_name="etf_momentum",
                reason="seed", created_at_utc=now, filled_at_utc=now,
            ))
            session.commit()
        engine.dispose()

        result = runner.invoke(app, ["reset", "--yes", "--skip-broker"])

        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Reset complete" in result.stdout
        assert "Local orders deleted:     1" in result.stdout
        assert "app.alpaca.markets" in result.stdout

        engine = create_engine(f"sqlite:///{db_path}")
        sf = sessionmaker(bind=engine)
        with sf() as session:
            assert session.query(OrderLog).count() == 0
        engine.dispose()

    @pytest.mark.usefixtures("_config_env")
    def test_reset_confirm_declined_aborts(self) -> None:
        result = runner.invoke(app, ["reset"], input="n\n")
        assert result.exit_code == 0
        assert "Aborted" in result.stdout

    @pytest.mark.usefixtures("_config_env")
    def test_reset_invokes_broker_when_not_skipped(self) -> None:
        mock_broker = MagicMock()
        mock_broker.cancel_all_orders.return_value = 2
        mock_broker.close_all_positions.return_value = 1

        with patch(
            "bread.execution.alpaca_broker.AlpacaBroker", return_value=mock_broker,
        ):
            result = runner.invoke(app, ["reset", "--yes"])

        assert result.exit_code == 0, result.stdout + result.stderr
        mock_broker.cancel_all_orders.assert_called_once()
        mock_broker.close_all_positions.assert_called_once()
        assert "Broker orders cancelled:  2" in result.stdout
        assert "Broker positions closed:  1" in result.stdout


class TestFetch:
    def test_fetch_requires_symbol(self) -> None:
        result = runner.invoke(app, ["fetch"])
        assert result.exit_code != 0

    @pytest.mark.usefixtures("_config_env")
    def test_fetch_output_format(self) -> None:
        mock_provider = MagicMock()
        mock_provider.get_bars.return_value = _make_ohlcv(250)

        with patch("bread.cli.data.AlpacaDataProvider", return_value=mock_provider):
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

        with patch("bread.cli.data.AlpacaDataProvider", return_value=mock_provider):
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
        from datetime import UTC, datetime

        from bread.execution.models import Account

        mock_broker = MagicMock()
        mock_broker.get_account.return_value = Account(
            equity=10000.0, cash=8000.0, buying_power=8000.0,
            last_equity=9900.0, created_at=datetime(2024, 1, 1, tzinfo=UTC),
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
        from datetime import UTC, datetime

        from bread.core.models import OrderSide, OrderStatus
        from bread.execution.models import Account, BrokerOrder

        mock_broker = MagicMock()
        mock_broker.get_account.return_value = Account(
            equity=10000.0, cash=8000.0, buying_power=8000.0,
            last_equity=9900.0, created_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
        mock_broker.get_positions.return_value = []
        mock_broker.get_orders.return_value = [
            BrokerOrder(
                id="o-1", symbol="SPY",
                side=OrderSide.BUY, status=OrderStatus.ACCEPTED,
                qty=5.0, filled_qty=0.0, filled_avg_price=None,
                submitted_at=None, created_at=None, filled_at=None,
                order_type="market",
            )
        ]

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
            patch("bread.cli.run.start_dashboard_thread") as mock_dash,
            patch("bread.app.run"),
        ):
            result = runner.invoke(app, ["run", "--mode", "paper"])

        assert result.exit_code == 0
        mock_dash.assert_called_once_with(8050)

    @pytest.mark.usefixtures("_config_env")
    def test_no_dashboard_flag_skips_dashboard(self) -> None:
        with (
            patch("bread.cli.run.start_dashboard_thread") as mock_dash,
            patch("bread.app.run"),
        ):
            result = runner.invoke(app, ["run", "--mode", "paper", "--no-dashboard"])

        assert result.exit_code == 0
        mock_dash.assert_not_called()

    @pytest.mark.usefixtures("_config_env")
    def test_custom_dashboard_port(self) -> None:
        with (
            patch("bread.cli.run.start_dashboard_thread") as mock_dash,
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
            patch("bread.cli._helpers.typer.echo", side_effect=lambda m: output.write(m)),
        ):
            start_dashboard_thread(8050)

        assert "http://localhost:8050" in output.getvalue()

    @pytest.mark.usefixtures("_config_env")
    def test_starts_daemon_thread(self) -> None:
        mock_app = MagicMock()
        mock_thread = MagicMock()
        with (
            patch("bread.dashboard.app.create_app", return_value=mock_app),
            patch("bread.cli._helpers.typer.echo"),
            patch("threading.Thread", return_value=mock_thread) as mock_thread_cls,
        ):
            start_dashboard_thread(8050)

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
            start_dashboard_thread(8050)


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


class TestBackfillOrdersHelpers:
    """Pure helpers — no CLI plumbing."""

    def test_infer_strategy_unique_symbol_wins(self) -> None:
        from bread.cli._helpers import infer_strategy_from_symbol

        universes = {"momentum": ["SPY", "QQQ"], "bonds": ["TLT"]}
        assert infer_strategy_from_symbol(universes, "TLT") == "bonds"

    def test_infer_strategy_ambiguous_falls_back_to_legacy(self) -> None:
        from bread.cli._helpers import infer_strategy_from_symbol

        universes = {"momentum": ["SPY"], "fade": ["SPY"]}
        assert infer_strategy_from_symbol(universes, "SPY") == "legacy"

    def test_infer_strategy_unknown_symbol_is_legacy(self) -> None:
        from bread.cli._helpers import infer_strategy_from_symbol

        universes = {"momentum": ["SPY"]}
        assert infer_strategy_from_symbol(universes, "XYZ") == "legacy"

    def test_infer_strategy_case_insensitive(self) -> None:
        from bread.cli._helpers import infer_strategy_from_symbol

        universes = {"momentum": ["SPY"]}
        assert infer_strategy_from_symbol(universes, "spy") == "momentum"


class TestBackfillOrders:
    """Covers `bread backfill-orders` — historical Alpaca order ingestion.

    The command pulls FILLED orders from Alpaca via list_historical_orders
    and inserts any whose broker_order_id isn't already in the local DB.
    Dry-run (default) must not commit.
    """

    def _make_alpaca_order(
        self,
        *,
        order_id: str,
        symbol: str = "SPY",
        side: str = "buy",
        qty: int = 10,
        status: str = "filled",
        filled_avg_price: str = "500.0",
    ):
        """Build a normalized BrokerOrder shaped like a backfilled Alpaca order."""
        from datetime import UTC, datetime

        from bread.core.models import OrderSide, OrderStatus
        from bread.execution.models import BrokerOrder

        status_map = {
            "filled": OrderStatus.FILLED,
            "canceled": OrderStatus.CANCELLED,
            "rejected": OrderStatus.REJECTED,
        }
        side_map = {"buy": OrderSide.BUY, "sell": OrderSide.SELL}
        return BrokerOrder(
            id=order_id,
            symbol=symbol,
            side=side_map.get(side.lower()),
            status=status_map.get(status.lower()),
            qty=float(qty),
            filled_qty=float(qty),
            filled_avg_price=float(filled_avg_price),
            submitted_at=datetime(2026, 3, 15, 13, 30, tzinfo=UTC),
            created_at=datetime(2026, 3, 15, 13, 29, tzinfo=UTC),
            filled_at=datetime(2026, 3, 15, 13, 31, tzinfo=UTC),
            order_type="market",
        )

    def _seed_existing(self, tmp_path: Path, broker_order_id: str) -> None:
        """Put one row in the local DB so we can test idempotency."""
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
                broker_order_id=broker_order_id,
                symbol="SPY", side="BUY", qty=10,
                status="FILLED", raw_filled_price=500.0, filled_price=500.5,
                strategy_name="etf_momentum", reason="seeded",
                created_at_utc=datetime(2026, 3, 15, tzinfo=UTC),
                filled_at_utc=datetime(2026, 3, 15, tzinfo=UTC),
            ))
            session.commit()
        engine.dispose()

    def _count_orders(self, tmp_path: Path) -> int:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from bread.db.models import OrderLog

        engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
        sf = sessionmaker(bind=engine)
        with sf() as session:
            n = session.query(OrderLog).count()
        engine.dispose()
        return n

    @pytest.mark.usefixtures("_config_env")
    def test_inserts_new_orders(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        mock_broker = MagicMock()
        mock_broker.get_account_created_at.return_value = datetime(
            2026, 1, 1, tzinfo=UTC,
        )
        mock_broker.list_historical_orders.return_value = [
            self._make_alpaca_order(order_id="a1"),
            self._make_alpaca_order(order_id="a2", symbol="QQQ", side="sell"),
            self._make_alpaca_order(order_id="a3", symbol="DIA"),
        ]

        with patch(
            "bread.execution.alpaca_broker.AlpacaBroker", return_value=mock_broker,
        ):
            result = runner.invoke(app, ["backfill-orders", "--no-dry-run"])

        assert result.exit_code == 0, result.stdout
        assert "inserted:            3" in result.stdout
        assert self._count_orders(tmp_path) == 3

    @pytest.mark.usefixtures("_config_env")
    def test_skips_already_present(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        self._seed_existing(tmp_path, "a1")

        mock_broker = MagicMock()
        mock_broker.get_account_created_at.return_value = datetime(
            2026, 1, 1, tzinfo=UTC,
        )
        mock_broker.list_historical_orders.return_value = [
            self._make_alpaca_order(order_id="a1"),  # dup
            self._make_alpaca_order(order_id="a2", symbol="QQQ"),
        ]

        with patch(
            "bread.execution.alpaca_broker.AlpacaBroker", return_value=mock_broker,
        ):
            result = runner.invoke(app, ["backfill-orders", "--no-dry-run"])

        assert result.exit_code == 0, result.stdout
        assert "inserted:            1" in result.stdout
        assert "skipped_duplicate:   1" in result.stdout
        assert self._count_orders(tmp_path) == 2  # 1 seeded + 1 new

    @pytest.mark.usefixtures("_config_env")
    def test_filters_non_filled(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        mock_broker = MagicMock()
        mock_broker.get_account_created_at.return_value = datetime(
            2026, 1, 1, tzinfo=UTC,
        )
        mock_broker.list_historical_orders.return_value = [
            self._make_alpaca_order(order_id="a1", status="filled"),
            self._make_alpaca_order(order_id="a2", status="canceled"),
            self._make_alpaca_order(order_id="a3", status="rejected"),
        ]

        with patch(
            "bread.execution.alpaca_broker.AlpacaBroker", return_value=mock_broker,
        ):
            result = runner.invoke(app, ["backfill-orders", "--no-dry-run"])

        assert result.exit_code == 0, result.stdout
        assert "inserted:            1" in result.stdout
        assert "skipped_non_filled:  2" in result.stdout

    @pytest.mark.usefixtures("_config_env")
    def test_dry_run_does_not_commit(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        mock_broker = MagicMock()
        mock_broker.get_account_created_at.return_value = datetime(
            2026, 1, 1, tzinfo=UTC,
        )
        mock_broker.list_historical_orders.return_value = [
            self._make_alpaca_order(order_id="a1"),
        ]

        with patch(
            "bread.execution.alpaca_broker.AlpacaBroker", return_value=mock_broker,
        ):
            result = runner.invoke(app, ["backfill-orders"])  # default dry-run

        assert result.exit_code == 0, result.stdout
        assert "Dry run" in result.stdout
        assert self._count_orders(tmp_path) == 0

    @pytest.mark.usefixtures("_config_env")
    def test_applies_paper_slippage_on_fill_price(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from bread.db.models import OrderLog

        mock_broker = MagicMock()
        mock_broker.get_account_created_at.return_value = datetime(
            2026, 1, 1, tzinfo=UTC,
        )
        mock_broker.list_historical_orders.return_value = [
            self._make_alpaca_order(order_id="a1", side="buy", filled_avg_price="500.0"),
        ]

        with patch(
            "bread.execution.alpaca_broker.AlpacaBroker", return_value=mock_broker,
        ):
            result = runner.invoke(app, ["backfill-orders", "--no-dry-run"])

        assert result.exit_code == 0, result.stdout

        engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
        sf = sessionmaker(bind=engine)
        with sf() as session:
            row = session.query(OrderLog).first()
            assert row is not None
            assert row.raw_filled_price == 500.0
            assert row.filled_price == pytest.approx(500.0 * 1.001)
            assert row.reason == "backfill"
            assert row.broker_order_id == "a1"
        engine.dispose()

    @pytest.mark.usefixtures("_config_env")
    def test_skips_zero_fill_price(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        mock_broker = MagicMock()
        mock_broker.get_account_created_at.return_value = datetime(2026, 1, 1, tzinfo=UTC)
        mock_broker.list_historical_orders.return_value = [
            self._make_alpaca_order(order_id="a1", filled_avg_price="0.0"),
            self._make_alpaca_order(order_id="a2", filled_avg_price="500.0"),
        ]

        with patch("bread.execution.alpaca_broker.AlpacaBroker", return_value=mock_broker):
            result = runner.invoke(app, ["backfill-orders", "--no-dry-run"])

        assert result.exit_code == 0, result.stdout
        assert "inserted:            1" in result.stdout
        assert "skipped_no_price:    1" in result.stdout
        assert self._count_orders(tmp_path) == 1

    @pytest.mark.usefixtures("_config_env")
    def test_rejects_malformed_from(self) -> None:
        result = runner.invoke(app, ["backfill-orders", "--from", "not-a-date"])
        assert result.exit_code == 1
        assert "must be YYYY-MM-DD" in result.stderr
