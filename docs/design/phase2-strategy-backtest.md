# Phase 2: Strategy + Backtest (Week 3)

## Goal

Build the strategy framework, implement the first strategy (ETF Momentum), and create the backtest engine. This phase produces a system that can evaluate historical performance of strategies — with no live/paper execution yet.

---

## Scope

### 2.1 Strategy Framework

- **`strategy/base.py`** — Abstract Strategy interface:
  ```python
  class Strategy(ABC):
      def evaluate(self, universe: dict[str, pd.DataFrame]) -> list[Signal]: ...
      @property
      def name(self) -> str: ...
      @property
      def universe(self) -> list[str]: ...
      @property
      def min_history_days(self) -> int: ...
  ```
- Same interface for backtest and live — the single most important design decision

- **`strategy/registry.py`** — Strategy discovery via `@register` decorator. Adding a new strategy requires: (1) create file, (2) implement `evaluate()`, (3) add YAML config. No changes to other modules.

### 2.2 ETF Momentum Strategy (`strategy/etf_momentum.py`)

- **Universe:** SPY, QQQ, IWM, DIA, XLF, XLK, XLE, XLV, GLD, TLT

- **Entry (long):**
  1. Price > SMA(200) — long-term uptrend filter
  2. RSI(14) bounces from <30 back above 30 — oversold bounce
  3. SMA(20) > SMA(50) — intermediate uptrend
  4. Volume > 20-day average — participation confirmation
  5. No earnings within 3 days — Finnhub calendar check

- **Exit:**
  1. RSI(14) > 70 — take profit (overbought)
  2. 1.5x ATR(14) stop loss — bracket order to Alpaca
  3. Trailing stop after 2x ATR gain
  4. Time stop: close after 15 trading days
  5. Trend reversal: SMA(20) crosses below SMA(50)

- **Characteristics:** 3-15 day holds, 4-8 trades/month, avoids PDT entirely

- **Config:** `config/strategies/etf_momentum.yaml` with all tunable parameters

### 2.3 Backtest Engine

- **`backtest/engine.py`** — Historical replay through the same Strategy interface. Slices data at each date to prevent look-ahead bias. Strategy code identical to live.

- **`backtest/data_feed.py`** — Historical data feed that serves data to the backtest engine, simulating real-time data arrival.

- **`backtest/metrics.py`** — Performance metrics:
  - Total return, CAGR
  - Sharpe ratio, Sortino ratio
  - Max drawdown
  - Win rate, profit factor
  - Average holding period

---

## Verification Criteria

All checks must pass before moving to Phase 3.

### Unit Tests

1. **Strategy registry** — `@register` decorator adds strategy to registry; duplicate name raises error; registry lists all registered strategies
2. **ETF Momentum evaluate()** — given synthetic DataFrames with known indicator values, `evaluate()` produces correct entry/exit signals:
   - Scenario: all entry conditions met → BUY signal generated
   - Scenario: RSI never below 30 → no signal
   - Scenario: price below SMA(200) → no signal
   - Scenario: RSI > 70 on existing position → SELL signal
3. **Backtest engine** — no look-ahead bias: strategy at date T only sees data up to T
4. **Metrics** — given a known sequence of trades, verify Sharpe, max drawdown, win rate match hand-calculated values

### Integration Tests

1. **Full backtest** — run ETF Momentum backtest over 2024-01-01 to 2025-12-31 using real Alpaca historical data:
   - Completes without errors
   - Generates at least 1 trade
   - All metrics are finite numbers (no NaN/Inf)
   - Sharpe ratio is a reasonable value (not obviously broken)

### CLI Verification

1. `python -m bread backtest --strategy etf_momentum --start 2024-01-01 --end 2025-12-31` runs and produces a metrics summary output

### Manual Checks

1. `ruff check src/` — clean
2. `mypy src/` — clean
3. Inspect backtest trade log — entries and exits make sense given the strategy rules
