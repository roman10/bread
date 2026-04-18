"""Unit tests for database initialization and ORM operations."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from bread.db.database import init_db, migrate_db
from bread.db.models import Base, MarketDataCache


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


class TestMigrateDb:
    def test_adds_raw_filled_price_column_to_existing_table(self) -> None:
        """Simulate a pre-migration DB: create tables without raw_filled_price,
        insert data, then run migrate_db and verify column added + backfill."""
        eng = create_engine("sqlite:///:memory:")

        # Create tables using the OLD schema (without raw_filled_price)
        Base.metadata.create_all(eng)
        with eng.connect() as conn:
            raw = conn.connection
            cur = raw.cursor()
            # Drop the column that create_all added (simulate old schema)
            # SQLite doesn't support DROP COLUMN before 3.35, so recreate
            cols = [
                r[1] for r in cur.execute("PRAGMA table_info(orders)").fetchall()
            ]
            assert "raw_filled_price" in cols  # sanity: create_all adds it

            old_cols = [c for c in cols if c != "raw_filled_price"]
            col_list = ", ".join(old_cols)
            cur.execute(f"CREATE TABLE orders_old AS SELECT {col_list} FROM orders")
            cur.execute("DROP TABLE orders")
            cur.execute("ALTER TABLE orders_old RENAME TO orders")
            raw.commit()

            # Verify column is gone
            cols_after = {
                r[1] for r in cur.execute("PRAGMA table_info(orders)").fetchall()
            }
            assert "raw_filled_price" not in cols_after

            # Insert a filled order (old schema)
            cur.execute(
                "INSERT INTO orders"
                " (symbol, side, qty, status, filled_price, strategy_name,"
                "  reason, created_at_utc)"
                " VALUES ('SPY', 'BUY', 10, 'FILLED', 500.0, 'test',"
                "  'test', '2026-01-01')"
            )
            raw.commit()

        # Run migration
        migrate_db(eng)

        # Verify column exists and backfill worked
        with eng.connect() as conn:
            raw = conn.connection
            cur = raw.cursor()
            cols_final = {
                r[1] for r in cur.execute("PRAGMA table_info(orders)").fetchall()
            }
            assert "raw_filled_price" in cols_final

            row = cur.execute(
                "SELECT raw_filled_price, filled_price FROM orders"
            ).fetchone()
            assert row[0] == 500.0  # backfilled from filled_price
            assert row[1] == 500.0

    def test_migration_is_idempotent(self) -> None:
        """Running migrate_db twice should not error."""
        eng = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(eng)
        migrate_db(eng)  # first run (column already exists from create_all)
        migrate_db(eng)  # second run — should be a no-op

    def test_rewrites_legacy_orderstatus_values(self) -> None:
        """Regression for Bug 1: pre-fix rows stored 'ORDERSTATUS.<X>' in the
        status column. The migration must rewrite them to our canonical
        OrderStatus values and be idempotent on a second run.
        """
        eng = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(eng)

        legacy_rows = [
            ("A1", "SPY", "BUY",  10, "ORDERSTATUS.FILLED"),
            ("A2", "QQQ", "BUY",   5, "ORDERSTATUS.NEW"),
            ("A3", "IWM", "SELL", 10, "ORDERSTATUS.CANCELED"),
            ("A4", "DIA", "BUY",   1, "ORDERSTATUS.REJECTED"),
            ("A5", "XLK", "BUY",   2, "ORDERSTATUS.PARTIALLY_FILLED"),
            ("A6", "XLF", "BUY",   1, "ORDERSTATUS.PENDING_NEW"),
            ("A7", "XLE", "BUY",   1, "ORDERSTATUS.EXPIRED"),
            ("A8", "GLD", "BUY",   1, "ORDERSTATUS.DONE_FOR_DAY"),
        ]
        with eng.connect() as conn:
            raw = conn.connection
            cur = raw.cursor()
            for bid, sym, side, qty, status in legacy_rows:
                cur.execute(
                    "INSERT INTO orders"
                    " (broker_order_id, symbol, side, qty, status,"
                    "  strategy_name, reason, created_at_utc)"
                    " VALUES (?, ?, ?, ?, ?, 'x', 'x', '2026-01-01')",
                    (bid, sym, side, qty, status),
                )
            raw.commit()

        migrate_db(eng)

        with eng.connect() as conn:
            raw = conn.connection
            cur = raw.cursor()
            rows = dict(
                cur.execute("SELECT broker_order_id, status FROM orders").fetchall()
            )
            assert rows == {
                "A1": "FILLED",
                "A2": "PENDING",
                "A3": "CANCELLED",
                "A4": "REJECTED",
                "A5": "ACCEPTED",
                "A6": "PENDING",
                "A7": "CANCELLED",
                "A8": "CANCELLED",
            }
            legacy_left = cur.execute(
                "SELECT COUNT(*) FROM orders WHERE status LIKE 'ORDERSTATUS.%'"
            ).fetchone()[0]
            assert legacy_left == 0

        # Second run: no-op. Values stay canonical.
        migrate_db(eng)
        with eng.connect() as conn:
            raw = conn.connection
            cur = raw.cursor()
            rows2 = dict(
                cur.execute("SELECT broker_order_id, status FROM orders").fetchall()
            )
            assert rows2 == rows
