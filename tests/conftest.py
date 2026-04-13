from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bread.core.models import Signal, SignalDirection
from bread.db.database import init_db


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: tests requiring external API keys")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    has_paper_keys = bool(
        os.environ.get("ALPACA_PAPER_API_KEY") and os.environ.get("ALPACA_PAPER_SECRET_KEY")
    )
    if has_paper_keys:
        return
    skip_integration = pytest.mark.skip(reason="Alpaca paper API keys not set")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def paper_config(monkeypatch: pytest.MonkeyPatch):
    """AppConfig loaded in paper mode with fake API keys (no real I/O)."""
    monkeypatch.setenv("ALPACA_PAPER_API_KEY", "fake-paper-key")
    monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "fake-paper-secret")
    from bread.core.config import load_config

    return load_config()


@pytest.fixture
def session_factory(paper_config):
    """In-memory SQLite session factory with all tables created."""
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    sf = sessionmaker(bind=engine)
    yield sf
    engine.dispose()


@pytest.fixture
def db_session(session_factory):
    """Single open session for tests that query/write data directly."""
    with session_factory() as session:
        yield session


@pytest.fixture
def mock_broker():
    """AlpacaBroker mock pre-configured with a healthy paper account."""
    broker = MagicMock()
    broker.get_account.return_value = SimpleNamespace(
        equity="10000",
        buying_power="8000",
        cash="8000",
        last_equity="9900",
    )
    broker.get_positions.return_value = []
    broker.get_orders.return_value = []
    return broker


@pytest.fixture
def sample_signal():
    """Canonical BUY signal for SPY, suitable for most unit tests."""
    return Signal(
        symbol="SPY",
        direction=SignalDirection.BUY,
        strength=0.7,
        stop_loss_pct=0.05,
        strategy_name="test_strategy",
        reason="test signal",
        timestamp=datetime.now(UTC),
    )


@pytest.fixture
def sample_bars():
    """60-row OHLCV DataFrame with a UTC DatetimeIndex (no indicators pre-computed)."""
    n = 60
    dates = [datetime(2025, 1, 2, tzinfo=UTC) + timedelta(days=i) for i in range(n)]
    rng = np.random.default_rng(42)
    close = 500.0 + np.cumsum(rng.normal(0, 2, n))
    return pd.DataFrame(
        {
            "open": close * 0.998,
            "high": close * 1.005,
            "low": close * 0.993,
            "close": close,
            "volume": rng.integers(1_000_000, 5_000_000, n).astype(float),
        },
        index=pd.DatetimeIndex(dates, name="timestamp"),
    )
