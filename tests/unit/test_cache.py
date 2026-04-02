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
from bread.data.cache import BarCache, is_trading_day, last_completed_trading_day
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
