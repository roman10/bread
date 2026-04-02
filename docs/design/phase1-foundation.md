# Phase 1: Foundation (Week 1-2)

## Goal

Set up project scaffolding, configuration system, database layer, data pipeline, and technical indicators. This phase produces a working data ingestion pipeline that fetches market data, caches it, and computes indicators — with no trading logic yet.

---

## Scope

### 1.1 Project Scaffolding

- `pyproject.toml` with all dependencies (pin minimum versions for key deps: `sqlalchemy>=2.0`, `pydantic>=2.0`, `pandas-ta>=0.3.14`, `alpaca-py>=0.21`, `holidays>=0.40`, `tenacity>=8.0`, `typer>=0.9`, `pyyaml>=6.0`)
- `.env.example` with required environment variables
- `.gitignore` for Python, secrets, SQLite files
- Directory structure as defined in design.md
- `src/bread/__init__.py`, `__main__.py` (`typer`-based CLI with Phase 1 commands):
  - `bread fetch <SYMBOL>` — fetch bars, cache, compute indicators, print summary (for debugging the pipeline)
  - `bread db init` — create/verify database tables
- **Structured logging** (`core/logging.py`) — configure `logging` module at startup with JSON-formatted output, log level from config. All modules use `logging.getLogger(__name__)`

### 1.2 Configuration (`core/config.py`)

- Pydantic models to validate YAML config at startup
- Load secrets from `.env` via `python-dotenv`
- `mode` field controlling paper/live switching
- App refuses to start if config is invalid
- Config files: `config/default.yaml`, `config/paper.yaml`, `config/live.yaml`
- **Merge strategy:** Deep merge with environment overlay. `default.yaml` is the base; `paper.yaml` or `live.yaml` is overlaid recursively (nested dicts merge, scalars override). Merged dict passed to `AppConfig.model_validate()`. No extra dependency — manual recursive merge (~10 lines) using `pyyaml` which is already required
  ```python
  def deep_merge(base: dict, override: dict) -> dict:
      result = base.copy()
      for k, v in override.items():
          if k in result and isinstance(result[k], dict) and isinstance(v, dict):
              result[k] = deep_merge(result[k], v)
          else:
              result[k] = v
      return result
  ```

### 1.3 Domain Models (`core/models.py`)

- Dataclasses for `Signal` (direction, strength, stop-loss)
- `Order`, `Position`, `PortfolioSnapshot` models
- All models serializable for database storage

### 1.4 Event Bus (`core/events.py`)

- Lightweight in-process event bus (dict of callbacks)
- Subscribe/publish interface
- No external dependencies (no Kafka/Redis)
- _Note: Infrastructure prep only — no modules in Phase 1 publish or subscribe to events. First real usage in Phase 3 (execution engine)_

### 1.5 Exceptions (`core/exceptions.py`)

- Custom exception hierarchy for the application

### 1.6 Database (`db/`)

- SQLAlchemy ORM with SQLite backend
- Tables: `trades`, `orders`, `portfolio_snapshots`, `signals_log`, and `market_data_cache` with schema:
  | Column | Type | Notes |
  |--------|------|-------|
  | `id` | INTEGER PK | Auto-increment |
  | `symbol` | TEXT | e.g., "SPY" |
  | `timeframe` | TEXT | e.g., "1Day" |
  | `date` | DATE | Bar date |
  | `open` | FLOAT | |
  | `high` | FLOAT | |
  | `low` | FLOAT | |
  | `close` | FLOAT | |
  | `volume` | INTEGER | |
  | `fetched_at` | DATETIME | When this row was fetched from API |
  - Unique constraint on `(symbol, timeframe, date)`
- Database initialization via `Base.metadata.create_all()` (Alembic migrations deferred until schema stabilizes)
- `db/database.py` — connection management
- `db/models.py` — ORM models

### 1.7 Data Pipeline (`data/`)

**All data fetching is synchronous** — adequate for a 15-min tick cycle with sequential processing. Async can be revisited if fetch latency becomes a bottleneck.

- **`data/provider.py`** — Abstract data provider interface:
  ```python
  class DataProvider(ABC):
      def get_bars(self, symbol: str, start: date, end: date, timeframe: TimeFrame) -> pd.DataFrame: ...
      def get_latest_bar(self, symbol: str) -> pd.Series: ...
  ```
- **`data/alpaca_data.py`** — Fetch OHLCV bars via `alpaca-py` `StockHistoricalDataClient`. Daily bars with configurable lookback (default 200 days, override via `data.lookback_days` in YAML — use 30 for dev/testing)
- **`data/finnhub_data.py`** — _(Deferred to Phase 2)_ News sentiment, earnings calendar. Not needed until ETF momentum strategy requires the earnings filter
- **`data/cache.py`** — SQLite cache layer using ORM models from `db/models.py`. Refresh if stale relative to **last completed trading day** — e.g., Friday's close data remains fresh through the weekend. Use the `holidays` package (`holidays.NYSE()`) for accurate NYSE holiday detection without heavy dependencies:
  ```python
  import holidays
  _nyse_holidays = holidays.NYSE()

  def is_trading_day(d: date) -> bool:
      return d.weekday() < 5 and d not in _nyse_holidays

  def last_completed_trading_day(as_of: date) -> date:
      d = as_of
      while not is_trading_day(d):
          d -= timedelta(days=1)
      return d
  ```
- **Error handling** — All API calls wrapped with retry logic:
  - Exponential backoff: 3 retries with 1s / 2s / 4s delays
  - On HTTP 429 (rate limit): respect `Retry-After` header, or backoff 60s
  - On network timeout (30s default): retry
  - On empty/invalid response: log warning, return `None` (let caller decide)
  - On auth failure (401/403): raise immediately, do not retry
  - Use `tenacity` library (add to deps) or manual retry loop

### 1.8 Technical Indicators (`data/indicators.py`)

- Compute via `pandas-ta`, configured in YAML:
  - SMA(20, 50, 200), EMA(9, 21)
  - RSI(14), MACD(12, 26, 9)
  - ATR(14), Bollinger Bands(20, 2)
  - Volume SMA(20)
- Returns enriched DataFrame with indicator columns appended to OHLCV data
- **NaN handling:** Trim leading rows where longest-window indicator (SMA 200) is NaN. Return only rows where all indicator columns are populated. Log the number of trimmed rows at DEBUG level

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

All integration tests require Alpaca paper API keys in `.env`. Mark with `@pytest.mark.integration` and skip automatically when keys are absent (`pytest.importorskip` pattern or env check in `conftest.py`). Document env setup in `.env.example`.

1. **Alpaca data fetch** — fetch 30 days of SPY bars from Alpaca paper API; result is a valid DataFrame with OHLCV columns
2. **Full pipeline** — fetch data → cache → compute indicators → verify enriched DataFrame has all expected columns and no NaN values
3. **Cache staleness** — fetch data, verify cache hit on immediate re-fetch, verify refresh after simulated staleness
4. **Retry behavior** — mock a 429 response, verify retry fires and eventually succeeds

### Manual Checks

1. `ruff check src/` — clean (no lint errors)
2. `mypy src/` — clean (no type errors)
3. `python -m bread --help` — CLI displays available commands
4. SQLite database file created at expected path after first run
