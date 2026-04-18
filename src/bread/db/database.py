"""Database engine, session factory, and initialization."""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from bread.db.models import Base

logger = logging.getLogger(__name__)


def resolve_db_path(configured_path: str) -> Path:
    """Resolve the database path relative to the project root."""
    path = Path(configured_path)
    if not path.is_absolute():
        project_root = Path(__file__).resolve().parents[3]
        path = project_root / path
    return path


def get_engine(db_path: str) -> Engine:
    resolved = resolve_db_path(db_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{resolved}", echo=False)


def get_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine)


def migrate_db(engine: Engine) -> None:
    """Apply incremental schema and data migrations.

    SQLAlchemy create_all only adds new tables, not new columns or data
    fixes on existing tables.  This function handles column additions and
    idempotent data repairs for running databases.
    """
    with engine.connect() as conn:
        raw_conn = conn.connection
        cursor = raw_conn.cursor()
        columns = {row[1] for row in cursor.execute("PRAGMA table_info(orders)").fetchall()}
        if "raw_filled_price" not in columns:
            cursor.execute("ALTER TABLE orders ADD COLUMN raw_filled_price REAL")
            cursor.execute(
                "UPDATE orders SET raw_filled_price = filled_price"
                " WHERE filled_price IS NOT NULL"
            )
            raw_conn.commit()
            logger.info("Migration: added raw_filled_price column to orders table")

        # Rewrite legacy "ORDERSTATUS.*" status values — written by a broken
        # str(alpaca_enum).upper() path. Idempotent: once rewritten, nothing
        # matches 'ORDERSTATUS.%' and subsequent runs no-op.
        legacy = cursor.execute(
            "SELECT COUNT(*) FROM orders WHERE status LIKE 'ORDERSTATUS.%'"
        ).fetchone()[0]
        if legacy:
            cursor.execute(
                "UPDATE orders SET status = 'FILLED' WHERE status = 'ORDERSTATUS.FILLED'"
            )
            cursor.execute(
                "UPDATE orders SET status = 'PENDING' WHERE status IN"
                " ('ORDERSTATUS.NEW', 'ORDERSTATUS.PENDING_NEW')"
            )
            cursor.execute(
                "UPDATE orders SET status = 'ACCEPTED' WHERE status IN ("
                "'ORDERSTATUS.ACCEPTED', 'ORDERSTATUS.ACCEPTED_FOR_BIDDING',"
                "'ORDERSTATUS.PENDING_CANCEL', 'ORDERSTATUS.PENDING_REPLACE',"
                "'ORDERSTATUS.REPLACED', 'ORDERSTATUS.HELD',"
                "'ORDERSTATUS.SUSPENDED', 'ORDERSTATUS.CALCULATED',"
                "'ORDERSTATUS.PARTIALLY_FILLED')"
            )
            cursor.execute(
                "UPDATE orders SET status = 'CANCELLED' WHERE status IN ("
                "'ORDERSTATUS.CANCELED', 'ORDERSTATUS.EXPIRED',"
                "'ORDERSTATUS.DONE_FOR_DAY', 'ORDERSTATUS.STOPPED')"
            )
            cursor.execute(
                "UPDATE orders SET status = 'REJECTED'"
                " WHERE status = 'ORDERSTATUS.REJECTED'"
            )
            raw_conn.commit()
            logger.info("Migration: rewrote %d legacy ORDERSTATUS.* rows", legacy)

        # Surface the backfill prompt if we have FILLED rows with no fill price.
        # The broken status path above also skipped the fill-capture branch, so
        # legacy FILLED rows have NULL filled_price / filled_at_utc until the
        # `bread repair-orders` command backfills them from Alpaca.
        unpriced = cursor.execute(
            "SELECT COUNT(*) FROM orders WHERE status = 'FILLED' AND filled_price IS NULL"
        ).fetchone()[0]
        if unpriced:
            logger.warning(
                "%d FILLED orders have no filled_price — run"
                " `bread repair-orders` to backfill from Alpaca",
                unpriced,
            )


def init_db(engine: Engine) -> None:
    """Create all tables and run migrations."""
    Base.metadata.create_all(engine)
    migrate_db(engine)
    logger.info("Database tables created")
