# Phase 4: Monitoring (Week 5)

## Goal

Add trade journaling, P&L tracking, and alerting. This phase completes the operational visibility layer so that the paper trading bot can be monitored and evaluated without manually checking the Alpaca dashboard.

---

## Scope

### 4.1 Trade Journal (`monitoring/journal.py`)

- Every trade logged to SQLite with:
  - Entry date, exit date
  - Entry price, exit price
  - P&L (absolute and percentage)
  - Strategy that generated the trade
  - Risk metrics at entry (position size, stop-loss level, account equity)
  - Entry/exit reasons (which signals triggered)
- Queryable by date range, strategy, symbol, P&L

### 4.2 P&L Tracker (`monitoring/tracker.py`)

- Daily, weekly, monthly P&L aggregation
- Running win rate
- Rolling Sharpe ratio
- Current and max drawdown
- Portfolio exposure (% invested vs cash)
- All metrics stored in `portfolio_snapshots` table

### 4.3 Alerts (`monitoring/alerts.py`)

- Via `apprise` library (supports Discord, email, Slack, etc.)
- Alert levels and triggers:

  | Event | Priority | Example |
  |-------|----------|---------|
  | Trade executed | Normal | "Bought 50 SPY @ $450.25, stop @ $443.50" |
  | Daily P&L summary | Normal | "Daily P&L: +$127.50 (+1.3%), 2 trades, 1W/1L" |
  | Loss limit hit | High | "Daily loss limit reached (1.5%). Trading halted." |
  | Max drawdown breached | Critical | "Max drawdown 7% breached. All trading halted." |
  | System error | Critical | "Unhandled exception in tick cycle: ..." |

- Alert destinations configured in YAML
- Rate limiting to prevent alert storms

### 4.4 CLI Extensions

- `bread status` enhanced with:
  - Current positions with unrealized P&L
  - Today's P&L
  - Open orders
  - Risk status (limits remaining)
- `bread journal` — display recent trade journal entries

---

## Verification Criteria

All checks must pass before moving to Phase 5.

### Unit Tests

1. **Trade journal** — log a trade → retrieve it with correct fields; query by date range returns correct subset; query by strategy filters correctly
2. **P&L tracker** — given a sequence of portfolio snapshots, daily/weekly/monthly aggregations are correct; drawdown calculation matches hand-computed value
3. **Alerts** — mock apprise → trade event triggers normal-priority alert with correct message format; loss limit triggers high-priority alert; rate limiter suppresses duplicate alerts within window

### Integration Tests

1. **Journal + execution** — execute a paper trade → journal entry automatically created with correct entry details
2. **P&L tracker + database** — run tracker update → `portfolio_snapshots` table populated with current metrics
3. **Alert delivery** — configure a test Discord webhook (or mock) → trigger a trade event → alert delivered with correct content

### End-to-End Test

1. Run paper trading bot for 1+ market day with monitoring enabled
2. Verify:
   - Trade journal entries appear for any executed trades
   - `bread status` displays current portfolio state accurately
   - `bread journal` displays trade history
   - Daily P&L summary alert sent at market close
   - If loss limit hit during the day: high-priority alert sent

### Manual Checks

1. `ruff check src/` — clean
2. `mypy src/` — clean
3. Discord/email alerts received and readable
4. `bread status` output matches Alpaca paper dashboard
