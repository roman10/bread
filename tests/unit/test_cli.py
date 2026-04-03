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
