# Phase 1: Foundation (Week 1-2)

## Goal

Set up project scaffolding, configuration system, database layer, data pipeline, and technical indicators. This phase produces a working data ingestion pipeline that fetches market data, caches it, and computes indicators — with no trading logic yet.

---

## Scope

### 1.1 Project Scaffolding

- `pyproject.toml` with all dependencies
- `.env.example` with required environment variables
- `.gitignore` for Python, secrets, SQLite files
- Directory structure as defined in design.md
- `src/bread/__init__.py`, `__main__.py` (stub CLI)

### 1.2 Configuration (`core/config.py`)

- Pydantic models to validate YAML config at startup
- Load secrets from `.env` via `python-dotenv`
- `mode` field controlling paper/live switching
- App refuses to start if config is invalid
- Config files: `config/default.yaml`, `config/paper.yaml`, `config/live.yaml`

### 1.3 Domain Models (`core/models.py`)

- Dataclasses for `Signal` (direction, strength, stop-loss)
- `Order`, `Position`, `PortfolioSnapshot` models
- All models serializable for database storage

### 1.4 Event Bus (`core/events.py`)

- Lightweight in-process event bus (dict of callbacks)
- Subscribe/publish interface
- No external dependencies (no Kafka/Redis)

### 1.5 Exceptions (`core/exceptions.py`)

- Custom exception hierarchy for the application

### 1.6 Database (`db/`)

- SQLAlchemy ORM with SQLite backend
- Tables: `trades`, `orders`, `portfolio_snapshots`, `market_data_cache`, `signals_log`
- Database initialization and migration support
- `db/database.py` — connection management
- `db/models.py` — ORM models

### 1.7 Data Pipeline (`data/`)

- **`data/provider.py`** — Abstract data provider interface
- **`data/alpaca_data.py`** — Fetch OHLCV bars via `alpaca-py` `StockHistoricalDataClient`. Daily bars with 200-day lookback
- **`data/finnhub_data.py`** — News sentiment, earnings calendar. Rate-limited to 50 req/min
- **`data/cache.py`** — SQLite cache layer. Refresh if data older than 1 day

### 1.8 Technical Indicators (`data/indicators.py`)

- Compute via `pandas-ta`, configured in YAML:
  - SMA(20, 50, 200), EMA(9, 21)
  - RSI(14), MACD(12, 26, 9)
  - ATR(14), Bollinger Bands(20, 2)
  - Volume SMA(20)
- Returns enriched DataFrame with indicator columns appended to OHLCV data

---

## Verification Criteria

All checks must pass before moving to Phase 2.

### Unit Tests (`pytest tests/unit/`)

1. **Config loading** — valid YAML loads successfully; invalid YAML raises `ValidationError`
2. **Domain models** — `Signal`, `Order`, `Position` instantiate with correct defaults and validate fields
3. **Event bus** — publish fires all subscribers; unsubscribe removes callback; no error on publish with no subscribers
4. **Database** — tables created on init; CRUD operations work for all ORM models
5. **Cache** — data cached on first fetch; cache hit on second fetch; stale data triggers refresh
6. **Indicators** — given a known OHLCV DataFrame, indicator columns are correct (spot-check SMA, RSI, ATR values against manual calculation)

### Integration Tests (`pytest tests/integration/`)

1. **Alpaca data fetch** — fetch 30 days of SPY bars from Alpaca paper API; result is a valid DataFrame with OHLCV columns
2. **Finnhub data fetch** — fetch news for SPY; result contains headline and timestamp fields
3. **Full pipeline** — fetch data → cache → compute indicators → verify enriched DataFrame has all expected columns

### Manual Checks

1. `ruff check src/` — clean (no lint errors)
2. `mypy src/` — clean (no type errors)
3. `python -m bread --help` — CLI displays available commands
4. SQLite database file created at expected path after first run
