# Phase 2: Strategy + Backtest (Week 3)

## Goal

Build the strategy framework, implement the first strategy (ETF Momentum), and create the backtest engine. This phase produces a system that can evaluate historical performance of strategies — with no live/paper execution yet.

---

## Implementation Readiness

**Status:** Ready for implementation.

Scope has been trimmed to only what Phase 2 functionally needs. Deferred to their owning phases:

- **Event bus** (`core/events.py`) → Phase 3
- **Finnhub data provider** → Phase 3 (earnings check replaced with a no-op stub in Phase 2)
- **`get_latest_bar()`** on DataProvider → Phase 3
- **Execution-related DB tables** (`orders`, `trades`) → Phase 3
- **Portfolio snapshots table** → Phase 4

The contracts below are the implementation source of truth for Phase 2. They include the naming, path-resolution, indicator-dependency, and execution-order rules needed to implement Phase 2 without guessing.

---

## Scope

### 2.1 Domain Models (`core/models.py`)

Define dataclasses for strategy output. These were deferred from Phase 1.

```python
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class SignalDirection(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class Signal:
    symbol: str
    direction: SignalDirection
    strength: float          # 0.0 to 1.0
    stop_loss_pct: float     # e.g. 0.05 for 5%
    strategy_name: str
    reason: str              # human-readable explanation
    timestamp: datetime      # when the signal was generated
```

Rules:
- `strength` must be in `[0.0, 1.0]`. Values outside this range are a bug in the strategy.
- `stop_loss_pct` is always positive, representing percentage distance from entry price.
- `reason` is a short string explaining which conditions triggered (for trade journal / debugging).
- `frozen=True` — signals are immutable once created.
- Additional models (`Order`, `Position`, `PortfolioSnapshot`) are deferred to Phase 3 where the consuming code exists.

### 2.2 New Exceptions (`core/exceptions.py`)

Add to the existing exception hierarchy:

```python
class StrategyError(BreadError):
    """Strategy evaluation error."""

class BacktestError(BreadError):
    """Backtest engine error."""
```

### 2.3 Configuration Additions

#### `config/strategies/etf_momentum.yaml`

```yaml
universe:
  - SPY
  - QQQ
  - IWM
  - DIA
  - XLF
  - XLK
  - XLE
  - XLV
  - GLD
  - TLT

entry:
  sma_long: 200          # price > SMA(sma_long) for uptrend
  rsi_period: 14
  rsi_oversold: 30       # RSI bounce threshold
  sma_fast: 20           # SMA(sma_fast) > SMA(sma_mid)
  sma_mid: 50
  volume_sma_period: 20
  volume_mult: 1.0       # must be >= 1.0; volume > volume_mult * volume_sma

exit:
  rsi_overbought: 70
  atr_stop_mult: 1.5     # stop loss = atr_stop_mult * ATR
  time_stop_days: 15     # close after N trading days
```

#### Pydantic config models (`core/config.py` additions)

```python
class StrategySettings(BaseModel):
    name: str  # canonical snake_case identifier used by config, CLI, registry, and Signal.strategy_name
    config_path: str  # relative to config/

class BacktestSettings(BaseModel):
    initial_capital: float = Field(default=10000.0, gt=0)
    commission_per_trade: float = Field(default=0.0, ge=0)  # Alpaca is commission-free
    slippage_pct: float = Field(default=0.001, ge=0)        # 0.1% default slippage estimate

class AppConfig(BaseModel):
    # ... existing fields ...
    strategies: list[StrategySettings] = Field(default_factory=list)
    backtest: BacktestSettings = Field(default_factory=BacktestSettings)

    @model_validator(mode="after")
    def _unique_strategy_names(self) -> AppConfig:
        names = [s.name for s in self.strategies]
        if len(names) != len(set(names)):
            dupes = {n for n in names if names.count(n) > 1}
            raise ValueError(f"Duplicate strategy names: {dupes}")
        return self
```

#### `config/default.yaml` additions

```yaml
strategies:
  - name: etf_momentum
    config_path: strategies/etf_momentum.yaml

backtest:
  initial_capital: 10000.0
  commission_per_trade: 0.0
  slippage_pct: 0.001
```

#### Strategy config loading

Strategy YAML files are loaded on demand by the strategy implementation, not by `load_config()`. `load_config()` validates only the `StrategySettings` metadata.

Phase 2 exports the config directory path for strategy config resolution:

```python
# Add to core/config.py (rename existing _CONFIG_DIR)
CONFIG_DIR: Path = Path(__file__).resolve().parents[3] / "config"
```

Rules:
- `StrategySettings.name` is the canonical strategy identifier. The same exact lowercase snake_case value is used in config, the CLI `--strategy` flag, the registry key, and `Signal.strategy_name`.
- Strategy names must be unique within `AppConfig.strategies` (enforced by the `_unique_strategy_names` validator above).
- `StrategySettings.config_path` is resolved relative to `CONFIG_DIR`.
- The CLI resolves `config_path` to an absolute `Path` via `CONFIG_DIR / strategy_settings.config_path` before instantiating the strategy.
- Phase 2 creates `config/strategies/etf_momentum.yaml` as part of the implementation.

### 2.4 Strategy Framework

#### Abstract interface (`strategy/base.py`)

```python
from abc import ABC, abstractmethod
import pandas as pd
from bread.core.models import Signal


class Strategy(ABC):
    @abstractmethod
    def evaluate(self, universe: dict[str, pd.DataFrame]) -> list[Signal]:
        """Evaluate the strategy on enriched OHLCV+indicator DataFrames.

        Args:
            universe: mapping of symbol → DataFrame with OHLCV + indicator columns.
                      Each DataFrame has a UTC DatetimeIndex named 'timestamp',
                      sorted ascending, with indicator columns from compute_indicators().

        Returns:
            List of Signal objects. May be empty if no conditions are met.
        """
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique strategy identifier (e.g. 'etf_momentum')."""
        ...

    @property
    @abstractmethod
    def universe(self) -> list[str]:
        """List of symbols this strategy trades."""
        ...

    @property
    @abstractmethod
    def min_history_days(self) -> int:
        """Minimum number of trading days of history required for evaluation."""
        ...

    @property
    @abstractmethod
    def time_stop_days(self) -> int:
        """Number of trading bars to hold before a time-stop exit."""
        ...
```

Rules:
- `evaluate()` receives DataFrames that already have indicator columns appended (output of `compute_indicators()`).
- `evaluate()` must not modify the input DataFrames.
- `evaluate()` must only read data up to the last row of the DataFrame. The backtest engine controls the time boundary by slicing before calling `evaluate()`.
- If `evaluate()` encounters an error, it raises `StrategyError`. It never returns partial results silently.

#### Strategy registry (`strategy/registry.py`)

```python
_REGISTRY: dict[str, type[Strategy]] = {}


def register(name: str):
    """Class decorator factory to register a strategy under its canonical identifier."""

    def decorator(cls: type[Strategy]) -> type[Strategy]:
        if name in _REGISTRY:
            raise ValueError(f"Strategy already registered: {name}")
        _REGISTRY[name] = cls
        return cls

    return decorator


def get_strategy(name: str) -> type[Strategy]:
    """Look up a registered strategy class by name. Raises KeyError if not found."""
    if name not in _REGISTRY:
        raise KeyError(f"Unknown strategy: {name}. Available: {list(_REGISTRY.keys())}")
    return _REGISTRY[name]


def list_strategies() -> list[str]:
    """Return names of all registered strategies."""
    return list(_REGISTRY.keys())
```

Rules:
- Registration happens at import time via `@register("<strategy_name>")`.
- The registry key is the canonical strategy identifier from config and CLI (for example `etf_momentum`), not the Python class name.
- `strategy/__init__.py` must import all strategy modules to trigger registration:
  ```python
  from . import etf_momentum  # noqa: F401
  ```
- Adding a new strategy: (1) create file, (2) decorate with `@register("<strategy_name>")`, (3) import in `__init__.py`, (4) add YAML config. No changes to framework code.

### 2.5 ETF Momentum Strategy (`strategy/etf_momentum.py`)

#### Constructor

```python
@register("etf_momentum")
class EtfMomentum(Strategy):
    def __init__(self, config_path: Path, indicator_settings: IndicatorSettings) -> None:
        """Load strategy-specific config from YAML."""
        ...
```

The constructor loads the YAML from the provided absolute `config_path` and stores the parsed parameters. If the file is missing or invalid, raise `StrategyError`.

The constructor also validates that the precomputed global indicator settings can satisfy the strategy:
- `entry.sma_long`, `entry.sma_fast`, and `entry.sma_mid` must all be present in `AppConfig.indicators.sma_periods`
- `entry.rsi_period` must equal `AppConfig.indicators.rsi_period`
- `entry.volume_sma_period` must equal `AppConfig.indicators.volume_sma_period`
- `entry.volume_mult` must be `>= 1.0`

Phase 2 uses only indicators produced by the global `compute_indicators()` pipeline. Strategy-specific custom indicator pipelines are out of scope.

#### Properties

- `name` → `"etf_momentum"`
- `universe` → the symbol list from the YAML config
- `min_history_days` → `max(entry.sma_long, entry.rsi_period, entry.volume_sma_period, indicator_settings.atr_period)` (200 with the default config)
- `time_stop_days` → `exit.time_stop_days`

#### `evaluate()` logic

For each symbol present in the provided `universe` mapping, check conditions against the **last row** of that symbol's DataFrame (the "current" bar). A configured symbol may be absent from `universe` if the data feed excluded it for insufficient history; missing symbols are skipped, not treated as errors.

For a given symbol and evaluation call, emit **at most one** signal:
- If any SELL condition is true, emit a SELL signal and do not also emit a BUY.
- Otherwise, emit a BUY signal only if all BUY conditions are true.
- The strategy may emit SELL signals even when no position is currently open; the backtest engine is responsible for ignoring SELL signals for symbols with no open position.

**Entry conditions (all must be true to emit a BUY signal):**

1. `close > sma_{entry.sma_long}` — price above long-term SMA
2. `rsi_{entry.rsi_period}` crossed above `entry.rsi_oversold` — the current bar's RSI is above the threshold AND at least one of the previous 3 bars had RSI below the threshold (bounce detection)
3. `sma_{entry.sma_fast} > sma_{entry.sma_mid}` — intermediate uptrend
4. `volume > entry.volume_mult * volume_sma_{entry.volume_sma_period}` — volume confirmation

**Exit conditions (any one triggers a SELL signal):**

1. `rsi_{entry.rsi_period} > exit.rsi_overbought` — overbought on the same RSI period used for entry
2. Trend reversal: `sma_{entry.sma_fast} < sma_{entry.sma_mid}` (SMA cross-under)

Note: ATR-based stop loss and time stop are execution-level concerns. In Phase 2, `evaluate()` includes `stop_loss_pct` in the Signal (computed on the current bar as `exit.atr_stop_mult * atr_{indicator_settings.atr_period} / close`), but the actual stop order submission is deferred to Phase 3.

**Signal strength:**

For BUY signals, strength is computed as:
```python
strength = max(0.0, min(1.0, (volume / volume_sma_current) - 1.0))  # clamp to [0.0, 1.0]
```
Higher volume relative to average = stronger signal. If volume exactly equals the SMA, strength is 0.0.

For SELL signals, strength is always `1.0`.

**Reason string format:**

```
f"BUY: close={close:.2f} > sma{entry.sma_long}={sma_long:.2f}, "
f"rsi={rsi:.1f} bounce, "
f"sma{entry.sma_fast}={sma_fast:.2f} > sma{entry.sma_mid}={sma_mid:.2f}, "
f"vol_ratio={ratio:.1f}x"
```

```
f"SELL: rsi={rsi:.1f} > {exit.rsi_overbought} overbought"
```

```
f"SELL: sma{entry.sma_fast}={sma_fast:.2f} < sma{entry.sma_mid}={sma_mid:.2f} trend reversal"
```

Rules:
- `evaluate()` raises `StrategyError` if the current DataFrame for a symbol is empty, missing required indicator columns, or yields an invalid signal payload.
- `stop_loss_pct` must be strictly positive.

### 2.6 Backtest Engine

#### Data feed (`backtest/data_feed.py`)

```python
class HistoricalDataFeed:
    def __init__(
        self,
        provider: DataProvider,
        config: AppConfig,
    ) -> None:
        """Fetches historical data directly from the provider."""
        ...

    def load_universe(
        self,
        symbols: list[str],
        start: date,
        end: date,
    ) -> dict[str, pd.DataFrame]:
        """Fetch and enrich data for all symbols.

        Returns:
            dict of symbol → enriched DataFrame (OHLCV + indicators),
            filtered to [start, end] date range.
        """
        ...
```

Rules:
- Computes the longest indicator window from `config.indicators` (same formula as `compute_indicators()` uses) and calculates `fetch_start = start - timedelta(days=int(longest_window * 1.5))` to provide sufficient pre-`start` data for indicator warmup.
- For each symbol, calls `provider.get_bars(symbol, fetch_start, end, config.data.default_timeframe)` to fetch raw bars covering the warmup + backtest range.
- Calls `compute_indicators()` on each symbol's full DataFrame using the global `AppConfig.indicators` settings.
- Filters the enriched DataFrame to `[start, end]` inclusive **after** indicator computation, so indicator values at `start` are valid. Filtering compares `.date()` of the UTC DatetimeIndex against `start` and `end`.
- If a symbol raises `InsufficientHistoryError` during indicator computation, log a warning and exclude it from the result (do not raise).
- If `provider.get_bars()` raises `DataProviderError` for a symbol (e.g. unknown ticker), log a warning and exclude it.
- Returns only symbols that have data within the requested date range.
- Returned symbols may be a strict subset of the requested `symbols` list.
- Does **not** use `BarCache`. Backtesting calls the provider directly for the exact date range needed, avoiding the cache's `lookback_days`-relative fetch logic which cannot cover arbitrary historical ranges.

#### Backtest engine (`backtest/engine.py`)

```python
@dataclass
class Trade:
    symbol: str
    direction: SignalDirection
    entry_date: date
    entry_price: float
    exit_date: date | None = None
    exit_price: float | None = None
    shares: int = 0
    stop_loss_price: float | None = None
    pnl: float = 0.0
    exit_reason: str = ""

@dataclass
class BacktestResult:
    trades: list[Trade]
    equity_curve: pd.Series           # Index of date objects → portfolio value (one entry per simulation date)
    metrics: dict[str, float | int]   # output of compute_metrics()
    initial_capital: float
    final_equity: float

class BacktestEngine:
    def __init__(
        self,
        strategy: Strategy,
        config: AppConfig,
    ) -> None:
        ...

    def run(
        self,
        universe_data: dict[str, pd.DataFrame],
        start: date,
        end: date,
    ) -> BacktestResult:
        """Run the backtest over the date range."""
        ...
```

**Execution model:**

The backtest iterates over the sorted union of bar dates present in `universe_data` within `[start, end]`. It does **not** synthesize extra dates for weekends or holidays.

For a simulation date `T`, a symbol is considered tradable only if it has a bar dated `T`. If an open position's symbol has no bar on `T`, carry forward the most recent close for equity-marking only and do not evaluate stop-loss or strategy exits for that symbol on `T`.

The backtest loop is:

1. **Slice data** — For each symbol, slice the DataFrame to include only rows up to and including the current date. This prevents look-ahead bias.
2. **Check exits** — For each open position that has a bar on the current date, check:
   - Stop loss hit: if the current bar's `low <= stop_loss_price`, exit at `stop_loss_price`.
   - Time stop: if `trading_days_held >= strategy.time_stop_days`, exit at current `close`.
   - Strategy SELL signal: evaluated later in the loop and applied only to symbols with an open position.
3. **Evaluate strategy** — Call `strategy.evaluate(sliced_universe)` to get signals.
4. **Validate signals** — For every signal returned, verify:
   - `signal.strategy_name == strategy.name`
   - `0.0 <= signal.strength <= 1.0`
   - `signal.stop_loss_pct > 0`
   - `signal.symbol` exists in `sliced_universe`
   Invalid signals are a strategy bug; raise `StrategyError`.
5. **Apply SELL signals** — For each SELL signal whose symbol currently has an open position, exit at the current `close`.
6. **Process entries** — For BUY signals:
   - Ignore any symbol that already has an open position.
   - Ignore any symbol that already exited earlier on the same simulation date; no same-day exit-and-re-entry in Phase 2.
   - Sort eligible BUY signals by descending `strength`, then by `symbol` ascending as a deterministic tie-breaker.
   - Compute `equity_before_entries` after all exits for the day.
   - `capital_per_position = equity_before_entries * (1 / max_positions)` where `max_positions = 5`. This fixed-fraction approach (industry standard in backtrader, zipline, QuantConnect) trades capital efficiency for predictable risk — each position is always 20% regardless of open slots. A dynamic `equity / available_slots` approach is deferred to Phase 3's risk config alongside trailing stops.
   - Apply slippage: `entry_price = close * (1 + slippage_pct)`
   - Compute position size: `shares = floor(capital_per_position / entry_price)`
   - Compute stop loss price once at entry: `entry_price * (1 - stop_loss_pct)`
   - Deduct cost from cash: `shares * entry_price + commission_per_trade`
7. **Record equity** — `cash + sum(position_value for open positions)`, using the current close when available or the most recent prior close otherwise.

**Position tracking rules:**
- Maximum 5 concurrent positions (hardcoded in Phase 2; extracted to risk config in Phase 3).
- One position per symbol at a time.
- No short selling in Phase 2.
- If insufficient cash to open a new position, skip the signal (log at DEBUG).
- Stop loss price is static for the life of the trade in Phase 2. Trailing stops are deferred to Phase 3.
- Gap-through stop-loss behavior is simplified in Phase 2: if a bar trades through the stop, the fill price is still `stop_loss_price`.
- On exit: `cash += shares * exit_price - commission_per_trade`. Commission applies to both entry and exit.
- `trade.pnl = (exit_price - entry_price) * shares - 2 * commission_per_trade`.
- Exit precedence when multiple conditions trigger on the same bar for the same symbol: stop-loss (step 2) wins over time-stop (step 2, checked second), which wins over strategy SELL (step 5). Only the first triggered exit applies; the position is already closed by the time later checks run.
- `trading_days_held` counts completed trading bars after the entry bar. Example: `time_stop_days = 15` exits on the 15th bar after `entry_date`; the entry day does not count.
- If `universe_data` is empty after loading, `run()` raises `BacktestError`.
- Trades that are still open at end of backtest are force-closed at the last available bar's close price on or before `end` with `exit_reason = "backtest_end"`.

**Logging:**
- INFO: trade entries (`ENTRY symbol=SPY shares=10 price=450.45 stop=427.93`), trade exits (`EXIT symbol=SPY price=460.00 pnl=95.50 reason=rsi_overbought`), backtest start/end summary.
- DEBUG: signal evaluation counts per simulation date, skipped signals (insufficient cash, position limit, no open position for SELL), equity snapshots, data feed symbol load/exclusion.
- WARNING: symbols excluded from universe (insufficient history, provider errors).

#### Metrics (`backtest/metrics.py`)

```python
def compute_metrics(
    trades: list[Trade],
    equity_curve: pd.Series,
    initial_capital: float,
) -> dict[str, float | int]:
    """Compute backtest performance metrics.

    Returns dict with keys:
        total_return_pct, cagr_pct, sharpe_ratio, sortino_ratio,
        max_drawdown_pct, win_rate_pct, profit_factor,
        total_trades, avg_holding_days
    """
    ...
```

**Metric definitions:**

| Metric | Formula | Notes |
|--------|---------|-------|
| `total_return_pct` | `(final - initial) / initial * 100` | |
| `cagr_pct` | `((final / initial) ^ (365.25 / days) - 1) * 100` | `days` = calendar days in backtest period |
| `sharpe_ratio` | `mean(daily_returns) / std(daily_returns) * sqrt(252)` | Annualized. Use 0.0 if std is 0. |
| `sortino_ratio` | `mean(daily_returns) / downside_std * sqrt(252)` | `downside_std` = std of negative returns only. Use 0.0 if no negative returns. |
| `max_drawdown_pct` | `max((peak - trough) / peak) * 100` | Rolling peak drawdown on equity curve |
| `win_rate_pct` | `winning_trades / total_trades * 100` | A trade wins if `pnl > 0`. Use 0.0 if no trades. |
| `profit_factor` | `sum(winning_pnl) / abs(sum(losing_pnl))` | Use `float('inf')` if no losing trades. Use 0.0 if no winning trades. |
| `total_trades` | count of closed trades | |
| `avg_holding_days` | `mean(exit_date - entry_date)` in calendar days | Calendar days (includes weekends/holidays) is the standard user-facing convention. Use 0.0 if no trades. |

Rules:
- Daily returns are computed from the equity curve: `equity_curve.pct_change().dropna()`.
- All percentage metrics are expressed as percentages (e.g. 15.0 for 15%), not decimals.
- `profit_factor` is allowed to be `float("inf")` only when there are no losing trades.
- If the equity curve has fewer than 2 data points, return all float metrics as 0.0 and `total_trades` as an integer count.

### 2.7 Database Additions (`db/models.py`)

Add a `signals_log` table for recording signals generated during backtests:

```python
class SignalLog(Base):
    __tablename__ = "signals_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_name: Mapped[str] = mapped_column(String, nullable=False)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    direction: Mapped[str] = mapped_column(String, nullable=False)  # "BUY" or "SELL"
    strength: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss_pct: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    signal_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_signals_strategy_symbol", "strategy_name", "symbol"),
    )
```

Create this table in Phase 2. Persisting signals to it is explicitly **non-blocking** for the first implementation and may be omitted if it would complicate the core backtest loop.

### 2.8 CLI (`__main__.py` additions)

Add the `backtest` command:

```
bread backtest --strategy <name> --start <YYYY-MM-DD> --end <YYYY-MM-DD>
```

Behavior:

1. Load config.
2. Initialize logging.
3. Auto-initialize DB.
4. Match `--strategy` exactly against `AppConfig.strategies[].name`.
5. Resolve that strategy's `config_path` against the project `config/` directory.
6. Look up the strategy class from the registry using the same canonical name.
7. Instantiate the strategy with the resolved absolute `config_path` and `config.indicators`.
8. Create `HistoricalDataFeed`, load universe data.
9. Create `BacktestEngine`, run backtest.
10. Print metrics summary.

**Required output format:**

```
Backtest: etf_momentum | 2024-01-01 to 2025-12-31
---
Total return:     12.34%
CAGR:              6.21%
Sharpe ratio:      0.85
Sortino ratio:     1.12
Max drawdown:      5.67%
Win rate:         58.33%
Profit factor:     1.45
Total trades:        24
Avg holding days:  8.50
```

Rules:
- Percentages formatted to 2 decimal places, right-aligned.
- Ratios formatted to 2 decimal places.
- `total_trades` as integer, right-aligned.
- `avg_holding_days` to 2 decimal places.
- The header line shows strategy name and date range.
- `--strategy` must use the canonical lowercase snake_case strategy name from config and the registry.
- Exit code `0` on success, non-zero on failure.

---

## Verification Criteria

All checks must pass before moving to Phase 3.

### Unit Tests (`pytest tests/unit/`)

1. **Domain models** (`test_models.py`)
   - `Signal` is immutable (frozen dataclass)
   - `SignalDirection` enum has `BUY` and `SELL` values

2. **Strategy registry** (`test_registry.py`)
   - `@register("example_strategy")` adds strategy to registry
   - Duplicate canonical strategy name raises `ValueError`
   - `get_strategy()` returns the registered class
   - `get_strategy()` raises `KeyError` for unknown name
   - `list_strategies()` returns all registered names

3. **ETF Momentum evaluate()** (`test_etf_momentum.py`)
   Given synthetic DataFrames with known indicator values:
   - **All entry conditions met** → BUY signal generated with correct `stop_loss_pct` and `strength`
   - **RSI never below 30** (no bounce) → no BUY signal
   - **Price below SMA(200)** → no BUY signal
   - **SMA(20) < SMA(50)** → no BUY signal
   - **Volume below threshold** → no BUY signal
   - **RSI > 70** → SELL signal with reason containing "overbought"
   - **SMA(20) crosses below SMA(50)** → SELL signal with reason containing "trend reversal"
   - **Required indicator column missing** → `StrategyError`

4. **Backtest engine** (`test_backtest_engine.py`)
   - **No look-ahead bias**: strategy at date T receives data only up to T. Mock strategy that records the max date it sees; assert it never exceeds the current simulation date.
   - **Position limit**: with 5 positions open, a 6th BUY signal is skipped.
   - **Stop loss exit**: when bar low <= stop loss price, position exits at stop price.
   - **Time stop**: position held for `time_stop_days` exits at close.
   - **Force close at backtest end**: open positions are closed on the last date.
   - **Slippage applied**: entry price = close * (1 + slippage_pct).
   - **SELL without open position**: SELL signal is ignored.
   - **No same-day re-entry**: symbol exited on date T does not reopen on T.
   - **Deterministic BUY ordering**: stronger signal wins first; symbol name breaks ties.
   - **Invalid signal payload**: engine raises `StrategyError`

5. **Metrics** (`test_metrics.py`)
   Given a known sequence of trades and equity curve:
   - `total_return_pct` matches hand-calculated value
   - `sharpe_ratio` matches hand-calculated value (within 0.01 tolerance)
   - `max_drawdown_pct` matches hand-calculated value
   - `win_rate_pct` = 50.0 for 1 win + 1 loss
   - `profit_factor` = `float('inf')` when no losing trades
   - All metrics are 0.0 (except `total_trades`) when equity curve has < 2 points
   - No NaN or Inf values in normal cases

### Integration Tests (`pytest tests/integration/`)

1. **Full backtest** — Run ETF Momentum backtest over 2024-01-01 to 2024-12-31 using real Alpaca historical data:
   - Completes without errors
   - Generates at least 1 trade
   - No metric is NaN; `profit_factor` may be `inf` only in the no-losing-trades case
   - Equity curve has one entry per trading day
   - `total_trades` > 0

### CLI Verification

1. `python -m bread backtest --strategy etf_momentum --start 2024-01-01 --end 2024-12-31` runs and produces the metrics summary format specified above
2. `python -m bread backtest --strategy nonexistent --start 2024-01-01 --end 2024-12-31` exits with non-zero code and error message

### Manual Checks

1. `ruff check src/` — clean
2. `mypy src/` — clean
3. Inspect backtest trade log — entries and exits make sense given the strategy rules
