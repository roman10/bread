"""Universe providers — resolve symbol lists from various sources."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from bread.core.exceptions import ConfigError

if TYPE_CHECKING:
    import pandas as pd

logger = logging.getLogger(__name__)


class UniverseProvider(ABC):
    """Abstract base for symbol list providers."""

    @abstractmethod
    def get_symbols(self) -> list[str]:
        """Return the current list of tradeable symbols."""
        ...

    def get_asset_class_map(self) -> dict[str, str]:
        """Return symbol -> asset_class_name mapping.

        Empty dict if the provider has no classification info.
        """
        return {}

    def refresh(self) -> None:
        """Re-fetch constituency data. No-op by default."""


class PredefinedProvider(UniverseProvider):
    """Static symbol list — backward compatible with YAML-defined universes."""

    def __init__(self, symbols: list[str]) -> None:
        self._symbols = [s.upper() for s in symbols]

    def get_symbols(self) -> list[str]:
        return list(self._symbols)


# ---------------------------------------------------------------------------
# Wikipedia-backed index provider
# ---------------------------------------------------------------------------

_INDEX_URLS: dict[str, str] = {
    "sp500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "nasdaq100": "https://en.wikipedia.org/wiki/Nasdaq-100",
}

# Column names per index in the Wikipedia tables
_INDEX_COLUMNS: dict[str, dict[str, str]] = {
    "sp500": {"symbol": "Symbol", "sector": "GICS Sector"},
    "nasdaq100": {"symbol": "Ticker", "sector": "GICS Sector"},
}


class IndexProvider(UniverseProvider):
    """Loads index constituents and GICS sector from Wikipedia.

    Results are cached to a local JSON file with a configurable TTL.
    """

    def __init__(
        self,
        index_name: str,
        cache_dir: Path,
        ttl_days: int = 7,
    ) -> None:
        if index_name not in _INDEX_URLS:
            raise ConfigError(
                f"Unknown index: {index_name}. Available: {list(_INDEX_URLS.keys())}"
            )
        self._index_name = index_name
        self._cache_dir = cache_dir
        self._ttl_days = ttl_days
        self._symbols: list[str] = []
        self._sector_map: dict[str, str] = {}
        self._load()

    @property
    def _cache_path(self) -> Path:
        return self._cache_dir / f"{self._index_name}.json"

    def get_symbols(self) -> list[str]:
        return list(self._symbols)

    def get_asset_class_map(self) -> dict[str, str]:
        return dict(self._sector_map)

    def refresh(self) -> None:
        """Force re-fetch from Wikipedia, update cache."""
        self._fetch_and_cache()

    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load from cache if fresh, otherwise fetch."""
        if self._cache_is_fresh():
            self._load_from_cache()
        else:
            try:
                self._fetch_and_cache()
            except Exception:
                # Fall back to stale cache if available
                if self._cache_path.exists():
                    logger.warning(
                        "Failed to fetch %s from Wikipedia, using stale cache",
                        self._index_name,
                    )
                    self._load_from_cache()
                else:
                    raise

    def _cache_is_fresh(self) -> bool:
        if not self._cache_path.exists():
            return False
        try:
            data = json.loads(self._cache_path.read_text())
            cached_at = datetime.fromisoformat(data["cached_at"])
            age_days = (datetime.now(UTC) - cached_at).days
            return age_days < self._ttl_days
        except (json.JSONDecodeError, KeyError, ValueError):
            return False

    def _load_from_cache(self) -> None:
        data = json.loads(self._cache_path.read_text())
        self._symbols = data["symbols"]
        self._sector_map = data.get("sector_map", {})
        logger.info(
            "Loaded %d symbols for %s from cache", len(self._symbols), self._index_name
        )

    def _fetch_and_cache(self) -> None:
        """Scrape Wikipedia and write results to cache."""
        import pandas as pd

        url = _INDEX_URLS[self._index_name]
        cols = _INDEX_COLUMNS[self._index_name]

        logger.info("Fetching %s constituency from Wikipedia", self._index_name)
        tables = pd.read_html(url)

        # Find the table containing the expected symbol column
        df = self._find_table(tables, cols["symbol"])

        symbols: list[str] = []
        sector_map: dict[str, str] = {}
        has_sector = cols["sector"] in df.columns

        for _, row in df.iterrows():
            raw_symbol = str(row[cols["symbol"]]).strip().upper()
            # Some Wikipedia entries use dots (BRK.B) — Alpaca uses hyphens
            symbol = raw_symbol.replace(".", "-")
            symbols.append(symbol)

            if has_sector:
                sector = str(row[cols["sector"]]).strip()
                if sector and sector != "nan":
                    sector_map[symbol] = sector

        self._symbols = symbols
        self._sector_map = sector_map

        # Write cache
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        cache_data = {
            "index": self._index_name,
            "cached_at": datetime.now(UTC).isoformat(),
            "symbols": symbols,
            "sector_map": sector_map,
        }
        self._cache_path.write_text(json.dumps(cache_data, indent=2))
        logger.info(
            "Cached %d symbols for %s (%d with sector data)",
            len(symbols),
            self._index_name,
            len(sector_map),
        )

    @staticmethod
    def _find_table(tables: Sequence[object], column_name: str) -> pd.DataFrame:
        """Find the DataFrame containing the expected column."""
        import pandas as pd

        for table in tables:
            if not isinstance(table, pd.DataFrame):
                continue
            if column_name in table.columns:
                return table
        raise ConfigError(
            f"Could not find table with column '{column_name}' in Wikipedia page"
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class UniverseRegistry:
    """Creates and caches UniverseProvider instances from config specs."""

    def __init__(
        self,
        specs: Mapping[str, object],
        cache_dir: Path,
    ) -> None:
        self._specs = specs
        self._cache_dir = cache_dir
        self._providers: dict[str, UniverseProvider] = {}

    def get(self, name: str) -> UniverseProvider:
        if name not in self._providers:
            self._providers[name] = self._create(name)
        return self._providers[name]

    def all_providers(self) -> list[UniverseProvider]:
        """Return all providers that have been resolved via get()."""
        return list(self._providers.values())

    def _create(self, name: str) -> UniverseProvider:
        raw = self._specs.get(name)
        if raw is None:
            raise ConfigError(f"Unknown universe provider: {name}")

        from bread.core.config import UniverseProviderSpec

        # Normalize to Pydantic model
        if isinstance(raw, UniverseProviderSpec):
            spec = raw
        elif isinstance(raw, dict):
            spec = UniverseProviderSpec.model_validate(raw)
        else:
            raise ConfigError(f"Invalid provider spec type for '{name}': {type(raw)}")

        if spec.type == "predefined":
            return PredefinedProvider(spec.symbols)
        elif spec.type == "index":
            if not spec.index:
                raise ConfigError(f"Index provider '{name}' requires 'index' field")
            return IndexProvider(spec.index, self._cache_dir, spec.ttl_days)
        else:
            raise ConfigError(f"Unknown provider type: {spec.type}")
