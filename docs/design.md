# Bread: Algorithmic Trading App - Technical Design

## Context

Build an algorithmic trading application for a startup ("bread") targeting small-capital traders ($5K-$20K). The goal is to generate consistent monthly income (~$1,000/month target, though realistic early returns will be lower). The app will use **Alpaca Markets** (commission-free, excellent REST API, free paper trading) as the primary broker, with **Finnhub** for supplementary market data. The repo will be hosted at `roman10/bread` on GitHub.

**Key reality check:** $1,000/month on $5-20K requires 60-240% annual returns. Professional quants average 10-20%. We'll build the infrastructure to scale, start with realistic expectations (2-5% monthly), and grow capital over time.

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
│   │   └── metrics.py            # Sharpe, drawdown, etc.
│   ├── monitoring/
│   │   ├── tracker.py            # P&L tracking
│   │   ├── journal.py            # Trade journal
│   │   └── alerts.py             # Discord/email via apprise
│   └── db/
│       ├── database.py           # SQLite connection
│       └── models.py             # SQLAlchemy ORM
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

Fixed fractional: `position = (equity × risk_pct) / stop_loss_pct`. Default: 1% risk per trade. On $10K with 5% stop: $2,000 per position.

### Hard Limits & Circuit Breakers (`risk/limits.py`)

| Limit | Default | Action |
|-------|---------|--------|
| Max positions | 5 | Reject new entries |
| Max single position | 20% of equity | Cap position size |
| Max sector exposure | 40% of equity | Reject if exceeded |
| Max daily loss | 2% of equity | Halt trading for the day |
| Max weekly loss | 5% of equity | Halt + alert |
| Max drawdown from peak | 10% of equity | Halt all trading, require manual restart |
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
- `bread run` — start the trading bot
- `bread backtest` — run historical backtest
- `bread status` — show current portfolio and P&L

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

## Architecture Principles

1. **Bracket orders over software stops** — Alpaca executes stops even if bot crashes
2. **Fail closed** — errors reject trades, never silently proceed
3. **Same code for backtest and live** — strategy interface identical in both modes
4. **Configuration over code** — tune via YAML, no code changes needed
5. **Gradual scaling** — paper → $1K live → $5K → $10K → $20K

---

## Implementation Roadmap

| Phase | Timeline | Deliverable |
|-------|----------|-------------|
| 1. Foundation | Week 1-2 | Scaffolding, config, database, data pipeline, indicators |
| 2. Strategy + Backtest | Week 3 | Strategy framework, ETF momentum, backtest engine |
| 3. Execution + Paper | Week 4 | Execution engine, orchestrator, paper trading |
| 4. Monitoring | Week 5 | Trade journal, P&L tracker, alerts |
| 5. Validation | Week 6-8 | 2-4 weeks paper trading, tuning |
| 6. Go Live | Week 9+ | Live with minimal capital, gradual scaling |

---

## Verification

1. `pytest tests/unit/` — all pass
2. `python -m bread backtest --strategy etf_momentum --start 2024-01-01 --end 2025-12-31` — positive Sharpe
3. `python -m bread run --mode paper` — scheduler fires, data fetches, signals generate
4. Alpaca paper dashboard — paper orders appear
5. `ruff check src/` and `mypy src/` — clean
