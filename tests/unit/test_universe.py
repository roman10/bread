"""Tests for universe providers."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from bread.core.exceptions import ConfigError
from bread.data.universe import (
    IndexProvider,
    PredefinedProvider,
    UniverseRegistry,
    resolve_strategy_universe,
)


class TestPredefinedProvider:
    def test_returns_symbols(self) -> None:
        provider = PredefinedProvider(["SPY", "QQQ", "IWM"])
        assert provider.get_symbols() == ["SPY", "QQQ", "IWM"]

    def test_uppercases_symbols(self) -> None:
        provider = PredefinedProvider(["spy", "qqq"])
        assert provider.get_symbols() == ["SPY", "QQQ"]

    def test_empty_list(self) -> None:
        provider = PredefinedProvider([])
        assert provider.get_symbols() == []

    def test_returns_copy(self) -> None:
        provider = PredefinedProvider(["SPY"])
        result = provider.get_symbols()
        result.append("QQQ")
        assert provider.get_symbols() == ["SPY"]

    def test_no_asset_class_map(self) -> None:
        provider = PredefinedProvider(["SPY"])
        assert provider.get_asset_class_map() == {}

    def test_refresh_is_noop(self) -> None:
        provider = PredefinedProvider(["SPY"])
        provider.refresh()  # should not raise
        assert provider.get_symbols() == ["SPY"]


class TestUniverseRegistry:
    def test_predefined_provider(self, tmp_path: Path) -> None:
        specs = {
            "etfs": {"type": "predefined", "symbols": ["SPY", "QQQ"]},
        }
        registry = UniverseRegistry(specs, tmp_path)
        provider = registry.get("etfs")
        assert provider.get_symbols() == ["SPY", "QQQ"]

    def test_unknown_provider_raises(self, tmp_path: Path) -> None:
        registry = UniverseRegistry({}, tmp_path)
        with pytest.raises(ConfigError, match="Unknown universe provider"):
            registry.get("nonexistent")

    def test_unknown_type_raises(self, tmp_path: Path) -> None:
        specs = {"bad": {"type": "magic"}}
        registry = UniverseRegistry(specs, tmp_path)
        with pytest.raises(Exception):
            registry.get("bad")

    def test_caches_provider_instance(self, tmp_path: Path) -> None:
        specs = {"etfs": {"type": "predefined", "symbols": ["SPY"]}}
        registry = UniverseRegistry(specs, tmp_path)
        p1 = registry.get("etfs")
        p2 = registry.get("etfs")
        assert p1 is p2

    def test_all_providers_returns_only_resolved(self, tmp_path: Path) -> None:
        specs = {
            "a": {"type": "predefined", "symbols": ["SPY"]},
            "b": {"type": "predefined", "symbols": ["QQQ"]},
        }
        registry = UniverseRegistry(specs, tmp_path)
        assert len(registry.all_providers()) == 0  # nothing resolved yet
        registry.get("a")
        assert len(registry.all_providers()) == 1
        registry.get("b")
        assert len(registry.all_providers()) == 2

    def test_index_provider_missing_index_raises(self, tmp_path: Path) -> None:
        specs = {"bad": {"type": "index"}}
        registry = UniverseRegistry(specs, tmp_path)
        with pytest.raises(ConfigError, match="requires 'index' field"):
            registry.get("bad")


class TestIndexProvider:
    def _write_cache(
        self, cache_dir: Path, index_name: str, symbols: list[str],
        sector_map: dict[str, str] | None = None, age_days: int = 0,
    ) -> None:
        """Write a fake cache file."""
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached_at = datetime.now(UTC) - timedelta(days=age_days)
        data = {
            "index": index_name,
            "cached_at": cached_at.isoformat(),
            "symbols": symbols,
            "sector_map": sector_map or {},
        }
        (cache_dir / f"{index_name}.json").write_text(json.dumps(data))

    def test_loads_from_fresh_cache(self, tmp_path: Path) -> None:
        self._write_cache(
            tmp_path, "sp500",
            ["AAPL", "MSFT"],
            {"AAPL": "Information Technology", "MSFT": "Information Technology"},
            age_days=1,
        )
        provider = IndexProvider("sp500", tmp_path, ttl_days=7)
        assert provider.get_symbols() == ["AAPL", "MSFT"]
        assert provider.get_asset_class_map() == {
            "AAPL": "Information Technology",
            "MSFT": "Information Technology",
        }

    def test_stale_cache_triggers_fetch(self, tmp_path: Path) -> None:
        # Write stale cache (10 days old, TTL is 7)
        self._write_cache(tmp_path, "sp500", ["OLD"], age_days=10)

        import pandas as pd

        fake_df = pd.DataFrame({
            "Symbol": ["AAPL", "MSFT"],
            "GICS Sector": ["Information Technology", "Information Technology"],
        })
        with patch("pandas.read_html", return_value=[fake_df]):
            provider = IndexProvider("sp500", tmp_path, ttl_days=7)

        assert provider.get_symbols() == ["AAPL", "MSFT"]

    def test_fetch_failure_falls_back_to_stale_cache(self, tmp_path: Path) -> None:
        self._write_cache(tmp_path, "sp500", ["CACHED"], age_days=10)

        with patch("pandas.read_html", side_effect=Exception("network error")):
            provider = IndexProvider("sp500", tmp_path, ttl_days=7)

        assert provider.get_symbols() == ["CACHED"]

    def test_fetch_failure_no_cache_raises(self, tmp_path: Path) -> None:
        with patch("pandas.read_html", side_effect=Exception("network error")):
            with pytest.raises(Exception, match="network error"):
                IndexProvider("sp500", tmp_path, ttl_days=7)

    def test_unknown_index_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="Unknown index"):
            IndexProvider("ftse100", tmp_path)

    def test_dot_to_hyphen_conversion(self, tmp_path: Path) -> None:
        """Wikipedia uses BRK.B but Alpaca uses BRK-B."""
        import pandas as pd

        fake_df = pd.DataFrame({
            "Symbol": ["BRK.B", "BF.B"],
            "GICS Sector": ["Financials", "Consumer Staples"],
        })
        with patch("pandas.read_html", return_value=[fake_df]):
            provider = IndexProvider("sp500", tmp_path, ttl_days=7)

        assert "BRK-B" in provider.get_symbols()
        assert "BF-B" in provider.get_symbols()

    def test_refresh_overwrites_cache(self, tmp_path: Path) -> None:
        self._write_cache(tmp_path, "sp500", ["OLD"], age_days=0)
        provider = IndexProvider("sp500", tmp_path, ttl_days=7)
        assert provider.get_symbols() == ["OLD"]

        import pandas as pd

        fake_df = pd.DataFrame({
            "Symbol": ["NEW"],
            "GICS Sector": ["Technology"],
        })
        with patch("pandas.read_html", return_value=[fake_df]):
            provider.refresh()

        assert provider.get_symbols() == ["NEW"]

    def test_returns_copies(self, tmp_path: Path) -> None:
        self._write_cache(
            tmp_path, "sp500", ["AAPL"],
            {"AAPL": "Information Technology"},
        )
        provider = IndexProvider("sp500", tmp_path, ttl_days=7)

        symbols = provider.get_symbols()
        symbols.append("HACK")
        assert provider.get_symbols() == ["AAPL"]

        acm = provider.get_asset_class_map()
        acm["HACK"] = "hacking"
        assert "HACK" not in provider.get_asset_class_map()

    def test_nasdaq100_column_mapping(self, tmp_path: Path) -> None:
        import pandas as pd

        fake_df = pd.DataFrame({
            "Ticker": ["AAPL", "NVDA"],
            "GICS Sector": ["Information Technology", "Information Technology"],
        })
        with patch("pandas.read_html", return_value=[fake_df]):
            provider = IndexProvider("nasdaq100", tmp_path, ttl_days=7)

        assert provider.get_symbols() == ["AAPL", "NVDA"]

    def test_corrupted_cache_triggers_fetch(self, tmp_path: Path) -> None:
        """Corrupted JSON cache should be treated as stale and trigger fetch."""
        import pandas as pd

        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "sp500.json").write_text("{INVALID JSON")

        fake_df = pd.DataFrame({
            "Symbol": ["AAPL"],
            "GICS Sector": ["Information Technology"],
        })
        with patch("pandas.read_html", return_value=[fake_df]):
            provider = IndexProvider("sp500", tmp_path, ttl_days=7)

        assert provider.get_symbols() == ["AAPL"]

    def test_creates_cache_directory(self, tmp_path: Path) -> None:
        """Cache directory is created automatically on fetch."""
        import pandas as pd

        cache_dir = tmp_path / "nested" / "cache"
        assert not cache_dir.exists()

        fake_df = pd.DataFrame({
            "Symbol": ["AAPL"],
            "GICS Sector": ["Information Technology"],
        })
        with patch("pandas.read_html", return_value=[fake_df]):
            provider = IndexProvider("sp500", cache_dir, ttl_days=7)

        assert cache_dir.exists()
        assert provider.get_symbols() == ["AAPL"]


class TestResolveStrategyUniverse:
    def test_string_resolves_via_registry(self, tmp_path: Path) -> None:
        specs = {"sp500": {"type": "predefined", "symbols": ["AAPL", "MSFT"]}}
        registry = UniverseRegistry(specs, tmp_path)
        result = resolve_strategy_universe(
            {"universe": "sp500"}, registry, "test_strategy"
        )
        assert result == ["AAPL", "MSFT"]

    def test_list_returns_none(self, tmp_path: Path) -> None:
        registry = UniverseRegistry({}, tmp_path)
        result = resolve_strategy_universe(
            {"universe": ["SPY", "QQQ"]}, registry, "test_strategy"
        )
        assert result is None

    def test_missing_key_returns_none(self, tmp_path: Path) -> None:
        registry = UniverseRegistry({}, tmp_path)
        result = resolve_strategy_universe({}, registry, "test_strategy")
        assert result is None
