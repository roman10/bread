"""Unit tests for CLI commands."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from bread.__main__ import app

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
