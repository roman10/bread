# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Bread is an algorithmic swing trading app for small-capital retail traders ($5K-$20K). It connects to Alpaca Markets for commission-free paper and live trading, targeting ~20% annual returns via ETF momentum strategies.

**Current status:** Phase 5 (dashboard) complete, Phase 6 (validation) in progress. See `docs/design.md` and `docs/design/` for phase-by-phase design docs.

## Commands

```bash
# Install
pip install -e ".[dev]"              # main + dev deps
pip install -e ".[dashboard]"        # dashboard deps (Dash/Plotly)

# Run
bread db init                        # initialize SQLite database
bread fetch SPY                      # fetch bars + compute indicators
bread backtest --strategy etf_momentum --start 2023-01-01 --end 2024-01-01
bread run --mode paper               # start paper trading (15-min tick loop)
bread status                         # account equity, positions, risk status
bread journal --days 30              # trade journal
bread dashboard --port 8050          # Dash monitoring UI at localhost:8050

# Test
pytest tests/unit/                   # unit tests (no API keys needed)
pytest tests/unit/test_validators.py # single test file
pytest -m integration                # integration tests (need ALPACA_PAPER_API_KEY)

# Quality
ruff check .                         # lint
ruff format .                        # format
mypy src/                            # type check (strict mode)
```

## Architecture

### Trading Loop (`app.py` tick cycle, every 15 min via APScheduler)

1. **Reconcile** — sync local positions with broker
2. **Snapshot** — store equity/P&L to DB
3. **Evaluate** — each strategy fetches bars → computes indicators → emits `Signal` objects
4. **Execute** — SELL signals first, then BUY signals sorted by strength; risk validation before every buy
5. **Notify** — Discord/apprise alerts

### Key Module Responsibilities

- **`core/`** — Pydantic config models (YAML + .env deep merge), domain dataclasses (`Signal`, `Position`, `Order`), structured logging
- **`data/`** — `AlpacaDataProvider` fetches OHLCV bars, `BarCache` avoids re-fetching (SQLite, NYSE-holiday-aware), `indicators.py` wraps pandas-ta
- **`strategy/`** — Abstract `Strategy.evaluate(universe) → List[Signal]` interface. Registry pattern via `@register("name")` decorator. One strategy implemented: `etf_momentum`
- **`execution/`** — `ExecutionEngine` processes signals, `AlpacaBroker` submits bracket orders (market buy + OCO stop-loss/take-profit) and handles reconciliation
- **`risk/`** — **Most critical module.** `RiskManager` coordinates position sizing (fixed fractional) and a validator chain: buying power, max positions, concentration, asset class exposure, daily/weekly loss limits, max drawdown, PDT guard. Validators short-circuit on first failure
- **`backtest/`** — No-look-ahead historical replay engine with metrics (Sharpe, Sortino, max drawdown, win rate)
- **`monitoring/`** — `AlertManager` (apprise multi-channel, rate-limited), `TradeJournal` (BUY/SELL FIFO pair matching), `PnLTracker` (equity snapshots, drawdown)
- **`db/`** — SQLAlchemy ORM models: `MarketDataCache`, `OrderLog`, `SignalLog`, `PortfolioSnapshot`
- **`dashboard/`** — Dash app with portfolio overview and trade journal pages, smart refresh (30s market hours, 5min off-hours)

### Configuration Hierarchy

`config/default.yaml` (base) → `config/{paper,live}.yaml` (mode overrides) → `.env` (secrets). Config validated by Pydantic models in `core/config.py`. Live mode has stricter risk limits than paper.

### Design Patterns

- **Strategy Registry** — `@register("name")` class decorator in `strategy/registry.py`
- **Pydantic validation** everywhere for config and models
- **Single-process, in-memory** — no external message bus or cache; APScheduler for scheduling
- **SQLite** — zero-ops database at `data/bread.db` (git-ignored)

## Tool Configuration

- **ruff**: line-length 100, rules E/F/I/N/W/UP, target Python 3.11
- **mypy**: strict mode with pydantic plugin; `pandas_ta`, `alpaca.*`, `apscheduler.*` have `ignore_missing_imports`
- **pytest**: markers `integration` for tests requiring API keys
