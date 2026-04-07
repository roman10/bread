"""Unit tests for the bar cache."""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from bread.core.config import AppConfig
from bread.data.cache import (
    BarCache,
    CachingDataProvider,
    is_market_open,
    is_trading_day,
    last_completed_trading_day,
)
from bread.db.database import init_db


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session
    session.close()


@pytest.fixture()
def paper_config(monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    monkeypatch.delenv("BREAD_MODE", raising=False)
    return AppConfig(
        mode="paper",
        alpaca={"paper_api_key": "pk", "paper_secret_key": "sk"},
    )


def _make_ohlcv_df(start: date, days: int) -> pd.DataFrame:
    """Create a fake OHLCV DataFrame matching the provider contract."""
    dates = pd.bdate_range(start=start, periods=days, tz="UTC")
    return pd.DataFrame(
        {
            "open": [100.0] * days,
            "high": [105.0] * days,
            "low": [99.0] * days,
            "close": [103.0] * days,
            "volume": [1_000_000] * days,
        },
        index=pd.DatetimeIndex(dates, name="timestamp"),
    )


class TestTradingDayHelpers:
    def test_weekday_is_trading_day(self) -> None:
        # 2025-01-06 is a Monday
        assert is_trading_day(date(2025, 1, 6)) is True

    def test_weekend_is_not_trading_day(self) -> None:
        # 2025-01-04 is Saturday
        assert is_trading_day(date(2025, 1, 4)) is False

    def test_holiday_is_not_trading_day(self) -> None:
        # 2025-01-01 is New Year's Day
        assert is_trading_day(date(2025, 1, 1)) is False

    def test_last_completed_after_close(self) -> None:
        # Monday at 4:30 PM ET = today
        et = ZoneInfo("America/New_York")
        dt = datetime(2025, 1, 6, 16, 30, tzinfo=et).astimezone(UTC)
        assert last_completed_trading_day(dt) == date(2025, 1, 6)

    def test_last_completed_before_close(self) -> None:
        # Monday at 2:00 PM ET = previous Friday
        et = ZoneInfo("America/New_York")
        dt = datetime(2025, 1, 6, 14, 0, tzinfo=et).astimezone(UTC)
        assert last_completed_trading_day(dt) == date(2025, 1, 3)

    def test_last_completed_on_weekend(self) -> None:
        # Saturday -> previous Friday
        et = ZoneInfo("America/New_York")
        dt = datetime(2025, 1, 4, 12, 0, tzinfo=et).astimezone(UTC)
        assert last_completed_trading_day(dt) == date(2025, 1, 3)


class TestIsMarketOpen:
    _et = ZoneInfo("America/New_York")

    def test_open_during_hours(self) -> None:
        # Wednesday 10:00 AM ET
        dt = datetime(2025, 1, 8, 10, 0, tzinfo=self._et)
        assert is_market_open(dt) is True

    def test_closed_before_open(self) -> None:
        # Wednesday 9:00 AM ET
        dt = datetime(2025, 1, 8, 9, 0, tzinfo=self._et)
        assert is_market_open(dt) is False

    def test_open_at_open(self) -> None:
        # Wednesday 9:30 AM ET exactly
        dt = datetime(2025, 1, 8, 9, 30, tzinfo=self._et)
        assert is_market_open(dt) is True

    def test_closed_at_close(self) -> None:
        # Wednesday 4:00 PM ET exactly (market closes at 16:00)
        dt = datetime(2025, 1, 8, 16, 0, tzinfo=self._et)
        assert is_market_open(dt) is False

    def test_closed_on_weekend(self) -> None:
        # Saturday 11:00 AM ET
        dt = datetime(2025, 1, 4, 11, 0, tzinfo=self._et)
        assert is_market_open(dt) is False

    def test_closed_on_holiday(self) -> None:
        # New Year's Day 2025 (Wednesday) at 11:00 AM ET
        dt = datetime(2025, 1, 1, 11, 0, tzinfo=self._et)
        assert is_market_open(dt) is False


class TestBarCache:
    def test_first_fetch_populates_cache(
        self, db_session: Session, paper_config: AppConfig
    ) -> None:
        provider = MagicMock()
        provider.get_bars.return_value = _make_ohlcv_df(date(2025, 1, 2), 30)

        cache = BarCache(db_session, provider, paper_config)
        # Use a fixed as_of after market close on a known trading day
        et = ZoneInfo("America/New_York")
        as_of = datetime(2025, 2, 14, 17, 0, tzinfo=et).astimezone(UTC)
        result = cache.get_bars("SPY", as_of_utc=as_of)

        assert not result.empty
        provider.get_bars.assert_called_once()

    def test_second_fetch_is_cache_hit(
        self, db_session: Session, paper_config: AppConfig
    ) -> None:
        provider = MagicMock()
        # 35 bdays from Jan 2 covers through ~Feb 19
        provider.get_bars.return_value = _make_ohlcv_df(date(2025, 1, 2), 35)

        cache = BarCache(db_session, provider, paper_config)
        et = ZoneInfo("America/New_York")
        as_of = datetime(2025, 2, 14, 17, 0, tzinfo=et).astimezone(UTC)

        cache.get_bars("SPY", as_of_utc=as_of)
        cache.get_bars("SPY", as_of_utc=as_of)

        # Provider called only once — second call was a cache hit
        assert provider.get_bars.call_count == 1

    def test_stale_cache_triggers_refresh(
        self, db_session: Session, paper_config: AppConfig
    ) -> None:
        provider = MagicMock()
        provider.get_bars.return_value = _make_ohlcv_df(date(2025, 1, 2), 30)

        cache = BarCache(db_session, provider, paper_config)
        et = ZoneInfo("America/New_York")

        # First fetch: Friday after close
        as_of_1 = datetime(2025, 2, 14, 17, 0, tzinfo=et).astimezone(UTC)
        cache.get_bars("SPY", as_of_utc=as_of_1)

        # Second fetch: next Monday after close (new trading day, cache is stale)
        provider.get_bars.return_value = _make_ohlcv_df(date(2025, 1, 2), 31)
        as_of_2 = datetime(2025, 2, 18, 17, 0, tzinfo=et).astimezone(UTC)
        cache.get_bars("SPY", as_of_utc=as_of_2)

        assert provider.get_bars.call_count == 2

    def test_refresh_requests_enough_history_for_indicators(
        self, db_session: Session, paper_config: AppConfig
    ) -> None:
        """Regression: _refresh must fetch lookback_days + indicator warmup."""
        provider = MagicMock()
        provider.get_bars.return_value = _make_ohlcv_df(date(2024, 1, 2), 400)

        cache = BarCache(db_session, provider, paper_config)
        et = ZoneInfo("America/New_York")
        as_of = datetime(2025, 2, 14, 17, 0, tzinfo=et).astimezone(UTC)
        cache.get_bars("SPY", as_of_utc=as_of)

        call_args = provider.get_bars.call_args
        start_date = call_args[0][1]  # second positional arg
        end_date = call_args[0][2]  # third positional arg
        calendar_days_requested = (end_date - start_date).days

        # With default config (SMA-200, lookback_days=200), we need at least
        # (200 + 200) trading days ≈ 570+ calendar days, not just 300.
        assert calendar_days_requested >= 500


class TestBarCacheBatch:
    def test_batch_fetch_all_stale(
        self, db_session: Session, paper_config: AppConfig
    ) -> None:
        """First batch call should trigger a single batch API request."""
        provider = MagicMock()
        spy_df = _make_ohlcv_df(date(2025, 1, 2), 30)
        qqq_df = _make_ohlcv_df(date(2025, 1, 2), 30)
        provider.get_bars_batch.return_value = {"SPY": spy_df, "QQQ": qqq_df}

        cache = BarCache(db_session, provider, paper_config)
        et = ZoneInfo("America/New_York")
        as_of = datetime(2025, 2, 14, 17, 0, tzinfo=et).astimezone(UTC)

        result = cache.get_bars_batch(["SPY", "QQQ"], as_of_utc=as_of)

        assert "SPY" in result
        assert "QQQ" in result
        provider.get_bars_batch.assert_called_once()
        provider.get_bars.assert_not_called()

    def test_batch_fetch_all_fresh(
        self, db_session: Session, paper_config: AppConfig
    ) -> None:
        """If all symbols are cached, no API call should be made."""
        provider = MagicMock()
        spy_df = _make_ohlcv_df(date(2025, 1, 2), 35)
        qqq_df = _make_ohlcv_df(date(2025, 1, 2), 35)
        provider.get_bars_batch.return_value = {"SPY": spy_df, "QQQ": qqq_df}

        cache = BarCache(db_session, provider, paper_config)
        et = ZoneInfo("America/New_York")
        as_of = datetime(2025, 2, 14, 17, 0, tzinfo=et).astimezone(UTC)

        # First call populates cache
        cache.get_bars_batch(["SPY", "QQQ"], as_of_utc=as_of)
        provider.get_bars_batch.reset_mock()

        # Second call at same as_of should be all cache hits
        result = cache.get_bars_batch(["SPY", "QQQ"], as_of_utc=as_of)

        assert "SPY" in result
        assert "QQQ" in result
        provider.get_bars_batch.assert_not_called()

    def test_batch_fetch_mixed_stale_fresh(
        self, db_session: Session, paper_config: AppConfig
    ) -> None:
        """Only stale symbols should be fetched in the batch call."""
        provider = MagicMock()
        spy_df = _make_ohlcv_df(date(2025, 1, 2), 35)
        qqq_df = _make_ohlcv_df(date(2025, 1, 2), 35)
        provider.get_bars_batch.return_value = {"SPY": spy_df, "QQQ": qqq_df}

        cache = BarCache(db_session, provider, paper_config)
        et = ZoneInfo("America/New_York")
        as_of_fri = datetime(2025, 2, 14, 17, 0, tzinfo=et).astimezone(UTC)

        # Pre-populate only SPY
        provider.get_bars.return_value = spy_df
        cache.get_bars("SPY", as_of_utc=as_of_fri)
        provider.get_bars_batch.reset_mock()

        # Batch fetch both — SPY is fresh, QQQ is stale
        provider.get_bars_batch.return_value = {"QQQ": qqq_df}
        result = cache.get_bars_batch(["SPY", "QQQ"], as_of_utc=as_of_fri)

        assert "SPY" in result
        assert "QQQ" in result
        # Batch should only include the stale symbol
        call_args = provider.get_bars_batch.call_args
        assert call_args[0][0] == ["QQQ"]

    def test_batch_partial_failure(
        self, db_session: Session, paper_config: AppConfig
    ) -> None:
        """If a symbol is missing from batch response, it's omitted from result."""
        provider = MagicMock()
        spy_df = _make_ohlcv_df(date(2025, 1, 2), 30)
        # Batch returns only SPY, not MISSING
        provider.get_bars_batch.return_value = {"SPY": spy_df}

        cache = BarCache(db_session, provider, paper_config)
        et = ZoneInfo("America/New_York")
        as_of = datetime(2025, 2, 14, 17, 0, tzinfo=et).astimezone(UTC)

        result = cache.get_bars_batch(["SPY", "MISSING"], as_of_utc=as_of)

        assert "SPY" in result
        assert "MISSING" not in result


class TestCachingDataProvider:
    """Tests for CachingDataProvider (backtest caching)."""

    def test_first_call_fetches_from_api(self, db_session: Session) -> None:
        provider = MagicMock()
        provider.get_bars.return_value = _make_ohlcv_df(date(2024, 1, 2), 80)

        cached = CachingDataProvider(provider, db_session)
        result = cached.get_bars("SPY", date(2024, 1, 2), date(2024, 4, 19), "1Day")

        assert not result.empty
        provider.get_bars.assert_called_once()

    def test_second_call_is_cache_hit(self, db_session: Session) -> None:
        provider = MagicMock()
        # 80 bdays from Jan 2 covers through ~Apr 22
        provider.get_bars.return_value = _make_ohlcv_df(date(2024, 1, 2), 80)

        cached = CachingDataProvider(provider, db_session)
        cached.get_bars("SPY", date(2024, 1, 2), date(2024, 4, 19), "1Day")
        cached.get_bars("SPY", date(2024, 1, 2), date(2024, 4, 19), "1Day")

        # Provider called only once — second call was cached
        assert provider.get_bars.call_count == 1

    def test_narrower_range_hits_cache(self, db_session: Session) -> None:
        """A request for a subset of cached data should not re-fetch."""
        provider = MagicMock()
        provider.get_bars.return_value = _make_ohlcv_df(date(2024, 1, 2), 80)

        cached = CachingDataProvider(provider, db_session)
        cached.get_bars("SPY", date(2024, 1, 2), date(2024, 4, 19), "1Day")

        # Request a subset — should be a cache hit
        result = cached.get_bars("SPY", date(2024, 2, 1), date(2024, 3, 29), "1Day")

        assert not result.empty
        assert provider.get_bars.call_count == 1

    def test_wider_range_triggers_refetch(self, db_session: Session) -> None:
        """A request extending beyond cached data should re-fetch."""
        provider = MagicMock()
        provider.get_bars.return_value = _make_ohlcv_df(date(2024, 6, 3), 50)

        cached = CachingDataProvider(provider, db_session)
        cached.get_bars("SPY", date(2024, 6, 3), date(2024, 8, 9), "1Day")

        # Request earlier start — cache miss
        provider.get_bars.return_value = _make_ohlcv_df(date(2024, 1, 2), 160)
        cached.get_bars("SPY", date(2024, 1, 2), date(2024, 8, 9), "1Day")

        assert provider.get_bars.call_count == 2

    def test_batch_caches_all_symbols(self, db_session: Session) -> None:
        provider = MagicMock()
        spy_df = _make_ohlcv_df(date(2024, 1, 2), 80)
        qqq_df = _make_ohlcv_df(date(2024, 1, 2), 80)
        provider.get_bars_batch.return_value = {"SPY": spy_df, "QQQ": qqq_df}

        cached = CachingDataProvider(provider, db_session)
        cached.get_bars_batch(
            ["SPY", "QQQ"], date(2024, 1, 2), date(2024, 4, 19), "1Day",
        )
        provider.get_bars_batch.reset_mock()

        # Second call — all cached
        result = cached.get_bars_batch(
            ["SPY", "QQQ"], date(2024, 1, 2), date(2024, 4, 19), "1Day",
        )

        assert "SPY" in result
        assert "QQQ" in result
        provider.get_bars_batch.assert_not_called()

    def test_batch_mixed_cached_and_missing(self, db_session: Session) -> None:
        provider = MagicMock()
        spy_df = _make_ohlcv_df(date(2024, 1, 2), 80)

        # Pre-populate SPY only
        cached = CachingDataProvider(provider, db_session)
        provider.get_bars.return_value = spy_df
        cached.get_bars("SPY", date(2024, 1, 2), date(2024, 4, 19), "1Day")

        # Batch fetch both — SPY cached, QQQ needs API
        qqq_df = _make_ohlcv_df(date(2024, 1, 2), 80)
        provider.get_bars_batch.return_value = {"QQQ": qqq_df}
        result = cached.get_bars_batch(
            ["SPY", "QQQ"], date(2024, 1, 2), date(2024, 4, 19), "1Day",
        )

        assert "SPY" in result
        assert "QQQ" in result
        # Only QQQ should be fetched
        call_args = provider.get_bars_batch.call_args
        assert call_args[0][0] == ["QQQ"]

    def test_weekend_start_date_hits_cache(self, db_session: Session) -> None:
        """Start date on weekend should still hit cache with Monday data."""
        provider = MagicMock()
        # Data starts Monday 2024-01-02, 80 bdays covers through ~Apr 22
        provider.get_bars.return_value = _make_ohlcv_df(date(2024, 1, 2), 80)

        cached = CachingDataProvider(provider, db_session)
        cached.get_bars("SPY", date(2024, 1, 2), date(2024, 4, 19), "1Day")

        # Request with Sunday start (2023-12-31) — cache has Mon Jan 2 data
        result = cached.get_bars("SPY", date(2023, 12, 31), date(2024, 4, 19), "1Day")

        # Should still be a cache hit since Dec 31 2023 was a Sunday,
        # and Jan 1 2024 was a holiday — first trading day is Jan 2
        assert provider.get_bars.call_count == 1
