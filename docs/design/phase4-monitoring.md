# Phase 4: Monitoring (Week 5)

## Goal

Add trade journaling, P&L tracking, and alerting. This phase completes the operational visibility layer so that the paper trading bot can be monitored and evaluated without manually checking the Alpaca dashboard.

---

## Implementation Readiness

**Status:** Ready for implementation.

### Prerequisites (all met)

- `OrderLog` table records all order submissions with `filled_price` and `filled_at_utc` columns (currently always null — Phase 4 adds fill reconciliation)
- `PortfolioSnapshot` table populated every tick by `ExecutionEngine.save_snapshot()`
- `bread status` CLI command exists with basic account + position display
- Execution engine has `_get_peak_equity()`, `_get_weekly_pnl()`, `_get_day_trade_count()` helpers

### What's new vs what exists

| Capability | Exists (Phase 3) | New (Phase 4) |
|-----------|-------------------|---------------|
| Portfolio snapshots | `save_snapshot()` writes equity/cash/positions per tick | No change — already complete |
| Peak equity | `_get_peak_equity()` queries MAX(equity) | No change |
| Weekly P&L | `_get_weekly_pnl()` computes from snapshots | No change |
| Order logging | `_log_order()` writes on submission | **Fill reconciliation** — update `filled_price`, `filled_at_utc`, `status` from broker |
| Trade journal | Not implemented | **New** — query layer on `OrderLog` for completed round-trips |
| P&L aggregation | Daily P&L from Alpaca account only | **New** — daily/weekly/monthly aggregation from snapshots |
| Alerts | Not implemented | **New** — apprise-based notifications for trade events and risk breaches |
| CLI journal | Not implemented | **New** — `bread journal` command |
| CLI status | Basic account + positions | **Enhanced** — add risk status and open orders |

### Deferred

- **Signal persistence to `signals_log`** — still deferred. Journal shows order `reason` field which captures the signal's explanation. Full signal logging adds complexity with minimal value at this stage.
- **Real-time WebSocket trade fill events** — deferred to Phase 5 (dashboard). Polling in `reconcile()` is sufficient for the 15-minute tick cycle.

---

## New Dependencies

Add to `pyproject.toml`:

```
"apprise>=1.7",
```

---

## Scope

### 4.0 Foundation: Fill Reconciliation

**Problem:** `_log_order()` creates OrderLog entries with status=PENDING at submission time. `filled_price` and `filled_at_utc` are always null. The journal and P&L tracker need fill data.

**Solution:** Extend `reconcile()` in `ExecutionEngine` to update order fill status from the broker.

#### Changes to `execution/engine.py`

Add a `_reconcile_orders()` method called from `reconcile()`:

```python
def _reconcile_orders(self) -> None:
    """Update pending/accepted orders with fill status from broker."""
    with self._session_factory() as session:
        pending = session.execute(
            select(OrderLog).where(
                OrderLog.status.in_(["PENDING", "ACCEPTED"])
            )
        ).scalars().all()

        if not pending:
            return

        try:
            broker_orders = self._broker.get_orders(status="all")
        except Exception:
            logger.exception("Failed to fetch orders for reconciliation")
            return

        broker_map = {o.id: o for o in broker_orders}

        for order in pending:
            if not order.broker_order_id:
                continue
            broker_order = broker_map.get(order.broker_order_id)
            if broker_order is None:
                continue

            new_status = str(broker_order.status).upper()
            if new_status == order.status:
                continue

            order.status = new_status
            if new_status == "FILLED":
                order.filled_price = float(broker_order.filled_avg_price or 0)
                order.filled_at_utc = broker_order.filled_at
                logger.info(
                    "Order %s filled: %s %s @ %.2f",
                    order.broker_order_id, order.side, order.symbol,
                    order.filled_price,
                )
            elif new_status in ("CANCELLED", "REJECTED"):
                logger.info(
                    "Order %s %s: %s %s",
                    order.broker_order_id, new_status.lower(),
                    order.side, order.symbol,
                )

        session.commit()
```

Call `_reconcile_orders()` at the end of `reconcile()`:

```python
def reconcile(self) -> None:
    # ... existing position reconciliation ...
    self._reconcile_orders()
```

#### Changes to `execution/alpaca_broker.py`

The existing `get_orders(status="open")` needs to support `status="all"` for fetching filled/cancelled orders. Verify the Alpaca API supports this — `GetOrdersRequest(status=QueryOrderStatus.ALL)` returns all orders. No code change needed if the current implementation passes the status string through. If it only supports `"open"`, add `"all"` and `"closed"` as valid values.

---

### 4.1 Trade Journal (`monitoring/journal.py`)

The journal is a **query layer** on the existing `OrderLog` table, not a new table. A "trade" is a completed round-trip: a BUY fill followed by a SELL fill (or close) for the same symbol.

```python
@dataclass(frozen=True)
class JournalEntry:
    symbol: str
    strategy_name: str
    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float
    qty: int
    pnl: float
    pnl_pct: float
    hold_days: int  # calendar days
    entry_reason: str
    exit_reason: str


def get_journal(
    session: Session,
    *,
    start: date | None = None,
    end: date | None = None,
    strategy: str | None = None,
    symbol: str | None = None,
    limit: int = 100,
) -> list[JournalEntry]:
    """Query completed round-trip trades from OrderLog.

    Pairs BUY fills with subsequent SELL fills for the same symbol+strategy.
    Returns entries sorted by exit_date descending (most recent first).
    """
```

**Pairing logic:**

1. Query all FILLED orders, sorted by `filled_at_utc` ascending.
2. For each BUY fill, find the next SELL fill for the same `(symbol, strategy_name)`.
3. Compute `pnl = (exit_price - entry_price) * qty`.
4. Compute `pnl_pct = (exit_price - entry_price) / entry_price * 100`.
5. Compute `hold_days = (exit_date - entry_date).days`.
6. Unpaired BUY fills (still open) are excluded from journal results.
7. Apply optional filters (`start`/`end` on exit_date, `strategy`, `symbol`).

**Edge cases:**

- Partial fills: use `filled_price` as-is (Alpaca provides average fill price).
- Position added to (multiple BUYs before SELL): pair each BUY with its proportional share of the SELL. For Phase 4, simplify: pair the most recent unmatched BUY with the SELL.
- Orphan SELL without preceding BUY (e.g., position from before bot started, recovered via reconciliation): skip, log DEBUG.

```python
def get_journal_summary(entries: list[JournalEntry]) -> dict[str, float | int]:
    """Compute summary stats from journal entries.

    Returns:
        win_rate_pct, avg_win, avg_loss, expectancy,
        total_pnl, total_trades, best_trade, worst_trade
    """
```

---

### 4.2 P&L Tracker (`monitoring/tracker.py`)

Aggregates P&L from `PortfolioSnapshot` table. The snapshots are already written by `ExecutionEngine.save_snapshot()` every tick. The tracker queries and summarizes them.

```python
@dataclass(frozen=True)
class DailySummary:
    date: date
    open_equity: float
    close_equity: float
    pnl: float
    pnl_pct: float
    open_positions: int
    high_equity: float
    low_equity: float


def get_daily_summaries(
    session: Session,
    start: date | None = None,
    end: date | None = None,
) -> list[DailySummary]:
    """Aggregate snapshots into daily summaries.

    Groups snapshots by date. For each day:
    - open_equity = first snapshot of the day
    - close_equity = last snapshot of the day
    - pnl = close_equity - open_equity
    - high/low = max/min equity across all snapshots that day
    """


def get_period_pnl(
    session: Session,
    period: Literal["daily", "weekly", "monthly"],
) -> list[tuple[str, float, float]]:
    """Return (period_label, pnl, pnl_pct) tuples.

    - daily: last 30 days
    - weekly: last 12 weeks
    - monthly: last 12 months

    Each entry: (label, absolute_pnl, pnl_pct).
    """


def get_drawdown_series(
    session: Session,
) -> list[tuple[date, float]]:
    """Return (date, drawdown_pct) series from portfolio snapshots.

    Computes rolling peak equity and current drawdown at each snapshot.
    Returns one entry per day (using end-of-day snapshot).
    """
```

---

### 4.3 Alerts (`monitoring/alerts.py`)

Uses `apprise` for multi-channel notifications. All alert logic is synchronous (runs in the tick thread).

#### Config model (`core/config.py` addition)

```python
class AlertSettings(BaseModel):
    enabled: bool = False
    urls: list[str] = Field(default_factory=list)  # apprise URIs
    on_trade: bool = True
    on_daily_summary: bool = True
    on_risk_breach: bool = True
    on_error: bool = True
    rate_limit_seconds: int = Field(default=60, ge=0)  # min seconds between same-type alerts
```

Add to `AppConfig`:

```python
class AppConfig(BaseModel):
    # ... existing fields ...
    alerts: AlertSettings = Field(default_factory=AlertSettings)
```

#### Config YAML (`config/default.yaml` addition)

```yaml
alerts:
  enabled: false
  urls: []  # e.g. ["discord://webhook_id/webhook_token", "mailto://user:pass@gmail.com"]
  on_trade: true
  on_daily_summary: true
  on_risk_breach: true
  on_error: true
  rate_limit_seconds: 60
```

#### Alert manager

```python
class AlertManager:
    def __init__(self, config: AlertSettings) -> None:
        """Initialize apprise with configured notification URLs."""

    def notify_trade(self, symbol: str, side: str, qty: int, price: float,
                     reason: str) -> None:
        """Send trade execution notification (normal priority).

        Format: "BUY 50 SPY @ $450.25 — reason: rsi bounce"
        or:     "SELL 50 SPY @ $460.00 — reason: overbought"
        """

    def notify_daily_summary(self, equity: float, daily_pnl: float,
                             daily_pct: float, trades_today: int,
                             wins: int, losses: int) -> None:
        """Send end-of-day summary (normal priority).

        Format: "Daily P&L: +$127.50 (+1.3%) | 2 trades (1W/1L) | Equity: $10,234.50"
        """

    def notify_risk_breach(self, breach_type: str, details: str) -> None:
        """Send risk limit breach alert (high priority).

        breach_type: "daily_loss", "weekly_loss", "max_drawdown", "pdt"
        """

    def notify_error(self, error: str) -> None:
        """Send system error alert (critical priority).

        Includes exception type and message. Stack trace truncated to 500 chars.
        """

    def _should_send(self, alert_type: str) -> bool:
        """Rate limiting: check if enough time has passed since last alert of this type."""
```

Rate limiting: track `_last_sent: dict[str, datetime]` per alert type. If less than `rate_limit_seconds` has elapsed since the last alert of the same type, skip it and log at DEBUG.

#### Integration into tick cycle (`app.py`)

Add `_alert_manager: AlertManager | None = None` to module-level state. Initialize in `run()`.

Inject alert calls into `tick()`:

```python
def tick() -> None:
    # ... existing logic ...
    try:
        _engine.reconcile()
        _engine.save_snapshot()

        # ... data refresh + strategy evaluation ...

        _engine.process_signals(all_signals, prices)

        # NEW: alert on trades
        if _alert_manager and all_signals:
            for sig in all_signals:
                _alert_manager.notify_trade(
                    sig.symbol, sig.direction, 0, prices.get(sig.symbol, 0),
                    sig.reason,
                )

    except Exception:
        logger.exception("Tick failed")
        # NEW: alert on error
        if _alert_manager:
            import traceback
            _alert_manager.notify_error(traceback.format_exc()[-500:])
```

**Daily summary alert:** Add a separate APScheduler job that runs once at 4:05 PM ET (after market close):

```python
scheduler.add_job(
    _send_daily_summary,
    CronTrigger(
        day_of_week="mon-fri",
        hour=16, minute=5,
        timezone="America/New_York",
    ),
    id="daily_summary",
)
```

**Risk breach alerts:** Inject into `process_signals()` in the execution engine, or add a post-tick check in `app.py` that compares current drawdown/loss against limits and fires `notify_risk_breach()` when thresholds are hit.

---

### 4.4 CLI Extensions

#### `bread journal`

```
bread journal [--strategy NAME] [--symbol SYMBOL] [--days N]
```

Default: last 30 days, all strategies, all symbols.

**Required output format:**

```
Trade Journal (last 30 days)
---
DATE       SYMBOL  SIDE  QTY  ENTRY     EXIT      P&L        HOLD  STRATEGY       REASON
2026-03-15 SPY     SELL    2  $502.30   $510.15   +$15.70    5d    etf_momentum   overbought
2026-03-10 XLK     SELL    5  $198.40   $188.52   -$49.40   12d    etf_momentum   stop_loss

Summary: 2 trades | Win rate: 50.0% | Total P&L: -$33.70 | Avg hold: 8.5 days
```

- Table columns: fixed-width, left-aligned symbol/strategy, right-aligned numbers
- P&L with `+`/`-` sign prefix
- Hold days as `Nd` format
- Summary line at bottom

#### `bread status` enhancements

Add to existing output:

```
Risk Status:
  Daily loss: -$45.00 / -$150.00 (30.0% of limit)
  Weekly loss: -$120.00 / -$300.00 (40.0% of limit)
  Drawdown: 1.2% / 7.0% (17.1% of limit)
  Positions: 2 / 5
  Day trades (5d): 1 / 3

Open Orders (1):
  SPY  BUY  qty=2  bracket  status=ACCEPTED  submitted=2026-03-20 10:15
```

This requires querying:
- Risk limits from config
- Current drawdown, daily/weekly P&L from engine helpers or snapshot queries
- Day trade count from OrderLog
- Open orders from broker

---

## Implementation Order (4 steps)

### Step 1: Fill Reconciliation + Config

| File | Change |
|------|--------|
| `pyproject.toml` | Add `"apprise>=1.7"` |
| `src/bread/core/config.py` | Add `AlertSettings`, add `alerts` field to `AppConfig` |
| `config/default.yaml` | Add `alerts:` section |
| `src/bread/execution/engine.py` | Add `_reconcile_orders()`, call from `reconcile()` |
| `tests/unit/test_execution_engine.py` | Add ~4 tests for order reconciliation |

Run existing tests after to confirm nothing breaks.

### Step 2: Trade Journal + P&L Tracker

- **Create** `src/bread/monitoring/__init__.py`
- **Create** `src/bread/monitoring/journal.py` — `JournalEntry`, `get_journal()`, `get_journal_summary()`
- **Create** `src/bread/monitoring/tracker.py` — `DailySummary`, `get_daily_summaries()`, `get_period_pnl()`, `get_drawdown_series()`
- **Create** `tests/unit/test_journal.py` — ~8 tests (round-trip pairing, filters, edge cases, summary stats)
- **Create** `tests/unit/test_tracker.py` — ~6 tests (daily aggregation, period P&L, drawdown series, empty DB)

All functions are pure queries (take a session, return dataclasses). No broker calls.

### Step 3: Alerts

- **Create** `src/bread/monitoring/alerts.py` — `AlertManager` with trade, summary, risk, error notifications + rate limiting
- **Modify** `src/bread/app.py` — Initialize `AlertManager`, inject alert calls into `tick()`, add daily summary scheduler job
- **Create** `tests/unit/test_alerts.py` — ~6 tests (mock apprise, rate limiting, disabled alerts, each notification type)

### Step 4: CLI Extensions

- **Modify** `src/bread/__main__.py` — Add `bread journal` command, enhance `bread status` with risk status and open orders
- **Extend** `tests/unit/test_cli.py` — ~4 tests for new/enhanced commands

---

## Files Summary

**New files (7):**
- `src/bread/monitoring/__init__.py`
- `src/bread/monitoring/journal.py`
- `src/bread/monitoring/tracker.py`
- `src/bread/monitoring/alerts.py`
- `tests/unit/test_journal.py`
- `tests/unit/test_tracker.py`
- `tests/unit/test_alerts.py`

**Modified files (6):**
- `pyproject.toml`
- `src/bread/core/config.py`
- `config/default.yaml`
- `src/bread/execution/engine.py`
- `src/bread/app.py`
- `src/bread/__main__.py`
- `tests/unit/test_execution_engine.py`
- `tests/unit/test_cli.py`

**Estimated: ~500 lines production code, ~500 lines test code, ~28 new tests**

---

## Verification Criteria

All checks must pass before moving to Phase 5.

### Unit Tests

1. **Fill reconciliation** — pending order updated to FILLED with correct price/timestamp; CANCELLED order updated; already-filled orders skipped; broker API failure handled gracefully
2. **Trade journal** — BUY+SELL paired into round-trip with correct P&L; unpaired BUY excluded; filters by date/strategy/symbol work; summary stats (win rate, expectancy) correct
3. **P&L tracker** — daily summary aggregates multiple snapshots per day correctly; weekly/monthly periods computed; drawdown series matches hand-calculated values; empty DB returns empty results
4. **Alerts** — mock apprise: trade event fires normal-priority notification; risk breach fires high-priority; rate limiter suppresses duplicate within window; disabled alerts send nothing
5. **CLI journal** — `bread journal` outputs table format; `--strategy` filter works; `--days` limits results
6. **CLI status** — enhanced output includes risk status section; open orders section shown when orders exist

### Integration Tests

1. **Journal + execution** — submit a bracket order on paper account, wait for fill, reconcile → journal entry appears with correct fill price
2. **Alert delivery** — configure a test webhook, trigger trade event → notification received

### End-to-End Test

1. Run paper trading bot for 1+ market day with alerts enabled
2. Verify:
   - `bread journal` shows any completed trades
   - `bread status` shows risk limits and open orders
   - Daily summary alert sent after market close
   - Trade alerts sent for executed orders
   - No unhandled exceptions in tick cycle

### Manual Checks

1. `ruff check src/` — clean
2. `mypy src/` — clean
3. Alert messages are readable and correctly formatted
4. `bread status` output matches Alpaca paper dashboard
