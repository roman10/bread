"""Integration tests requiring Alpaca paper API keys."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from bread.core.config import AppConfig, load_config
from bread.core.exceptions import DataProviderRateLimitError
from bread.data.alpaca_data import AlpacaDataProvider
from bread.data.cache import BarCache
from bread.data.indicators import compute_indicators, get_indicator_columns
from bread.db.database import get_engine, get_session_factory, init_db


@pytest.fixture()
def config() -> AppConfig:
    return load_config()


@pytest.mark.integration
class TestAlpacaFetch:
    def test_fetch_spy_daily_bars(self, config: AppConfig) -> None:
        provider = AlpacaDataProvider(config)
        end = date.today()
        start = end - timedelta(days=90)
        df = provider.get_bars("SPY", start, end, "1Day")

        assert len(df) >= 30
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]
        assert df.index.name == "timestamp"
        assert df.index.tz is not None  # timezone-aware
        assert df.index.is_monotonic_increasing


@pytest.mark.integration
class TestFullPipeline:
    def test_fetch_cache_enrich(self, config: AppConfig, tmp_path) -> None:
        # Use a temp DB
        engine = get_engine(str(tmp_path / "test.db"))
        init_db(engine)
        factory = get_session_factory(engine)

        provider = AlpacaDataProvider(config)
        with factory() as session:
            cache = BarCache(session, provider, config)
            bars = cache.get_bars("SPY")
            assert not bars.empty

            enriched = compute_indicators(bars, config.indicators)
            indicator_cols = get_indicator_columns(config.indicators)
            for col in indicator_cols:
                assert col in enriched.columns
            assert not enriched[indicator_cols].isna().any().any()


@pytest.mark.integration
class TestCacheStaleness:
    def test_second_fetch_is_cache_hit(self, config: AppConfig, tmp_path) -> None:
        engine = get_engine(str(tmp_path / "test.db"))
        init_db(engine)
        factory = get_session_factory(engine)

        provider = AlpacaDataProvider(config)
        # Wrap provider to count calls
        original_get_bars = provider.get_bars
        call_count = {"n": 0}

        def counting_get_bars(*args, **kwargs):
            call_count["n"] += 1
            return original_get_bars(*args, **kwargs)

        provider.get_bars = counting_get_bars  # type: ignore[assignment]

        with factory() as session:
            cache = BarCache(session, provider, config)
            cache.get_bars("SPY")
            cache.get_bars("SPY")

        assert call_count["n"] == 1  # second call was a cache hit


@pytest.mark.integration
class TestRetryBehavior:
    def test_retry_on_429(self, config: AppConfig) -> None:
        provider = AlpacaDataProvider(config)

        call_count = {"n": 0}
        original = provider._client.get_stock_bars

        def mock_429(*args, **kwargs):  # type: ignore[no-untyped-def]
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Raise the typed exception that tenacity is configured to retry
                raise DataProviderRateLimitError("429 Too Many Requests")
            return original(*args, **kwargs)

        provider._client.get_stock_bars = mock_429  # type: ignore[assignment]

        end = date.today()
        start = end - timedelta(days=45)
        # The retry should recover from the first 429
        df = provider.get_bars("SPY", start, end, "1Day")
        assert not df.empty
        assert call_count["n"] >= 2
