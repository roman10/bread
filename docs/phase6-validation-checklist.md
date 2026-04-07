# Phase 6 Validation Checklist

## Campaign Parameters

| Parameter | Value |
|-----------|-------|
| Start date | ________ |
| Planned end date | ________ (minimum 2 weeks, target 4 weeks) |
| Starting paper equity | $________ |
| Strategies enabled | etf_momentum, bb_mean_reversion, macd_trend, ema_crossover, sector_rotation, risk_off_rotation, breakout_squeeze, macd_divergence, gap_fade |
| Risk limits | daily 1.5%, weekly 3%, drawdown 7%, max 5 positions |
| Tick interval | 15 min, 9:00-15:59 ET Mon-Fri |
| Paper cost model | slippage 0.1%, commission $0 |

### Backtest Benchmarks (fill from latest backtest run)

| Metric | Backtest Value |
|--------|---------------|
| Win rate | ____% |
| Avg holding period | ____ days |
| Max drawdown | ____% |
| Monthly return estimate | ____% |
| Profit factor | ____ |

---

## Daily Monitoring Checklist

Copy this section for each trading day.

### Morning (within 30 min of 9:30 AM ET)

- [ ] `scripts/bread-launcher.sh status` — shows RUNNING, caffeinate active
- [ ] `bread status` — equity matches Alpaca dashboard
- [ ] Check log: `grep "Tick started" logs/bread-$(date +%Y-%m-%d).log` — first tick at ~9:00 AM ET
- [ ] If positions held overnight: verify bracket orders still exist on Alpaca dashboard
- [ ] No error alerts received since last check

### End of Day (after 4:05 PM ET)

- [ ] Daily summary alert received on Discord
- [ ] `bread status` — review equity, daily P&L, position count
- [ ] `bread journal --days 1` — review any trades that executed today
- [ ] For each trade: entry/exit matches strategy rules? (manual spot-check)
- [ ] Risk status: daily loss within limit, drawdown within limit
- [ ] `grep -cE "WARNING|ERROR" logs/bread-$(date +%Y-%m-%d).log` — note any issues
- [ ] Anomalies recorded in Observation Log below

**Time required:** 5-10 min on quiet days, 15-20 min if trades executed.

---

## Weekly Review Checklist

Perform every Friday after market close.

- [ ] Total trades this week: ____
- [ ] Win rate this week: ____% (from `bread journal --days 7`)
- [ ] Weekly P&L: $____ (____%)
- [ ] Max drawdown this week: ____%
- [ ] **Paper vs backtest comparison:**
  - Paper win rate vs backtest: ____ vs ____
  - Paper avg holding period vs backtest: ____ vs ____
  - Paper P&L trajectory: on track / below / above
- [ ] Review rejected signals: `grep "rejected" logs/bread-*.log | tail -20` — are rejections appropriate?
- [ ] Risk limits hit this week? Which ones?
- [ ] All 5 trading days had ticks? (check each day's log for `Tick started`)
- [ ] No missed scheduled jobs? `grep "Missed scheduled job" logs/bread-*.log`
- [ ] API errors with successful recovery? `grep "Failed" logs/bread-*.log`

---

## Verification Criteria Tracker

All criteria must pass before proceeding to Phase 7 (Go Live).

### System Reliability

| # | Criterion | How to Verify | Pass? | Date | Notes |
|---|-----------|---------------|-------|------|-------|
| SR1 | Bot ran 2+ weeks without unhandled crashes | `grep "FATAL\|unhandled" logs/bread-*.log` — zero results. Launcher never hit 5-crash limit. | | | |
| SR2 | All market days had successful tick cycles | For each trading day, confirm `Tick started` entries in log. Expect ~28 ticks/day (every 15 min, 9:00-15:45). | | | |
| SR3 | Scheduler fired on time | `grep "Missed scheduled job" logs/bread-*.log` — zero results. | | | |
| SR4 | Bot recovered from API errors | `grep -E "retry\|Failed to fetch" logs/bread-*.log`. If any occurred, verify subsequent ticks succeeded. | | | |
| SR5 | Bracket orders survived bot restart | **Test required**: stop bot while holding positions, verify bracket orders on Alpaca dashboard, restart, verify `bread status` shows correct positions. | | | |

### Trading Behavior

| # | Criterion | How to Verify | Pass? | Date | Notes |
|---|-----------|---------------|-------|------|-------|
| TB1 | Trades match strategy rules | For at least 5 trades across different strategies: review signal in `bread journal`, verify entry conditions against strategy config. | | | |
| TB2 | Risk limits respected | No position exceeded 20% of equity. No more than 5 concurrent positions. Daily/weekly/drawdown limits never breached. Check via `bread status` and logs. | | | |
| TB3 | PDT guard works | If equity < $25K: verify no 4th day trade in any 5-day window. `grep "PDT" logs/bread-*.log` for rejections. | | | |
| TB4 | No phantom trades | Every order in `bread journal` matches Alpaca order history. Every filled order on Alpaca appears in journal. Cross-reference at least 5 trades. | | | |

### Performance Sanity

| # | Criterion | How to Verify | Pass? | Date | Notes |
|---|-----------|---------------|-------|------|-------|
| PS1 | P&L in range of backtest expectations | Compare campaign P&L % vs backtest P&L % for same period length. "Same ballpark" = within 2x. | | | |
| PS2 | Win rate and holding period match backtest | Win rate within +/- 15 percentage points. Holding period within +/- 50%. | | | |
| PS3 | Max drawdown within limits | Peak-to-trough from dashboard equity curve never exceeded 7%. | | | |
| PS4 | No obviously broken trades | Review worst 3 trades. Were stop-losses reasonable? Were entries valid? No "buy then immediately hit stop" patterns? | | | |

### Operational Readiness

| # | Criterion | How to Verify | Pass? | Date | Notes |
|---|-----------|---------------|-------|------|-------|
| OR1 | Alerts delivered reliably | Daily summary alert received every trading day. Trade alerts received for executed trades. Check for gaps. | | | |
| OR2 | `bread status` is accurate | At least 3 times during campaign: compare `bread status` output to Alpaca dashboard. Equity, positions, and orders should match (small paper cost difference is OK). | | | |
| OR3 | Manual intervention tested | **Test required**: cancel an order via Alpaca dashboard, verify bot detects on next tick. Close a position via Alpaca dashboard, verify reconciliation. | | | |
| OR4 | Restart procedure tested | **Test required**: `bread-launcher.sh stop` then `bread-launcher.sh start`. Verify positions reconcile. Verify bracket orders still active on Alpaca. See Operational Guide section 7. | | | |

---

## Observation and Bug Log

Record anomalies, bugs, and questions as they arise.

**Categories:** `bug` | `anomaly` | `question` | `tuning` | `infra`
**Severity:** `critical` (wrong trades, data loss) | `high` (missed trades, reliability) | `medium` (cosmetic) | `low` (nice-to-have)

| Date | Time | Category | Description | Severity | Action Taken | Resolved? |
|------|------|----------|-------------|----------|--------------|-----------|
| | | | | | | |
| | | | | | | |
| | | | | | | |

---

## Parameter Tuning Log

Track any parameter changes made during Phase 6.3.

| Date | Parameter | Old Value | New Value | Rationale | Backtest Before | Backtest After |
|------|-----------|-----------|-----------|-----------|-----------------|----------------|
| | | | | | | |
| | | | | | | |

Parameters most likely to need tuning:
- `risk.risk_pct_per_trade` (currently 0.5%)
- `execution.take_profit_ratio` (currently 2.0x stop-loss)
- Per-strategy `atr_stop_mult` in `config/strategies/*.yaml`
- Per-strategy entry/exit RSI thresholds

---

## Go/No-Go Decision

Fill in at the end of the paper trading campaign.

| Item | Value |
|------|-------|
| Campaign dates | ____ to ____ |
| Total trading days | ____ |
| System Reliability criteria passed? | ____ / 5 |
| Trading Behavior criteria passed? | ____ / 4 |
| Performance Sanity criteria passed? | ____ / 4 |
| Operational Readiness criteria passed? | ____ / 4 |
| Open bugs at campaign end | ____ (list critical/high) |
| **Decision** | PROCEED to Phase 7 / EXTEND campaign / PAUSE for fixes |
| Rationale | |
| Decision date | |
