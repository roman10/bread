"""Unit tests for configuration loading and validation."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from bread.core.config import deep_merge, load_config
from bread.core.exceptions import ConfigError


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    """Create a minimal config directory."""
    d = tmp_path / "config"
    d.mkdir()
    (d / "default.yaml").write_text(
        dedent("""\
            mode: paper
            app:
              log_level: INFO
            db:
              path: data/bread.db
            data:
              default_timeframe: "1Day"
              lookback_days: 200
            indicators:
              sma_periods: [20, 50, 200]
              ema_periods: [9, 21]
              rsi_period: 14
              macd_fast: 12
              macd_slow: 26
              macd_signal: 9
              atr_period: 14
              bollinger_period: 20
              bollinger_stddev: 2.0
              volume_sma_period: 20
        """)
    )
    (d / "paper.yaml").write_text("")
    (d / "live.yaml").write_text("app:\n  log_level: WARNING\n")
    return d


class TestDeepMerge:
    def test_scalar_override(self) -> None:
        assert deep_merge({"a": 1}, {"a": 2}) == {"a": 2}

    def test_nested_merge(self) -> None:
        base = {"a": {"x": 1, "y": 2}}
        override = {"a": {"y": 3, "z": 4}}
        assert deep_merge(base, override) == {"a": {"x": 1, "y": 3, "z": 4}}

    def test_list_replaces(self) -> None:
        assert deep_merge({"a": [1, 2, 3]}, {"a": [4, 5]}) == {"a": [4, 5]}

    def test_none_replaces(self) -> None:
        assert deep_merge({"a": 1}, {"a": None}) == {"a": None}


class TestConfigLoading:
    def test_valid_paper_config(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ALPACA_PAPER_API_KEY", "pk-test")
        monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "sk-test")
        monkeypatch.delenv("BREAD_MODE", raising=False)

        cfg = load_config(config_dir)
        assert cfg.mode == "paper"
        assert cfg.alpaca.paper_api_key == "pk-test"

    def test_paper_mode_does_not_require_live_credentials(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ALPACA_PAPER_API_KEY", "pk-test")
        monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "sk-test")
        monkeypatch.delenv("ALPACA_LIVE_API_KEY", raising=False)
        monkeypatch.delenv("ALPACA_LIVE_SECRET_KEY", raising=False)
        monkeypatch.delenv("BREAD_MODE", raising=False)

        cfg = load_config(config_dir)
        assert cfg.mode == "paper"
        assert cfg.alpaca.live_api_key is None

    def test_live_mode_does_not_require_paper_credentials(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BREAD_MODE", "live")
        monkeypatch.setenv("ALPACA_LIVE_API_KEY", "lk-test")
        monkeypatch.setenv("ALPACA_LIVE_SECRET_KEY", "ls-test")
        monkeypatch.delenv("ALPACA_PAPER_API_KEY", raising=False)
        monkeypatch.delenv("ALPACA_PAPER_SECRET_KEY", raising=False)

        cfg = load_config(config_dir)
        assert cfg.mode == "live"
        assert cfg.app.log_level == "WARNING"  # from live.yaml overlay

    def test_missing_paper_credentials_fails(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ALPACA_PAPER_API_KEY", raising=False)
        monkeypatch.delenv("ALPACA_PAPER_SECRET_KEY", raising=False)
        monkeypatch.delenv("BREAD_MODE", raising=False)

        with pytest.raises(ConfigError):
            load_config(config_dir)

    def test_invalid_mode_fails(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BREAD_MODE", "invalid")
        with pytest.raises(ConfigError):
            load_config(config_dir)

    def test_invalid_lookback_days(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ALPACA_PAPER_API_KEY", "pk")
        monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "sk")
        monkeypatch.delenv("BREAD_MODE", raising=False)
        # Write a config with lookback_days below minimum
        (config_dir / "default.yaml").write_text(
            "mode: paper\ndata:\n  lookback_days: 10\n"
        )
        with pytest.raises(ConfigError):
            load_config(config_dir)


class TestPaperCostSettings:
    def test_paper_cost_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALPACA_PAPER_API_KEY", "pk")
        monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "sk")
        monkeypatch.delenv("BREAD_MODE", raising=False)
        cfg = load_config()
        assert cfg.execution.paper_cost.enabled is True
        assert cfg.execution.paper_cost.slippage_pct == 0.001
        assert cfg.execution.paper_cost.commission_per_trade == 0.0

    def test_paper_cost_validation_rejects_negative(self) -> None:
        from bread.core.config import PaperCostSettings
        with pytest.raises(Exception):  # pydantic ValidationError
            PaperCostSettings(slippage_pct=-0.01)
