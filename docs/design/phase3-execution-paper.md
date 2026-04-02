# Phase 3: Execution + Paper Trading (Week 4)

## Goal

Build the execution engine, risk management, and application orchestrator. Connect everything to Alpaca paper trading. This phase produces a fully operational paper trading bot that runs on a schedule, evaluates strategies, validates signals through risk checks, and submits bracket orders.

---

## Scope

### 3.1 Risk Management (`risk/`)

**This is the most critical module — errors here lose real money.**

- **`risk/position_sizer.py`** — Fixed fractional sizing: `position = (equity x risk_pct) / stop_loss_pct`. Default: 0.5% risk per trade. Conservative sizing aligned with 20% annual target.

- **`risk/limits.py`** — Hard limits and circuit breakers:

  | Limit | Default | Action |
  |-------|---------|--------|
  | Max positions | 5 | Reject new entries |
  | Max single position | 20% of equity | Cap position size |
  | Max sector exposure | 40% of equity | Reject if exceeded |
  | Max daily loss | 1.5% of equity | Halt trading for the day |
  | Max weekly loss | 3% of equity | Halt + alert |
  | Max drawdown from peak | 7% of equity | Halt all trading, require manual restart |
  | PDT guard | 3 day trades / 5 days | Block 4th day trade (account < $25K) |

- **`risk/validators.py`** — Pre-trade validation chain. Every signal passes through before becoming an order:
  1. Buying power check
  2. Position limit check
  3. Concentration check
  4. Drawdown check
  5. PDT check
  6. Spread/liquidity check
  7. Volatility check
  - Rejection logged with reason. No silent drops.

- **`risk/manager.py`** — Risk management engine orchestrating position sizing, limits, and validators.

### 3.2 Execution Engine (`execution/`)

- **`execution/alpaca_broker.py`** — Wraps `alpaca-py` `TradingClient`. Paper/live controlled by `paper=True/False`. Always uses bracket orders for automatic stop-loss/take-profit.

- **`execution/engine.py`** — Order management: submit orders, track fills, reconcile positions with broker state on every tick. Emit events for monitoring. Idempotent — safe to call multiple times.

### 3.3 Application Orchestrator (`app.py`)

- `APScheduler` fires `tick()` every 15 minutes during market hours (9:30 AM - 4:00 PM ET)
- **Tick cycle:** Refresh data → Evaluate strategies → Risk-check signals → Execute orders → Update monitoring

### 3.4 CLI (`__main__.py`)

- `bread run --mode paper` — start the paper trading bot
- `bread status` — show current portfolio and P&L

### 3.5 Paper → Live Switching

- Controlled by single config value
- Live mode reads `config/live.yaml`, applies stricter risk limits, requires typing "CONFIRM" on startup

---

## Verification Criteria

All checks must pass before moving to Phase 4.

### Unit Tests

1. **Position sizer** — given equity=$10K, risk_pct=0.5%, stop_loss_pct=5%, position size = $1,000
2. **Limits — max positions** — 5 positions held → 6th signal rejected with reason "max positions exceeded"
3. **Limits — daily loss** — simulate 1.5% equity loss → trading halted; new signals rejected with "daily loss limit"
4. **Limits — PDT guard** — 3 day trades in 5 days → 4th blocked; accounts >= $25K not blocked
5. **Limits — max drawdown** — 7% drawdown from peak → all trading halted
6. **Validator chain** — signal passes all 7 validators → approved; signal fails any validator → rejected with specific reason
7. **Execution engine** — order submitted → tracked; fill received → position updated; reconciliation corrects drift

### Integration Tests (Alpaca Paper)

1. **Submit bracket order** — submit a bracket order (buy SPY, stop-loss, take-profit) to Alpaca paper → order appears in Alpaca paper dashboard
2. **Position reconciliation** — manually create a position in paper account → engine reconciles and tracks it
3. **Full tick cycle** — trigger a tick → data refreshed, strategy evaluated, risk-checked, order submitted (or correctly rejected)

### End-to-End Test

1. `python -m bread run --mode paper` — starts without error
2. Scheduler fires `tick()` at correct intervals during market hours
3. Let run for 1+ market day — verify:
   - Logs show tick cycles executing
   - If signals generated: orders appear in Alpaca paper dashboard with bracket (stop-loss) attached
   - If no signals: logs show "no signals" or rejection reasons
   - No unhandled exceptions

### Manual Checks

1. `ruff check src/` — clean
2. `mypy src/` — clean
3. Alpaca paper dashboard shows any submitted orders with correct bracket structure
4. Risk rejection logs are clear and include specific reasons
