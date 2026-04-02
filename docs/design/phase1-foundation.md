# Phase 1: Foundation (Week 1-2)

## Goal

Set up project scaffolding, configuration, database, data pipeline, and technical indicators. This phase produces a working ingestion pipeline that fetches market data, caches raw bars locally, and computes indicators. No strategy, backtest, or trading behavior ships in Phase 1.

---

## Implementation Readiness

**Status:** Ready for implementation after the clarifications in this document.

This phase was previously underspecified in four places that would have caused drift during implementation:

1. Config shape and secret-loading order were not locked.
2. Table schemas were only defined for `market_data_cache`.
3. Cache freshness rules did not account for market-close timing.
4. Error handling said "return `None`", which conflicts with the project's no-silent-failure rule.

The contracts below resolve those gaps and should be treated as the implementation source of truth for Phase 1.

---

## Scope

### 1.1 Project Scaffolding

- Create `pyproject.toml` targeting **Python 3.11+**.
- Runtime dependencies:
  - `sqlalchemy>=2.0`
  - `pydantic>=2.0`
  - `pandas>=2.0`
  - `pandas-ta>=0.3.14`
  - `alpaca-py>=0.21`
  - `holidays>=0.40`
  - `tenacity>=8.0`
  - `typer>=0.9`
  - `pyyaml>=6.0`
  - `python-dotenv>=1.0`
- Dev dependencies:
  - `pytest>=8.0`
  - `ruff>=0.4`
  - `mypy>=1.8`
- Create `.env.example` with all required environment variables.
- Create `.gitignore` entries for Python artifacts, secrets, and SQLite files under `data/`.
- Create the directory structure defined in `docs/design.md`.
- Create `src/bread/__init__.py` and `src/bread/__main__.py`.
- CLI commands required in Phase 1:
  - `bread db init` — create tables and print the resolved SQLite path.
  - `bread fetch <SYMBOL>` — fetch daily bars, cache them, compute indicators, and print a one-line summary for debugging.
- Logging implementation:
  - Add `core/logging.py`.
  - Use the standard `logging` module, not `structlog`, for Phase 1.
  - Emit JSON-formatted logs to stdout.
  - Every module acquires loggers with `logging.getLogger(__name__)`.
  - Log level comes from config.

### 1.2 Configuration (`core/config.py`)

Use Pydantic models to validate the merged config at startup. If validation fails, the app exits with a non-zero status and prints the validation error.

#### Config files

- `config/default.yaml`
- `config/paper.yaml`
- `config/live.yaml`

#### Merge and secret resolution order

`mode` bootstrap rule:

- Default `mode` comes from `config/default.yaml`.
- `BREAD_MODE` may override that value before config merge.
- In Phase 1 there is no CLI `--mode` override for `bread db init` or `bread fetch`.

Merge order:

1. Load `config/default.yaml` and read its `mode`.
2. If `BREAD_MODE` is set, override the base `mode` with that value.
3. Overlay `config/paper.yaml` or `config/live.yaml` based on the resolved bootstrap `mode`.
4. Load `.env`.
5. Inject secrets from environment variables into the final config object.
6. Validate with `AppConfig.model_validate(...)`.

Nested dicts merge recursively; all other values (scalars, lists, `None`) fully replace the base value. A list in an overlay file replaces the entire list from the base, not appended.

```python
def deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result
```

#### Required config model

```python
class AppConfig(BaseModel):
    mode: Literal["paper", "live"]
    app: AppSettings
    db: DatabaseSettings
    data: DataSettings
    alpaca: AlpacaSettings
    indicators: IndicatorSettings
```

#### Required fields

| Section | Field | Type | Default / Rule |
|--------|------|------|----------------|
| `mode` | - | `"paper" \| "live"` | Required |
| `app` | `log_level` | `"DEBUG" \| "INFO" \| "WARNING" \| "ERROR"` | `INFO` |
| `app` | `timezone` | `str` | `"America/New_York"` |
| `db` | `path` | `str` | `"data/bread.db"` |
| `data` | `default_timeframe` | `"1Day"` | `"1Day"` in Phase 1 |
| `data` | `lookback_days` | `int` | `200`, minimum `30` |
| `data` | `request_timeout_seconds` | `int` | `30` |
| `data` | `max_retries` | `int` | `3` |
| `alpaca` | `paper_api_key` | `str` | From `ALPACA_PAPER_API_KEY` |
| `alpaca` | `paper_secret_key` | `str` | From `ALPACA_PAPER_SECRET_KEY` |
| `alpaca` | `live_api_key` | `str` | From `ALPACA_LIVE_API_KEY` |
| `alpaca` | `live_secret_key` | `str` | From `ALPACA_LIVE_SECRET_KEY` |
| `indicators` | `sma_periods` | `list[int]` | `[20, 50, 200]` |
| `indicators` | `ema_periods` | `list[int]` | `[9, 21]` |
| `indicators` | `rsi_period` | `int` | `14` |
| `indicators` | `macd_fast` | `int` | `12` |
| `indicators` | `macd_slow` | `int` | `26` |
| `indicators` | `macd_signal` | `int` | `9` |
| `indicators` | `atr_period` | `int` | `14` |
| `indicators` | `bollinger_period` | `int` | `20` |
| `indicators` | `bollinger_stddev` | `float` | `2.0` |
| `indicators` | `volume_sma_period` | `int` | `20` |

#### Secret rules

- API keys are never stored in YAML.
- In `paper` mode, only paper credentials are required.
- In `live` mode, only live credentials are required.
- `BREAD_MODE` must match `paper` or `live`; any other value is a startup error.
- Missing required credentials raise config validation errors at startup.

### 1.3 Domain Models (`core/models.py`)

Use frozen dataclasses for Phase 1 domain models. All timestamps in Python must be timezone-aware UTC datetimes.

Required models:

- `Signal`
  - `symbol: str`
  - `strategy_name: str`
  - `direction: Literal["buy", "sell"]`
  - `strength: float` in `[0.0, 1.0]`
  - `generated_at: datetime`
  - `stop_loss: float | None`
  - `reason: str | None`
  - `metadata: dict[str, Any]`
- `Order`
  - `symbol: str`
  - `side: Literal["buy", "sell"]`
  - `quantity: float`
  - `order_type: Literal["market", "limit", "stop", "stop_limit"]`
  - `status: str`
  - `submitted_at: datetime`
  - `filled_at: datetime | None`
  - `filled_avg_price: float | None`
- `Position`
  - `symbol: str`
  - `quantity: float`
  - `avg_entry_price: float`
  - `market_value: float`
  - `opened_at: datetime`
- `PortfolioSnapshot`
  - `captured_at: datetime`
  - `equity: float`
  - `cash: float`
  - `buying_power: float`
  - `positions_value: float`

Serialization requirement:

- Provide a single helper that converts these dataclasses into plain dictionaries for persistence and logging.
- Do not add per-model ad hoc serialization code.

### 1.4 Event Bus (`core/events.py`)

Implement a lightweight in-process event bus with this contract:

```python
class EventBus:
    def subscribe(self, event_name: str, callback: Callable[[Any], None]) -> Callable[[], None]: ...
    def publish(self, event_name: str, payload: Any) -> None: ...
```

Behavior rules:

- Subscribers are called synchronously in subscription order.
- `subscribe()` returns an unsubscribe function.
- Publishing to an event with no subscribers is a no-op.
- Callback exceptions are not swallowed; they propagate to the caller.
- No Phase 1 module is required to publish events yet. This is infrastructure only.

### 1.5 Exceptions (`core/exceptions.py`)

Define an explicit exception hierarchy:

- `BreadError` — base application error
- `ConfigError`
- `DatabaseError`
- `DataProviderError`
- `DataProviderAuthError`
- `DataProviderRateLimitError`
- `DataProviderResponseError`
- `CacheError`
- `IndicatorError`
- `InsufficientHistoryError`

Use these instead of raw `ValueError` / `RuntimeError` in application code.

### 1.6 Database (`db/`)

Use SQLAlchemy ORM with SQLite. Database initialization is done with `Base.metadata.create_all()`; Alembic is deferred until schema stabilizes.

#### Database path

- The resolved default database path is `data/bread.db`.
- `bread db init` must create parent directories if they do not exist.

#### Timestamp rule

- Persist timestamps in UTC.
- In SQLAlchemy models, use `DateTime(timezone=True)` for datetime columns.

#### Required tables

##### `market_data_cache`

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | Auto-increment |
| `symbol` | TEXT | e.g. `SPY` |
| `timeframe` | TEXT | `1Day` only in Phase 1 |
| `timestamp_utc` | DATETIME | Bar timestamp in UTC |
| `open` | FLOAT | Required |
| `high` | FLOAT | Required |
| `low` | FLOAT | Required |
| `close` | FLOAT | Required |
| `volume` | INTEGER | Required |
| `fetched_at_utc` | DATETIME | Time fetched from Alpaca |

Constraints:

- Unique constraint on `(symbol, timeframe, timestamp_utc)`
- Index on `(symbol, timeframe, timestamp_utc)`

##### `signals_log`

This table exists in Phase 1 as a forward-compatible skeleton for Phase 2.

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | Auto-increment |
| `created_at_utc` | DATETIME | Required |
| `strategy_name` | TEXT | Required |
| `symbol` | TEXT | Required |
| `direction` | TEXT | Required |
| `strength` | FLOAT | Nullable |
| `stop_loss` | FLOAT | Nullable |
| `reason` | TEXT | Nullable |
| `payload_json` | TEXT | JSON string |

##### `orders`

This table exists in Phase 1 as a forward-compatible skeleton for Phase 3.

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | Auto-increment |
| `created_at_utc` | DATETIME | Required |
| `symbol` | TEXT | Required |
| `side` | TEXT | Required |
| `quantity` | FLOAT | Required |
| `order_type` | TEXT | Required |
| `status` | TEXT | Required |
| `limit_price` | FLOAT | Nullable |
| `stop_price` | FLOAT | Nullable |
| `filled_avg_price` | FLOAT | Nullable |
| `broker_order_id` | TEXT | Nullable, unique when present |
| `payload_json` | TEXT | JSON string |

##### `trades`

This table exists in Phase 1 as a forward-compatible skeleton for Phase 3/4.

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | Auto-increment |
| `opened_at_utc` | DATETIME | Required |
| `closed_at_utc` | DATETIME | Nullable |
| `symbol` | TEXT | Required |
| `side` | TEXT | Required |
| `quantity` | FLOAT | Required |
| `entry_price` | FLOAT | Required |
| `exit_price` | FLOAT | Nullable |
| `pnl_amount` | FLOAT | Nullable |
| `pnl_pct` | FLOAT | Nullable |
| `strategy_name` | TEXT | Nullable |
| `order_id` | INTEGER | Nullable FK to `orders.id` |

##### `portfolio_snapshots`

This table exists in Phase 1 as a forward-compatible skeleton for Phase 4.

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | Auto-increment |
| `captured_at_utc` | DATETIME | Required |
| `equity` | FLOAT | Required |
| `cash` | FLOAT | Required |
| `buying_power` | FLOAT | Required |
| `positions_value` | FLOAT | Required |
| `day_pnl` | FLOAT | Nullable |

Required modules:

- `db/database.py` — engine, session factory, path resolution
- `db/models.py` — ORM models

### 1.7 Data Pipeline (`data/`)

All Phase 1 data fetching is synchronous. That is sufficient for daily bars and avoids unnecessary complexity.

#### Provider contract (`data/provider.py`)

```python
class DataProvider(ABC):
    def get_bars(
        self,
        symbol: str,
        start: date,
        end: date,
        timeframe: str,
    ) -> pd.DataFrame: ...

    def get_latest_bar(self, symbol: str, timeframe: str = "1Day") -> pd.Series: ...
```

DataFrame contract for `get_bars()`:

- Sorted ascending by timestamp
- Timezone-aware UTC `DatetimeIndex` named `timestamp`
- Required columns exactly: `open`, `high`, `low`, `close`, `volume`
- No duplicate timestamps

#### Alpaca implementation (`data/alpaca_data.py`)

- Use `alpaca-py` `StockHistoricalDataClient`.
- Phase 1 supports only `1Day` bars.
- Default lookback is `data.lookback_days` from config.
- Normalize Alpaca responses into the provider contract exactly.
- Convert symbol input to uppercase before requests and cache keys.

#### Finnhub (`data/finnhub_data.py`)

- Deferred to Phase 2.
- File may exist as a placeholder, but no functional implementation is required in Phase 1.

#### Cache behavior (`data/cache.py`)

Cache only **raw OHLCV bars**. Indicator-enriched DataFrames are computed in memory and are not persisted in Phase 1.

Freshness rule:

- Cache freshness is evaluated against the **last completed NYSE trading day**, not simply `today`.
- Convert `as_of` to `America/New_York`.
- If the local time is **4:00 PM ET or later** on a trading day, that day counts as completed.
- Otherwise, walk backward to the most recent prior trading day.
- Use `holidays.NYSE()` for holiday detection.

Reference implementation:

```python
import holidays
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

_nyse_holidays = holidays.NYSE()
_et = ZoneInfo("America/New_York")

def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in _nyse_holidays

def last_completed_trading_day(as_of_utc: datetime) -> date:
    local_dt = as_of_utc.astimezone(_et)
    candidate = local_dt.date()

    if not (is_trading_day(candidate) and local_dt.time() >= time(16, 0)):
        candidate -= timedelta(days=1)

    while not is_trading_day(candidate):
        candidate -= timedelta(days=1)

    return candidate
```

Refresh behavior:

- On cache miss: fetch full configured lookback and upsert into `market_data_cache`.
- On stale cache: fetch the full configured lookback again and upsert; do not try to patch only the missing tail in Phase 1.
- Upsert mechanism: use SQLite `INSERT ... ON CONFLICT(symbol, timeframe, timestamp_utc) DO UPDATE SET` to update OHLCV and `fetched_at_utc` columns. Use SQLAlchemy's `sqlite.insert(...).on_conflict_do_update(...)`.
- After fetch, load the requested range from SQLite and return it sorted ascending.

#### Retry and failure handling

Use `tenacity`.

- Retry on network timeouts and transient 5xx responses.
- Retry on HTTP 429 and respect `Retry-After` when present; otherwise sleep 60 seconds before the next attempt.
- Use exponential backoff for normal transient failures: 1s, 2s, 4s.
- Do **not** retry on 401/403.
- Do **not** return `None` on failures.
- Empty or malformed provider responses raise `DataProviderResponseError`.
- Authentication failures raise `DataProviderAuthError`.
- Exhausted retries raise `DataProviderError`.

### 1.8 Technical Indicators (`data/indicators.py`)

Compute indicators with `pandas-ta` using config values from `AppConfig.indicators`.

Column naming convention — names are derived dynamically from config values:

- `sma_{period}` for each period in `sma_periods` → e.g. `sma_20`, `sma_50`, `sma_200`
- `ema_{period}` for each period in `ema_periods` → e.g. `ema_9`, `ema_21`
- `rsi_{rsi_period}` → e.g. `rsi_14`
- `macd`, `macd_signal`, `macd_hist` (fixed names, parameters from `macd_fast`/`macd_slow`/`macd_signal`)
- `atr_{atr_period}` → e.g. `atr_14`
- `bb_lower_{bollinger_period}_{bollinger_stddev}`, `bb_mid_...`, `bb_upper_...` → e.g. `bb_lower_20_2.0`, `bb_mid_20_2.0`, `bb_upper_20_2.0`
- `volume_sma_{volume_sma_period}` → e.g. `volume_sma_20`

With default config, the expected output columns are:

- `sma_20`, `sma_50`, `sma_200`
- `ema_9`, `ema_21`
- `rsi_14`
- `macd`, `macd_signal`, `macd_hist`
- `atr_14`
- `bb_lower_20_2.0`, `bb_mid_20_2.0`, `bb_upper_20_2.0`
- `volume_sma_20`

Behavior rules:

- Return the original OHLCV columns plus the indicator columns above.
- If the input does not contain enough rows to compute the longest configured window, raise `InsufficientHistoryError`.
- Trim leading rows where any required indicator column is null.
- Log the number of trimmed rows at `DEBUG`.
- The returned DataFrame must contain no nulls in any required indicator column.

### 1.9 CLI Output Contract

#### `bread db init`

- Creates the SQLite database and all Phase 1 tables.
- Prints: `Initialized database at <resolved-path>`
- Exit code `0` on success, non-zero on failure.

#### `bread fetch <SYMBOL>`

Behavior:

1. Load config.
2. Initialize logging.
3. Auto-initialize DB tables if the database does not yet exist (same logic as `bread db init`). This makes `bread db init` a convenience command, not a prerequisite.
4. Fetch and cache raw bars.
5. Compute indicators.
6. Print one summary line.

Required summary format:

```text
SYMBOL=<symbol> bars=<count> start=<yyyy-mm-dd> end=<yyyy-mm-dd> indicators=<count>
```

Example:

```text
SYMBOL=SPY bars=201 start=2025-01-02 end=2025-10-20 indicators=14
```

This output is intentionally compact so tests can assert it easily.

---

## Verification Criteria

All checks must pass before Phase 2 starts.

### Unit Tests (`pytest tests/unit/`)

1. **Config loading**
   - Valid YAML + env secrets load successfully.
   - Invalid YAML raises a Pydantic validation error.
   - `paper` mode does not require live credentials.
   - `live` mode does not require paper credentials.
2. **Domain models**
   - `Signal`, `Order`, `Position`, and `PortfolioSnapshot` instantiate with valid data.
   - Invalid `Signal.strength` values are rejected.
3. **Event bus**
   - Publish invokes all subscribers in registration order.
   - Unsubscribe removes the callback.
   - Publish with no subscribers does not error.
   - Subscriber exceptions propagate.
4. **Database**
   - `bread db init` creates all five tables.
   - CRUD operations succeed for every ORM model.
   - `market_data_cache` unique constraint prevents duplicate bar rows.
5. **Cache**
   - First fetch populates cache.
   - Second fetch on the same day is a cache hit.
   - Stale cache triggers refresh based on last completed trading day logic.
6. **Indicators**
   - Given a known OHLCV DataFrame, indicator columns are present and numerically correct for spot-checked SMA, RSI, and ATR values.
   - Returned DataFrame contains no null indicator values.
   - Insufficient input history raises `InsufficientHistoryError`.

### Integration Tests (`pytest tests/integration/`)

Integration tests require Alpaca paper API keys in `.env`. Mark them with `@pytest.mark.integration` and skip automatically when the keys are absent. Document the required env vars in `.env.example`.

1. **Alpaca fetch**
   - Fetch 30+ days of `SPY` daily bars from Alpaca paper credentials.
   - Returned DataFrame matches the provider contract.
2. **Full pipeline**
   - Run `bread fetch SPY`.
   - Data is fetched, cached, enriched, and the summary line is printed.
3. **Cache staleness**
   - Fetch once, re-fetch immediately, then simulate stale time and verify refresh.
4. **Retry behavior**
   - Mock a 429 response and verify the retry path is exercised.

### Manual Checks

1. `ruff check src/` — clean
2. `mypy src/` — clean
3. `python -m bread --help` — CLI shows `fetch` and `db init`
4. `python -m bread db init` — creates `data/bread.db`
5. `python -m bread fetch SPY` — prints the required summary format
