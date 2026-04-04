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
    """Apply incremental schema migrations.

    SQLAlchemy create_all only adds new tables, not new columns on existing
    tables.  This function handles column additions for running databases.
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


def init_db(engine: Engine) -> None:
    """Create all tables and run migrations."""
    Base.metadata.create_all(engine)
    migrate_db(engine)
    logger.info("Database tables created")
