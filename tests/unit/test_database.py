"""Unit tests for database initialization and ORM operations."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from bread.db.database import init_db
from bread.db.models import MarketDataCache


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session
    session.close()


@pytest.fixture()
def engine():
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    return engine


class TestDatabaseInit:
    def test_creates_market_data_cache_table(self, engine) -> None:
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        assert "market_data_cache" in tables


class TestMarketDataCacheCRUD:
    def _make_bar(self, **overrides) -> MarketDataCache:
        defaults = {
            "symbol": "SPY",
            "timeframe": "1Day",
            "timestamp_utc": datetime(2025, 1, 2, tzinfo=UTC),
            "open": 100.0,
            "high": 105.0,
            "low": 99.0,
            "close": 103.0,
            "volume": 1_000_000,
            "fetched_at_utc": datetime.now(UTC),
        }
        defaults.update(overrides)
        return MarketDataCache(**defaults)

    def test_insert_and_read(self, db_session: Session) -> None:
        bar = self._make_bar()
        db_session.add(bar)
        db_session.commit()

        result = db_session.query(MarketDataCache).first()
        assert result is not None
        assert result.symbol == "SPY"
        assert result.close == 103.0

    def test_unique_constraint_prevents_duplicates(self, db_session: Session) -> None:
        bar1 = self._make_bar()
        bar2 = self._make_bar()  # same symbol/timeframe/timestamp
        db_session.add(bar1)
        db_session.commit()
        db_session.add(bar2)
        with pytest.raises(IntegrityError):
            db_session.commit()

    def test_different_symbols_allowed(self, db_session: Session) -> None:
        bar1 = self._make_bar(symbol="SPY")
        bar2 = self._make_bar(symbol="QQQ")
        db_session.add_all([bar1, bar2])
        db_session.commit()
        assert db_session.query(MarketDataCache).count() == 2
