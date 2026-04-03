# Phase 5: Dashboard (Week 6)

## Goal

Build a web-based monitoring dashboard using Dash so the trading bot can be supervised visually during paper trading and beyond. The dashboard is read-only — it does not execute trades or modify configuration.

---

## Scope

### 5.1 Dashboard Framework

- Dash 3.x with Bootstrap 5 dark theme (DARKLY)
- AG Grid for sortable, filterable financial tables
- Plotly charts with dark template
- Smart auto-refresh: 30s during market hours, 5min off-hours
- Standalone CLI command: `bread dashboard --port 8050 --debug`

### 5.2 Portfolio Overview Page (`/`)

- **KPI cards** — Equity, daily P&L ($ and %), buying power, drawdown from peak
- **Equity curve** — 90-day line chart with green fill (from `PortfolioSnapshot`)
- **Drawdown chart** — Red area chart showing rolling drawdown
- **Open positions table** — AG Grid: symbol, qty, entry price, current price, P&L, market value
- **Open orders table** — AG Grid: symbol, side, qty, type, status, submitted time
- **Connection indicator** — Green dot "Connected" / orange "API unavailable"

### 5.3 Trade Journal Page (`/trades`)

- **Filters** — Strategy dropdown, symbol text input (debounced), lookback slider (7-365 days), P&L period toggle (daily/weekly/monthly)
- **Summary KPI cards** — Total P&L, win rate, expectancy, trade count
- **P&L bar chart** — Green/red bars by selected period
- **Journal table** — Paginated AG Grid: date, symbol, qty, entry/exit prices, P&L, hold days, strategy, exit reason

### 5.4 Data Access Layer

- `DashboardData` class wrapping broker APIs and SQLite database
- Graceful degradation when broker unavailable (returns empty defaults, DB data still shown)
- Methods: `get_account_summary()`, `get_positions()`, `get_open_orders()`, `get_equity_curve()`, `get_drawdown_series()`, `get_period_pnl()`, `get_journal()`, `get_journal_summary()`

---

## Deferred to Future Phases

- **Backtest Explorer page** (`/backtest`) — Candlestick charts with entry/exit markers, TradingView integration, interactive backtest runner
- **Settings page** (`/settings`) — Config viewer/editor with Pydantic validation
- **WebSocket push** (`dash-socketio`) — Real-time trade fill events (polling-only for now)
- **Exposure breakdown** — Sector allocation pie/bar chart on Portfolio page
- **Trade detail panel** — Click-through with entry/exit reasoning and chart context

---

## Verification Criteria

1. `bread dashboard` serves on `:8050` without errors
2. Portfolio page shows live account data when broker is connected
3. Portfolio page degrades gracefully when broker is unavailable (shows cached DB data)
4. Trade journal page filters and displays completed trades correctly
5. Smart refresh interval switches between market/off-hours rates
6. Connection status indicator accurately reflects broker connectivity
7. 26 unit tests pass covering components, data layer, and graceful degradation
