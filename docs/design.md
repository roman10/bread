# Bread: Algorithmic Trading App - Technical Design

## Context

Build an algorithmic trading application for a startup ("bread") targeting small-capital traders ($5K-$20K). The goal is to achieve **~20% annual returns** — ambitious but realistic for systematic swing trading. On $10K capital, that's ~$2,000/year or ~$167/month. The app will use **Alpaca Markets** (commission-free, excellent REST API, free paper trading) as the primary broker, with **Finnhub** for supplementary market data. The repo will be hosted at `roman10/bread` on GitHub.

**Context:** Professional quants at large funds average 10-20% annually. A focused retail system trading liquid ETFs with disciplined risk management can target the higher end of that range. We'll build the infrastructure to scale, validate with paper trading, and grow capital gradually.

---

## Project Structure

```
bread/
├── pyproject.toml
├── .env.example
├── .gitignore
├── README.md
├── config/
│   ├── default.yaml              # All tunable parameters
│   ├── paper.yaml                # Paper trading overrides
│   ├── live.yaml                 # Live trading overrides
│   └── strategies/
│       └── etf_momentum.yaml     # Per-strategy params
├── src/bread/
│   ├── __init__.py
│   ├── __main__.py               # CLI entry: python -m bread
│   ├── app.py                    # Orchestrator
│   ├── core/
│   │   ├── config.py             # Pydantic config + YAML loading
│   │   ├── models.py             # Signal, Position, Order models
│   │   ├── events.py             # In-process event bus
│   │   └── exceptions.py
│   ├── data/
│   │   ├── provider.py           # Abstract data provider
│   │   ├── alpaca_data.py        # Alpaca bars + streaming
│   │   ├── finnhub_data.py       # News, sentiment, earnings
│   │   ├── cache.py              # SQLite cache layer
│   │   └── indicators.py         # Technical indicators (pandas-ta)
│   ├── strategy/
│   │   ├── base.py               # Abstract Strategy interface
│   │   ├── registry.py           # Strategy discovery
│   │   └── etf_momentum.py       # First strategy
│   ├── risk/
│   │   ├── manager.py            # Risk management engine (CRITICAL)
│   │   ├── position_sizer.py     # Position sizing algorithms
│   │   ├── limits.py             # Circuit breakers
│   │   └── validators.py         # Pre-trade validation chain
│   ├── execution/
│   │   ├── engine.py             # Order management
│   │   └── alpaca_broker.py      # Alpaca adapter (bracket orders)
│   ├── backtest/
│   │   ├── engine.py             # Historical replay
│   │   ├── data_feed.py          # Historical data feed
│   │   ├── metrics.py            # Sharpe, drawdown, etc.
│   │   └── models.py             # Trade, BacktestResult dataclasses
│   ├── monitoring/
│   │   ├── tracker.py            # P&L tracking
│   │   ├── journal.py            # Trade journal
│   │   └── alerts.py             # Discord/email via apprise
│   ├── db/
│   │   ├── database.py           # SQLite connection
│   │   └── models.py             # SQLAlchemy ORM
│   └── dashboard/
│       ├── app.py                # Dash app factory + layout
│       ├── pages/
│       │   ├── portfolio.py      # Portfolio overview (home page)
│       │   ├── backtest.py       # Backtest results explorer
│       │   ├── trades.py         # Trade journal viewer
│       │   └── settings.py       # Config editor
│       ├── components/
│       │   ├── charts.py         # Candlestick, equity curve, drawdown
│       │   ├── tables.py         # AG Grid tables for trades, positions
│       │   └── cards.py          # KPI cards (P&L, Sharpe, exposure)
│       └── callbacks/
│           ├── portfolio_cb.py   # Portfolio page callbacks
│           ├── backtest_cb.py    # Backtest page callbacks
│           └── trades_cb.py      # Trades page callbacks
├── tests/
│   ├── conftest.py
│   ├── unit/
│   └── integration/
├── scripts/
│   ├── run_backtest.py
│   └── download_history.py
└── notebooks/
    └── strategy_research.ipynb
```

---

## Core Modules

### 1. Configuration (`core/config.py`)

Pydantic models validate YAML config at startup. Secrets from `.env` via `python-dotenv`. A single `mode` field controls paper/live switching. App refuses to start if config is invalid.

### 2. Domain Models (`core/models.py`)

Dataclasses for `Signal` (strategy output with direction, strength, stop-loss), `Order`, `Position`, `PortfolioSnapshot`.

### 3. Event Bus (`core/events.py`)

Lightweight in-process event bus (dict of callbacks). Decouples modules — risk manager emits `OrderApproved`, execution engine subscribes. No external dependencies (not Kafka/Redis).

### 4. Database (`db/`)

SQLAlchemy ORM with SQLite. Tables: `trades`, `orders`, `portfolio_snapshots`, `market_data_cache`, `signals_log`. Zero-ops, single file, sufficient for this scale.

---

## Data Pipeline

### Alpaca Data (`data/alpaca_data.py`)

Fetch OHLCV bars via `alpaca-py` `StockHistoricalDataClient`. Daily bars with 200-day lookback. Cache to SQLite; refresh if data older than 1 day.

### Finnhub Data (`data/finnhub_data.py`)

Supplementary signals only — news sentiment, earnings calendar. Rate-limited to 50 req/min (headroom under 60 free tier limit). Used as filters, not primary signals.

### Technical Indicators (`data/indicators.py`)

Compute via `pandas-ta`, configured in YAML:
- SMA(20, 50, 200), EMA(9, 21)
- RSI(14), MACD(12, 26, 9)
- ATR(14), Bollinger Bands(20, 2)
- Volume SMA(20)

Returns enriched DataFrame with indicator columns appended to OHLCV data.

---

## Risk Management (Most Critical Module)

### Position Sizing (`risk/position_sizer.py`)

Fixed fractional: `position = (equity × risk_pct) / stop_loss_pct`. Default: 0.5% risk per trade. On $10K with 5% stop: $1,000 per position. Conservative sizing aligned with 20% annual target — no need to swing for the fences.

### Hard Limits & Circuit Breakers (`risk/limits.py`)

| Limit | Default | Action |
|-------|---------|--------|
| Max positions | 5 | Reject new entries |
| Max single position | 20% of equity | Cap position size |
| Max sector exposure | 40% of equity | Reject if exceeded |
| Max daily loss | 1.5% of equity | Halt trading for the day |
| Max weekly loss | 3% of equity | Halt + alert |
| Max drawdown from peak | 7% of equity | Halt all trading, require manual restart |
| PDT guard | 3 day trades / 5 days | Block 4th day trade (account < $25K) |

### Pre-Trade Validation (`risk/validators.py`)

Every signal passes through a validation chain before becoming an order:
1. Buying power check
2. Position limit check
3. Concentration check
4. Drawdown check
5. PDT check
6. Spread/liquidity check
7. Volatility check

Rejection logged with reason. No silent drops.

### Stop Loss Implementation

Stop losses submitted as **Alpaca bracket orders** (OCO), so they execute even if the bot is down. Never rely on software-only stops.

---

## Strategy Framework

### Abstract Interface (`strategy/base.py`)

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

Same interface for backtest and live — the single most important design decision.

### Strategy Registration (`strategy/registry.py`)

`@register` decorator. Adding a new strategy: (1) create file, (2) implement `evaluate()`, (3) add YAML config. No changes to other modules.

### ETF Momentum Strategy (`strategy/etf_momentum.py`)

**Universe:** SPY, QQQ, IWM, DIA, XLF, XLK, XLE, XLV, GLD, TLT

**Entry (long):**
1. Price > SMA(200) — long-term uptrend filter
2. RSI(14) bounces from <30 back above 30 — oversold bounce
3. SMA(20) > SMA(50) — intermediate uptrend
4. Volume > 20-day average — participation confirmation
5. No earnings within 3 days — Finnhub calendar check

**Exit:**
1. RSI(14) > 70 — take profit (overbought)
2. 1.5× ATR(14) stop loss — bracket order to Alpaca
3. Trailing stop after 2× ATR gain
4. Time stop: close after 15 trading days
5. Trend reversal: SMA(20) crosses below SMA(50)

**Characteristics:** 3-15 day holds, 4-8 trades/month, avoids PDT entirely.

---

## Execution Engine

### Alpaca Broker (`execution/alpaca_broker.py`)

Wraps `alpaca-py` `TradingClient`. Paper/live controlled by single `paper=True/False` flag. Always uses bracket orders for automatic stop-loss/take-profit.

### Order Management (`execution/engine.py`)

Submit orders, track fills, reconcile positions with broker state on every tick. Emit events for monitoring. Idempotent — safe to call multiple times.

### Paper → Live Switching

Controlled by single config value. Live mode:
- Reads `config/live.yaml` for live API keys
- Applies stricter risk limits
- Requires typing "CONFIRM" on startup

---

## Backtest Engine

### Historical Replay (`backtest/engine.py`)

Replay data through same Strategy interface. Slices data at each date to prevent look-ahead bias. Strategy code identical to live.

### Metrics (`backtest/metrics.py`)

Total return, CAGR, Sharpe ratio, Sortino ratio, max drawdown, win rate, profit factor, average holding period.

---

## Application Orchestrator

### Scheduler (`app.py`)

`APScheduler` fires `tick()` every 15 minutes during market hours (9:30 AM - 4:00 PM ET).

### Tick Cycle

```
Refresh data → Evaluate strategies → Risk-check signals → Execute orders → Update monitoring
```

### CLI (`__main__.py`)

`typer`-based CLI:
- `bread run` — start the trading bot (add `--dashboard` to serve web UI on `:8050`)
- `bread backtest` — run historical backtest
- `bread status` — show current portfolio and P&L
- `bread dashboard` — launch dashboard standalone (read-only, no trading)

---

## Monitoring & Alerts

### Trade Journal (`monitoring/journal.py`)

Every trade logged to SQLite: entry/exit, P&L, strategy, risk metrics at entry, reasons.

### P&L Tracker (`monitoring/tracker.py`)

Daily/weekly/monthly P&L, win rate, Sharpe, max drawdown, portfolio exposure.

### Alerts (`monitoring/alerts.py`)

Via `apprise` (Discord, email, Slack):
- Trade executed, daily P&L summary (normal priority)
- Loss limits hit (high priority)
- Max drawdown breached, system errors (critical)

---

## Dashboard (Phase 5)

### Tech Stack

| Component | Choice | Why |
|-----------|--------|-----|
| Framework | **Dash 3.x** (MIT license) | Python-native, battle-tested in finance, no JS toolchain |
| Charting | **dash-tradingview** | TradingView Lightweight Charts inside Dash — professional candlestick/OHLCV rendering |
| Tables | **dash-ag-grid** | Sortable, filterable financial tables with mini-charts in cells |
| Layout | **dash-bootstrap-components** | Responsive grid, modals, alerts — Bootstrap 5 |
| Real-time | **dcc.Interval** + **dash-socketio** | Polling for periodic refresh, WebSocket push for trade fill events |
| Server | **gunicorn** (production), Dash dev server (local) | Standard Flask deployment |

### Dependencies

```
dash>=3.0
dash-bootstrap-components>=1.6
dash-ag-grid>=31.0
dash-tradingview>=0.0.5
dash-socketio>=0.3
```

### Pages

#### Portfolio Overview (home: `/`)

The primary dashboard view. Displays at a glance:

- **KPI cards** — Total equity, daily P&L ($ and %), open positions count, buying power remaining, current drawdown from peak
- **Equity curve** — Line chart of portfolio value over time (from `portfolio_snapshots` table)
- **Open positions table** — AG Grid showing symbol, entry price, current price, unrealized P&L, stop-loss level, days held
- **Exposure breakdown** — Pie/bar chart of sector allocation vs. hard limits
- **Auto-refresh** — `dcc.Interval` at 15s during market hours, 5min after hours

#### Backtest Explorer (`/backtest`)

Interactive backtest result visualization:

- **Strategy selector** + date range picker → triggers backtest run or loads cached results
- **Candlestick chart** — TradingView chart with entry/exit markers overlaid, indicator overlays (SMA, RSI, Bollinger Bands) toggled via checkboxes
- **Equity curve** — Portfolio value over backtest period with drawdown shading
- **Metrics panel** — Total return, CAGR, Sharpe, Sortino, max drawdown, win rate, profit factor, avg holding period
- **Trade list** — AG Grid of all backtest trades, click-to-highlight on chart

#### Trade Journal (`/trades`)

Historical trade browser:

- **Filterable AG Grid** — All executed trades with columns: date, symbol, direction, entry/exit prices, P&L, strategy, hold duration, risk metrics at entry
- **Trade detail panel** — Click a row to see: entry/exit reasoning (from signals log), chart snapshot around trade period, risk state at time of entry
- **Summary stats** — Win rate, average win/loss, expectancy, P&L by strategy, P&L by symbol

#### Settings (`/settings`)

Config viewer and editor:

- **Current config display** — Renders active YAML config as a structured form (read from Pydantic models via `.model_dump()`)
- **Editable fields** — Risk limits, indicator parameters, strategy toggles, alert preferences
- **Validation** — Pydantic validates on submit, shows errors inline
- **Save** — Writes updated YAML, requires restart confirmation for live changes

### Integration with Trading Bot

The dashboard runs **in the same process** as the trading bot:

```
bread run --mode paper --dashboard
```

- `APScheduler` runs the trading tick cycle in the background
- Dash serves the UI on a configurable port (default `:8050`)
- Both share the same SQLAlchemy engine and session factory
- The event bus pushes trade events to the dashboard via `dash-socketio`
- Without `--dashboard`, the bot runs headless (CLI-only, current behavior preserved)

### Callback Structure

Callbacks are organized by page to prevent a monolithic callback file:

- Each page module registers its own callbacks via `dash.callback`
- Callbacks query SQLAlchemy directly (read-only for portfolio/trades, read-write for settings)
- Long-running operations (backtest execution) use Dash 3.x `background_callback` with a `diskcache` backend to avoid blocking the UI
- Error states in callbacks return user-friendly alert components, never raise exceptions

### Authentication

Not needed initially (single-user, localhost). Future option: `dash-auth` basic auth or reverse proxy (nginx) with HTTP basic auth for remote access.

---

## Architecture Principles

1. **Bracket orders over software stops** — Alpaca executes stops even if bot crashes
2. **Fail closed** — errors reject trades, never silently proceed
3. **Same code for backtest and live** — strategy interface identical in both modes
4. **Configuration over code** — tune via YAML, no code changes needed
5. **Gradual scaling** — paper → $1K live → $5K → $10K → $20K

---

## Implementation Status

| Phase | Status | Deliverable |
|-------|--------|-------------|
| 1. Foundation | **Complete** | Scaffolding, config, database, data pipeline, indicators |
| 2. Strategy + Backtest | **Complete** | Strategy framework, ETF momentum, backtest engine |
| 3. Execution + Paper | Pending | Execution engine, orchestrator, paper trading |
| 4. Monitoring | Pending | Trade journal, P&L tracker, alerts |
| 5. Dashboard (UI) | Pending | Dash-based web dashboard |
| 6. Validation | Pending | 2-4 weeks paper trading, tuning |
| 7. Go Live | Pending | Live with minimal capital, gradual scaling |

### Phase 1 Implementation Notes

Completed modules:
- `core/config.py` — Pydantic v2 config with YAML loading, deep merge, env-var secrets
- `core/exceptions.py` — Full exception hierarchy
- `core/logging.py` — JSON-formatted logging to stdout
- `data/provider.py` — Abstract `DataProvider` with `get_bars()` contract
- `data/alpaca_data.py` — Alpaca `StockHistoricalDataClient` with tenacity retries
- `data/cache.py` — SQLite bar cache with NYSE trading-day staleness logic
- `data/indicators.py` — pandas-ta indicator computation with configurable parameters
- `db/models.py` — SQLAlchemy 2.0+ ORM (`MarketDataCache` table)
- `db/database.py` — Engine, session factory, path resolution
- `__main__.py` — Typer CLI with `bread db init` and `bread fetch <SYMBOL>`

Deferred from Phase 1 to their owning phases:
- Domain models (`Signal`, `Order`, `Position`, `PortfolioSnapshot`) → Phase 2
- Event bus (`core/events.py`) → Phase 3
- Finnhub data provider → Phase 3
- `get_latest_bar()` on DataProvider → Phase 3
- Additional DB tables (`signals_log`, `orders`, `trades`, `portfolio_snapshots`) → Phase 2-4

### Phase 2 Implementation Notes

Completed modules:
- `core/models.py` — `SignalDirection` (StrEnum), `Signal` (frozen dataclass with `__post_init__` validation)
- `core/config.py` additions — `StrategySettings`, `BacktestSettings`, `AppConfig.strategies`/`backtest` fields, `_unique_strategy_names` validator, `CONFIG_DIR` export
- `core/exceptions.py` additions — `StrategyError`, `BacktestError`
- `strategy/base.py` — Abstract `Strategy` interface with `evaluate()`, `name`, `universe`, `min_history_days`, `time_stop_days`
- `strategy/registry.py` — `@register()` decorator, `get_strategy()`, `list_strategies()`
- `strategy/etf_momentum.py` — ETF Momentum strategy with entry/exit conditions, indicator validation, ATR-based stop loss
- `backtest/models.py` — `Trade` and `BacktestResult` dataclasses (extracted from engine for shared use by metrics and tests)
- `backtest/data_feed.py` — `HistoricalDataFeed` with indicator warmup, per-symbol error handling
- `backtest/engine.py` — `BacktestEngine` with position tracking, stop loss/time stop exits, slippage, commission, force-close at end
- `backtest/metrics.py` — `compute_metrics()` for Sharpe, Sortino, CAGR, drawdown, win rate, profit factor
- `db/models.py` additions — `SignalLog` table (created but signal persistence deferred)
- `__main__.py` additions — `bread backtest` CLI command
- `config/strategies/etf_momentum.yaml` — Strategy-specific parameters
- `config/default.yaml` additions — `strategies` and `backtest` sections

Structural decisions:
- `Trade`/`BacktestResult` in `backtest/models.py` (not in `engine.py`) to avoid circular imports between engine and metrics
- `SignalDirection` uses `StrEnum` (not `str, Enum`) for cleaner serialization
- Signal validation (`strength`, `stop_loss_pct`) in `__post_init__` rather than Pydantic
- Finnhub earnings check deferred to Phase 3 (replaced with no-op in Phase 2)

Deferred from Phase 2 to Phase 3:
- Signal persistence to `signals_log` table (table created, writes not implemented)
- Dynamic position sizing (fixed 1/5 capital per position in Phase 2)
- Trailing stops (static stop loss only)
- Finnhub earnings calendar check (no-op in Phase 2)

---

## Verification

1. `pytest tests/unit/` — all pass
2. `python -m bread backtest --strategy etf_momentum --start 2024-01-01 --end 2025-12-31` — positive Sharpe
3. `python -m bread run --mode paper` — scheduler fires, data fetches, signals generate
4. Alpaca paper dashboard — paper orders appear
5. `ruff check src/` and `mypy src/` — clean
