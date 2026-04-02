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

The contracts below are the implementation source of truth for Phase 2.

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
  volume_mult: 1.0       # volume > volume_mult * volume_sma

exit:
  rsi_overbought: 70
  atr_stop_mult: 1.5     # stop loss = atr_stop_mult * ATR
  atr_trail_trigger: 2.0 # activate trailing stop after 2x ATR gain
  time_stop_days: 15     # close after N trading days
```

#### Pydantic config models (`core/config.py` additions)

```python
class StrategySettings(BaseModel):
    name: str
    config_path: str  # relative to config/strategies/

class BacktestSettings(BaseModel):
    initial_capital: float = Field(default=10000.0, gt=0)
    commission_per_trade: float = Field(default=0.0, ge=0)  # Alpaca is commission-free
    slippage_pct: float = Field(default=0.001, ge=0)        # 0.1% default slippage estimate

class AppConfig(BaseModel):
    # ... existing fields ...
    strategies: list[StrategySettings] = []
    backtest: BacktestSettings = BacktestSettings()
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

Strategy YAML files are loaded on demand by the strategy implementation, not by `load_config()`. The `config_path` is resolved relative to the `config/` directory. Strategy classes receive the raw `dict` from `yaml.safe_load()` and validate it internally.

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
```

Rules:
- `evaluate()` receives DataFrames that already have indicator columns appended (output of `compute_indicators()`).
- `evaluate()` must not modify the input DataFrames.
- `evaluate()` must only read data up to the last row of the DataFrame. The backtest engine controls the time boundary by slicing before calling `evaluate()`.
- If `evaluate()` encounters an error, it raises `StrategyError`. It never returns partial results silently.

#### Strategy registry (`strategy/registry.py`)

```python
_REGISTRY: dict[str, type[Strategy]] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    """Class decorator to register a strategy."""
    name = cls.__name__
    if name in _REGISTRY:
        raise ValueError(f"Strategy already registered: {name}")
    _REGISTRY[name] = cls
    return cls


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
- Registration happens at import time via the `@register` decorator.
- The registry key is the **class name**, not a configurable string. This avoids mismatches.
- `strategy/__init__.py` must import all strategy modules to trigger registration:
  ```python
  from bread.strategy import etf_momentum  # noqa: F401
  ```
- Adding a new strategy: (1) create file, (2) decorate with `@register`, (3) import in `__init__.py`, (4) add YAML config. No changes to framework code.

### 2.5 ETF Momentum Strategy (`strategy/etf_momentum.py`)

#### Constructor

```python
@register
class EtfMomentum(Strategy):
    def __init__(self, config_path: Path) -> None:
        """Load strategy-specific config from YAML."""
        ...
```

The constructor loads `config/strategies/etf_momentum.yaml` and stores the parsed parameters. If the file is missing or invalid, raise `StrategyError`.

#### Properties

- `name` → `"etf_momentum"`
- `universe` → the symbol list from the YAML config
- `min_history_days` → `200` (driven by `entry.sma_long`)

#### `evaluate()` logic

For each symbol in the universe, check both entry and exit conditions against the **last row** of the provided DataFrame (the "current" bar).

**Entry conditions (all must be true to emit a BUY signal):**

1. `close > sma_{entry.sma_long}` — price above long-term SMA
2. `rsi_{entry.rsi_period}` crossed above `entry.rsi_oversold` — the current bar's RSI is above the threshold AND at least one of the previous 3 bars had RSI below the threshold (bounce detection)
3. `sma_{entry.sma_fast} > sma_{entry.sma_mid}` — intermediate uptrend
4. `volume > entry.volume_mult * volume_sma_20` — volume confirmation

**Exit conditions (any one triggers a SELL signal):**

1. `rsi_{entry.rsi_period} > exit.rsi_overbought` — overbought
2. Trend reversal: `sma_{entry.sma_fast} < sma_{entry.sma_mid}` (SMA cross-under)

Note: ATR-based stop loss, trailing stop, and time stop are execution-level concerns. In Phase 2, `evaluate()` includes `stop_loss_pct` in the Signal (computed as `exit.atr_stop_mult * atr_{indicators.atr_period} / close`), but the actual stop order submission is deferred to Phase 3.

**Signal strength:**

For BUY signals, strength is computed as:
```python
strength = min(1.0, (volume / volume_sma) - 1.0)  # clamp to [0.0, 1.0]
```
Higher volume relative to average = stronger signal. If volume exactly equals the SMA, strength is 0.0.

For SELL signals, strength is always `1.0`.

**Reason string format:**

```
"BUY: close={close:.2f} > sma200={sma:.2f}, rsi={rsi:.1f} bounce, sma20={sma20:.2f} > sma50={sma50:.2f}, vol_ratio={ratio:.1f}x"
```

```
"SELL: rsi={rsi:.1f} > 70 overbought"
```

```
"SELL: sma20={sma20:.2f} < sma50={sma50:.2f} trend reversal"
```

### 2.6 Backtest Engine

#### Data feed (`backtest/data_feed.py`)

```python
class HistoricalDataFeed:
    def __init__(
        self,
        session: Session,
        provider: DataProvider,
        config: AppConfig,
    ) -> None:
        """Uses the existing BarCache + compute_indicators pipeline."""
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
- Uses `BarCache.get_bars()` to fetch/cache raw bars for each symbol.
- Calls `compute_indicators()` on each symbol's DataFrame.
- Filters the enriched DataFrame to `[start, end]` inclusive.
- If a symbol has insufficient history for indicators, log a warning and exclude it from the result (do not raise).
- Returns only symbols that have data within the requested date range.

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
    equity_curve: pd.Series           # DatetimeIndex → portfolio value
    metrics: dict[str, float]         # output of compute_metrics()
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

The backtest iterates over each trading day in `[start, end]`:

1. **Slice data** — For each symbol, slice the DataFrame to include only rows up to and including the current date. This prevents look-ahead bias.
2. **Check exits** — For each open position, check:
   - Stop loss hit: if the current bar's `low <= stop_loss_price`, exit at `stop_loss_price`.
   - Time stop: if `trading_days_held >= exit.time_stop_days`, exit at current `close`.
   - Strategy SELL signal: exit at current `close`.
3. **Evaluate strategy** — Call `strategy.evaluate(sliced_universe)` to get signals.
4. **Process entries** — For each BUY signal where no position is already open for that symbol:
   - Compute position size: `shares = floor(capital_per_position / entry_price)`
   - `capital_per_position = equity * (1 / max_positions)` where `max_positions = 5`
   - Apply slippage: `entry_price = close * (1 + slippage_pct)`
   - Compute stop loss price: `entry_price * (1 - stop_loss_pct)`
   - Deduct cost from cash: `shares * entry_price + commission_per_trade`
5. **Record equity** — `cash + sum(position_value for open positions)`, using current close prices.

**Position tracking rules:**
- Maximum 5 concurrent positions (hardcoded in Phase 2; extracted to risk config in Phase 3).
- One position per symbol at a time.
- No short selling in Phase 2.
- If insufficient cash to open a new position, skip the signal (log at DEBUG).
- Trades that are still open at end of backtest are force-closed at the last bar's close price with `exit_reason = "backtest_end"`.

#### Metrics (`backtest/metrics.py`)

```python
def compute_metrics(
    trades: list[Trade],
    equity_curve: pd.Series,
    initial_capital: float,
) -> dict[str, float]:
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
| `avg_holding_days` | `mean(exit_date - entry_date)` in calendar days | Use 0.0 if no trades. |

Rules:
- Daily returns are computed from the equity curve: `equity_curve.pct_change().dropna()`.
- All percentage metrics are expressed as percentages (e.g. 15.0 for 15%), not decimals.
- If the equity curve has fewer than 2 data points, return all metrics as 0.0 except `total_trades`.

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
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_signals_strategy_symbol", "strategy_name", "symbol"),
    )
```

This table is written to during backtests (optional, for debugging/analysis). It is not on the critical path.

### 2.8 CLI (`__main__.py` additions)

Add the `backtest` command:

```
bread backtest --strategy <name> --start <YYYY-MM-DD> --end <YYYY-MM-DD>
```

Behavior:

1. Load config.
2. Initialize logging.
3. Auto-initialize DB.
4. Look up strategy from registry by name.
5. Instantiate strategy with its config path resolved from `AppConfig.strategies`.
6. Create `HistoricalDataFeed`, load universe data.
7. Create `BacktestEngine`, run backtest.
8. Print metrics summary.

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
- Exit code `0` on success, non-zero on failure.

---

## Verification Criteria

All checks must pass before moving to Phase 3.

### Unit Tests (`pytest tests/unit/`)

1. **Domain models** (`test_models.py`)
   - `Signal` is immutable (frozen dataclass)
   - `SignalDirection` enum has `BUY` and `SELL` values

2. **Strategy registry** (`test_registry.py`)
   - `@register` decorator adds strategy to registry
   - Duplicate name raises `ValueError`
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
   - **RSI > 70 on existing position** → SELL signal with reason containing "overbought"
   - **SMA(20) crosses below SMA(50)** → SELL signal with reason containing "trend reversal"

4. **Backtest engine** (`test_backtest_engine.py`)
   - **No look-ahead bias**: strategy at date T receives data only up to T. Mock strategy that records the max date it sees; assert it never exceeds the current simulation date.
   - **Position limit**: with 5 positions open, a 6th BUY signal is skipped.
   - **Stop loss exit**: when bar low <= stop loss price, position exits at stop price.
   - **Time stop**: position held for `time_stop_days` exits at close.
   - **Force close at backtest end**: open positions are closed on the last date.
   - **Slippage applied**: entry price = close * (1 + slippage_pct).

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
   - All metrics are finite numbers (no NaN/Inf)
   - Equity curve has one entry per trading day
   - `total_trades` > 0

### CLI Verification

1. `python -m bread backtest --strategy etf_momentum --start 2024-01-01 --end 2024-12-31` runs and produces the metrics summary format specified above
2. `python -m bread backtest --strategy nonexistent --start 2024-01-01 --end 2024-12-31` exits with non-zero code and error message

### Manual Checks

1. `ruff check src/` — clean
2. `mypy src/` — clean
3. Inspect backtest trade log — entries and exits make sense given the strategy rules
