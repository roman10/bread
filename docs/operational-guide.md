# Bread Trading Bot — Operational Guide

## Quick Reference

| What | Command / Location |
|------|-------------------|
| Start bot | `scripts/bread-launcher.sh start` |
| Stop bot | `scripts/bread-launcher.sh stop` |
| Bot status | `scripts/bread-launcher.sh status` |
| Account status | `bread status` |
| Trade journal | `bread journal --days 30` |
| Dashboard | `http://localhost:8050` |
| Tail logs | `scripts/bread-launcher.sh logs` |
| Log files | `logs/bread-YYYY-MM-DD.log` |
| Config (base) | `config/default.yaml` |
| Config (paper) | `config/paper.yaml` (overrides base) |
| Config (live) | `config/live.yaml` (overrides base) |
| Secrets | `.env` (API keys) |
| Alpaca paper dashboard | `https://app.alpaca.markets/paper/dashboard/overview` |

---

## 1. Pre-Flight Checklist

Run before starting the first paper trading campaign:

- [ ] `.env` contains `ALPACA_PAPER_API_KEY` and `ALPACA_PAPER_SECRET_KEY`
- [ ] `bread db init` — initialize SQLite database (auto-creates `data/bread.db`)
- [ ] `bread status` — confirms API connectivity (shows equity, cash, buying power)
- [ ] `bread fetch SPY` — confirms data pipeline (fetches bars + computes indicators)
- [ ] Discord webhook in `config/paper.yaml` is valid (send a test message)
- [ ] `pytest tests/unit/ -q` — all tests pass
- [ ] Mac lid open + AC power connected (required for `caffeinate` sleep prevention)
- [ ] System timezone matches `America/New_York` (pmset schedule times are local)

---

## 2. Starting the Bot

### Manual Start (recommended for first session)

```bash
scripts/bread-launcher.sh start
```

This runs `bread run --mode paper --dashboard` by default. The launcher:
1. Starts the bot in a background restart-loop wrapper
2. Attaches `caffeinate -s` to prevent system sleep on AC power
3. Saves PID to `logs/bread.pid`

**Custom args** (e.g., no dashboard):
```bash
scripts/bread-launcher.sh start -- --mode paper --no-dashboard
```

**Verify startup:**
- `scripts/bread-launcher.sh status` — shows RUNNING with PID and uptime
- `bread status` — shows equity, positions, risk status
- `http://localhost:8050` — dashboard loads, connection dot is green

### What Happens at Startup

1. Config loaded: `default.yaml` + `paper.yaml` merged, `.env` secrets injected
2. Database auto-initialized
3. Broker and data provider initialized (Alpaca paper API)
4. Risk manager initialized with 8 validators
5. 9 strategies loaded (claude_analyst disabled by default)
6. Alert manager initialized (Discord)
7. **Initial reconciliation** — syncs with Alpaca positions before any trading
8. Scheduler starts:
   - **Trading tick**: every 15 min, Mon-Fri 9:00-15:59 ET
   - **Daily summary**: Mon-Fri 4:05 PM ET
   - **Research scan**: every 4 hours (if Claude AI enabled)

### Scheduled Wake/Sleep (for unattended operation)

```bash
scripts/bread-launcher.sh schedule    # requires sudo
```

Sets macOS to wake at 9:25 AM and sleep at 4:10 PM, Mon-Fri. The bot must be started separately — this only controls wake/sleep.

Remove schedule: `scripts/bread-launcher.sh unschedule`

---

## 3. Stopping the Bot

```bash
scripts/bread-launcher.sh stop
```

This sends SIGTERM to the bot. The shutdown handler waits for the current tick to complete, then stops the scheduler. If the bot hasn't exited after 30 seconds, SIGKILL is sent.

### What Happens to Open Positions

**Bracket orders persist on Alpaca servers.** When you stop the bot:
- Open positions keep their stop-loss and take-profit orders active on Alpaca
- Positions are protected even with the bot offline
- This is the single most important safety feature — positions don't "drift" if the bot crashes

**Verify after stopping:** Check the Alpaca dashboard — open orders should still show bracket legs (stop-loss and take-profit).

### Emergency Stop

1. `scripts/bread-launcher.sh stop` (graceful)
2. If that hangs: `kill -9 $(cat logs/bread.pid)`
3. Verify all positions have bracket orders on Alpaca dashboard
4. To close ALL positions immediately: use Alpaca dashboard "Close All Positions"

---

## 4. The Tick Cycle

Every 15 minutes during market hours (9:00 AM - 3:59 PM ET, Mon-Fri):

| Step | What Happens | Log Pattern |
|------|-------------|-------------|
| 1. Reconcile | Sync local positions with broker. Detect bracket fills. Cancel stale orders (>30 min pending). | `Reconciliation: position X no longer on broker` |
| 2. Snapshot | Record equity, cash, daily P&L to database. Paper mode adjusts for simulated slippage (0.1%). | — |
| 3. Data fetch | Batch-fetch daily bars for all strategy universes. Compute indicators (SMA, EMA, RSI, MACD, ATR, Bollinger). | `Failed to compute indicators for X` (error) |
| 4. Strategy eval | Each of 9 strategies evaluates its universe, emits BUY/SELL signals. Signals logged to DB. | — |
| 5. Execute | SELLs first (close positions). BUYs sorted by strength, passed through risk validation chain, bracket orders submitted for approved buys. | `BUY X rejected: ...` |
| 6. Alert | Trade notifications sent for new/closed positions. | — |

Normal tick log output: `Tick started` ... `Tick complete: signals=N positions=N`

---

## 5. Monitoring During Paper Trading

### Tools

- **`bread status`** — Equity, cash, buying power, daily P&L, drawdown from peak, open positions with unrealized P&L, risk limit usage, open orders
- **Dashboard** (`localhost:8050`) — Portfolio page (equity curve, drawdown chart) + Trades page (trade history, signal log)
- **`bread journal --days N`** — Completed round-trip trades with entry/exit prices, P&L, holding period
- **`scripts/bread-launcher.sh logs`** — Tail today's log file
- **Discord alerts** — Trade executions, daily summary (4:05 PM ET), risk breaches, errors

### Key Log Patterns

| Pattern | Meaning | Action |
|---------|---------|--------|
| `Tick started` / `Tick complete` | Normal tick cycle | None |
| `Reconciliation: position X no longer on broker (bracket triggered)` | Stop-loss or take-profit filled while bot was running or offline | Review the trade in journal |
| `Reconciliation: found untracked position X` | Alpaca has a position the bot didn't track | Investigate — may be from manual trade or bot restart |
| `BUY X rejected: insufficient buying power` | Risk check blocked a trade | Normal if capital is deployed |
| `BUY X rejected: drawdown X% exceeds limit 7%` | Drawdown circuit breaker active | See Max-Drawdown Halt Recovery |
| `Stale order X for Y (age > 30 min) -- cancelling` | Order stuck too long, auto-cleaned | Review why order didn't fill |
| `Missed scheduled job: trading_tick` | Scheduler couldn't fire on time | Auto-recovery tick runs if market is open |
| `FATAL: 5 consecutive crashes` | Auto-restart exhausted | Stop bot, check logs, fix root cause |

### Alert Types

| Alert | When | Priority |
|-------|------|----------|
| Trade | BUY/SELL executed | Normal |
| Daily Summary | 4:05 PM ET each trading day | Normal |
| Risk Breach | Daily/weekly loss or drawdown limit hit | High |
| Error | Unhandled exception in tick cycle | Critical |

---

## 6. Manual Intervention Procedures

### Cancel a Specific Order

1. Open Alpaca dashboard > Orders tab
2. Find the order, click Cancel
3. Note: cancelling a bracket parent cancels its OCO legs (stop-loss + take-profit) too
4. The bot detects the change on the next tick's reconciliation

### Close a Specific Position

1. Open Alpaca dashboard > Positions tab
2. Click "Close" on the position
3. This submits a market sell and automatically cancels associated bracket OCO orders
4. The bot detects the closed position on the next tick's reconciliation

### Close All Positions (Emergency)

1. Alpaca dashboard > "Close All Positions"
2. Stop the bot: `scripts/bread-launcher.sh stop`
3. Verify: 0 positions, 0 open orders on Alpaca dashboard

### Prevent New Trades Without Closing Existing Ones

Stop the bot. Bracket orders remain active on Alpaca, protecting all open positions. No new trades will be opened. Restart when ready to resume.

---

## 7. Restart Procedure

1. Stop: `scripts/bread-launcher.sh stop`
2. (Optional) Verify positions and bracket orders on Alpaca dashboard
3. Start: `scripts/bread-launcher.sh start`
4. The bot runs initial reconciliation before the scheduler starts:
   - Positions on Alpaca that the bot doesn't know about are added with `strategy_name="unknown"`
   - Positions the bot expected but Alpaca no longer has are removed (bracket triggered while offline)
   - Pending/accepted orders are updated to their actual fill status
5. Verify: `bread status` should match Alpaca dashboard

**Known limitation:** Reconciled positions after a restart have `strategy_name="unknown"` and `stop_loss_price=0.0` because the bot has no memory of the original signal. The bracket orders on Alpaca still protect the position — this is cosmetic only. The `bread journal` will show these as "unknown" strategy.

---

## 8. Auto-Restart Behavior

The launcher script wraps the bot in a restart loop:

- On crash (non-zero exit): waits 10 seconds, then restarts
- Consecutive crash counter resets on any clean run
- After 5 consecutive crashes: gives up with `FATAL` message
- Recovery: diagnose the root cause from logs, then `scripts/bread-launcher.sh start`

---

## 9. Max-Drawdown Halt Recovery

### What Triggers It

The drawdown circuit breaker fires when `(peak_equity - current_equity) / peak_equity >= 7%` (paper mode). Peak equity is the maximum equity ever recorded in the portfolio snapshots table.

### What Happens

- **All BUY signals are rejected** with reason: `drawdown X.XX% exceeds limit 7%`
- SELL signals still process normally (existing positions can be closed)
- Bracket orders on Alpaca still function (stop-loss and take-profit can trigger)
- The bot keeps running, ticking, and reconciling — it just won't open new positions
- Daily loss (1.5%) and weekly loss (3%) circuit breakers work the same way

### How to Resume

- The drawdown check compares peak equity vs current equity. There is no manual reset.
- Trading resumes automatically when equity recovers enough that the drawdown is below the limit
- Daily loss limit resets the next trading day (Alpaca's `last_equity` updates overnight)
- Weekly loss limit resets on Monday (computed from the earliest snapshot of the current week)

### If the Strategy Is Genuinely Failing

- Review trades in `bread journal` to understand why
- Consider Phase 6.3 parameter tuning (entry/exit thresholds, stop-loss ATR multiplier)
- Re-run backtests with tuned parameters to confirm improvement before resuming

---

## 10. Configuration Reference

Commonly adjusted parameters:

| Parameter | File | Default | Notes |
|-----------|------|---------|-------|
| `risk.risk_pct_per_trade` | `default.yaml` | 0.005 (0.5%) | Capital risked per trade |
| `risk.max_positions` | `default.yaml` | 5 | Maximum simultaneous positions |
| `risk.max_position_pct` | `default.yaml` | 0.20 (20%) | Max single position as % of equity |
| `risk.max_daily_loss_pct` | `default.yaml` | 0.015 (1.5%) | Daily loss circuit breaker |
| `risk.max_weekly_loss_pct` | `default.yaml` | 0.03 (3%) | Weekly loss circuit breaker |
| `risk.max_drawdown_pct` | `default.yaml` | 0.07 (7%) | Max drawdown circuit breaker |
| `execution.tick_interval_minutes` | `default.yaml` | 15 | Minutes between strategy evaluations |
| `execution.take_profit_ratio` | `default.yaml` | 2.0 | Take-profit as multiple of stop-loss distance |
| `execution.paper_cost.slippage_pct` | `default.yaml` | 0.001 (0.1%) | Simulated fill slippage in paper mode |
| `alerts.enabled` | `paper.yaml` | true | Enable/disable Discord alerts |

Config merge order: `default.yaml` -> `{paper,live}.yaml` (deep merge) -> `.env` (secrets)

Strategy-specific parameters are in `config/strategies/{name}.yaml`.

---

## 11. Troubleshooting

| Issue | Cause | Fix |
|-------|-------|-----|
| `Missing API credentials for paper mode` | `.env` not found or keys not set | Create `.env` with `ALPACA_PAPER_API_KEY` and `ALPACA_PAPER_SECRET_KEY` |
| No ticks firing during market hours | Market holiday, or scheduler not running | Check `scripts/bread-launcher.sh status`. Check NYSE calendar. Check logs for `Tick started`. |
| Stale PID file prevents start | Bot crashed without cleanup | `scripts/bread-launcher.sh stop` cleans it up, or `rm logs/bread.pid` |
| Dashboard won't start (port in use) | Previous instance still running | `lsof -i :8050` to find the process, kill it |
| Discord alerts not delivering | Webhook URL invalid or rate-limited | Verify URL in `config/paper.yaml`. Rate limit is 60s between same alert type. |
| Mac sleeping during market hours | Lid closed, or not on AC power | Keep lid open + AC connected. Verify: `scripts/bread-launcher.sh status` shows caffeinate active. |
| `bread status` shows different equity than Alpaca | Paper cost model applies 0.1% slippage adjustment | Normal — small difference expected in paper mode |
