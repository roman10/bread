# Phase 3: Execution + Paper Trading (Week 4)

## Goal

Build the execution engine, risk management, and application orchestrator. Connect everything to Alpaca paper trading. This phase produces a fully operational paper trading bot that runs on a schedule, evaluates strategies, validates signals through risk checks, and submits bracket orders.

---

## New Dependencies

Add to `pyproject.toml`:

```
"apscheduler>=3.10",
```

---

## Scope

### 3.0 Foundation Extensions

Changes to existing modules required before building the new Phase 3 modules.

#### Config Additions (`core/config.py`)

Add two new settings models and include them in `AppConfig`:

```python
class RiskSettings(BaseModel):
    risk_pct_per_trade: float = Field(default=0.005, gt=0, le=0.05)  # 0.5%
    max_positions: int = Field(default=5, ge=1)
    max_position_pct: float = Field(default=0.20, gt=0, le=1.0)     # 20% of equity
    max_asset_class_pct: float = Field(default=0.40, gt=0, le=1.0)   # 40% of equity
    max_daily_loss_pct: float = Field(default=0.015, gt=0, le=1.0)   # 1.5%
    max_weekly_loss_pct: float = Field(default=0.03, gt=0, le=1.0)   # 3%
    max_drawdown_pct: float = Field(default=0.07, gt=0, le=1.0)      # 7%
    pdt_enabled: bool = True
    asset_classes: dict[str, list[str]] = Field(default_factory=lambda: {
        "equity_broad": ["SPY", "QQQ", "IWM", "DIA"],
        "financials": ["XLF"],
        "technology": ["XLK"],
        "energy": ["XLE"],
        "healthcare": ["XLV"],
        "commodities": ["GLD"],
        "fixed_income": ["TLT"],
    })


class ExecutionSettings(BaseModel):
    tick_interval_minutes: int = Field(default=15, ge=1)
    take_profit_ratio: float = Field(default=2.0, gt=0)  # take_profit_pct = stop_loss_pct * this
```

Add to `AppConfig`:

```python
class AppConfig(BaseModel):
    # ... existing fields ...
    risk: RiskSettings = Field(default_factory=RiskSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
```

#### Config Additions (`config/default.yaml`)

```yaml
risk:
  risk_pct_per_trade: 0.005
  max_positions: 5
  max_position_pct: 0.20
  max_asset_class_pct: 0.40
  max_daily_loss_pct: 0.015
  max_weekly_loss_pct: 0.03
  max_drawdown_pct: 0.07
  pdt_enabled: true
  asset_classes:
    equity_broad: [SPY, QQQ, IWM, DIA]
    financials: [XLF]
    technology: [XLK]
    energy: [XLE]
    healthcare: [XLV]
    commodities: [GLD]
    fixed_income: [TLT]

execution:
  tick_interval_minutes: 15
  take_profit_ratio: 2.0
```

#### Domain Models (`core/models.py`)

```python
class OrderStatus(StrEnum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"

class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

@dataclass
class Order:
    symbol: str
    side: OrderSide
    qty: int
    status: OrderStatus
    broker_order_id: str | None = None
    stop_loss_price: float | None = None
    take_profit_price: float | None = None
    filled_price: float | None = None
    strategy_name: str = ""
    reason: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    filled_at: datetime | None = None

@dataclass
class Position:
    symbol: str
    qty: int
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    broker_order_id: str
    strategy_name: str
    entry_date: date
```

#### DB Tables (`db/models.py`)

```python
class OrderLog(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    broker_order_id: Mapped[str | None] = mapped_column(String, nullable=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    stop_loss_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    filled_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    strategy_name: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    filled_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_orders_symbol_status", "symbol", "status"),
    )


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    equity: Mapped[float] = mapped_column(Float, nullable=False)
    cash: Mapped[float] = mapped_column(Float, nullable=False)
    positions_value: Mapped[float] = mapped_column(Float, nullable=False)
    open_positions: Mapped[int] = mapped_column(Integer, nullable=False)
    daily_pnl: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        Index("ix_snapshots_ts", "timestamp_utc"),
    )
```

#### Exception Types (`core/exceptions.py`)

```python
class ExecutionError(BreadError):
    """Execution engine error."""

class RiskError(BreadError):
    """Risk management error."""

class OrderError(ExecutionError):
    """Order submission or tracking error."""
```

---

### 3.1 Risk Management (`risk/`)

**This is the most critical module — errors here lose real money.**

#### `risk/position_sizer.py`

Fixed fractional sizing. Single pure function:

```python
def compute_position_size(
    equity: float,
    risk_pct: float,          # from config, default 0.005
    stop_loss_pct: float,     # from Signal
    max_position_pct: float,  # from config, default 0.20
    price: float,             # current market price
) -> int:
    """Return number of shares to buy. Always >= 0."""
    risk_dollars = equity * risk_pct
    position_value = risk_dollars / stop_loss_pct
    max_value = equity * max_position_pct
    capped_value = min(position_value, max_value)
    shares = int(capped_value / price)
    return max(shares, 0)
```

Example: equity=$10K, risk_pct=0.5%, stop_loss_pct=5%, price=$450
- risk_dollars = $50
- position_value = $1,000
- max_value = $2,000 (20% of $10K)
- capped_value = $1,000
- shares = 2

#### `risk/limits.py`

Stateless limit checks. Each returns `(passed: bool, reason: str)`. Inputs come from the Alpaca broker, not local state.

| Check | Inputs | Logic |
|-------|--------|-------|
| `check_max_positions` | open position count, config | `count < max_positions` |
| `check_position_concentration` | proposed position value, equity | `value / equity <= max_position_pct` |
| `check_asset_class_exposure` | symbol, open positions, equity, asset_classes map | total value in same asset class / equity <= `max_asset_class_pct` |
| `check_daily_loss` | today's P&L (from Alpaca `account.equity - account.last_equity`), equity | `abs(loss) / equity < max_daily_loss_pct` |
| `check_weekly_loss` | week's realized P&L (from `orders` table), equity | `abs(loss) / equity < max_weekly_loss_pct` |
| `check_drawdown` | current equity, peak equity (from `MAX(equity)` in `portfolio_snapshots`) | `(peak - current) / peak < max_drawdown_pct` |
| `check_pdt` | day trade count in last 5 trading days (from `orders` table), account equity | if `equity < 25_000` and `pdt_enabled`: `count < 3` |

**Data sources for derived values:**

- **Daily P&L**: Alpaca `TradeAccount.equity` minus `TradeAccount.last_equity` (previous close). No local tracking needed.
- **Weekly P&L**: Sum of realized P&L from `orders` table where `filled_at_utc` is within the current calendar week (Monday-Friday). Unrealized P&L from current positions (broker market value minus entry cost).
- **Peak equity**: `SELECT MAX(equity) FROM portfolio_snapshots`. Updated every tick.
- **Day trade count**: Count symbols with both a BUY fill and a SELL fill on the same calendar day within the last 5 trading days, from `orders` table.

#### `risk/validators.py`

Pre-trade validation chain. Every BUY signal passes through before becoming an order:

```python
@dataclass(frozen=True)
class ValidationResult:
    approved: bool
    rejections: list[str]   # empty if approved

def validate_signal(
    signal: Signal,
    position_size: int,
    price: float,
    account: TradeAccount,     # from alpaca-py
    positions: list[Position],
    config: RiskSettings,
    peak_equity: float,
) -> ValidationResult:
    """Run all validators in order. Short-circuit on first failure."""
```

Validators run in order:

1. **Position size > 0** — sizing returned zero shares (position too small to trade)
2. **Buying power** — `account.buying_power >= position_size * price`
3. **Position limit** — `len(positions) < max_positions`
4. **Concentration** — single position cap + asset class exposure limit
5. **Drawdown** — daily loss, weekly loss, and max drawdown from peak
6. **PDT guard** — day trade count (unlikely for swing trading, but safety net)

Rejection logged with specific reason. No silent drops.

> **Deferred to future phases:** Spread/liquidity and volatility validators. The current ETF universe (SPY, QQQ, etc.) trades billions in daily volume with sub-penny spreads. These checks add complexity without practical value at this scale. Revisit when expanding to small-cap or illiquid instruments.

#### `risk/manager.py`

Orchestrates position sizing + validation:

```python
class RiskManager:
    def __init__(self, config: RiskSettings) -> None: ...

    def evaluate(
        self,
        signal: Signal,
        price: float,
        account: TradeAccount,
        positions: list[Position],
        peak_equity: float,
    ) -> tuple[int, ValidationResult]:
        """Size the position, then validate.

        Returns (shares, validation_result).
        If validation fails, shares may be > 0 but should not be used.
        """
```

---

### 3.2 Execution Engine (`execution/`)

#### `execution/alpaca_broker.py`

Wraps `alpaca-py` `TradingClient`. Paper/live controlled by `config.mode`.

```python
class AlpacaBroker:
    def __init__(self, config: AppConfig) -> None:
        """Initialize TradingClient with mode-appropriate credentials.
        Uses paper=True/False based on config.mode.
        """

    def get_account(self) -> TradeAccount:
        """Fetch account info (equity, buying_power, cash, last_equity)."""

    def get_positions(self) -> list[AlpacaPosition]:
        """Fetch all open positions from Alpaca."""

    def get_orders(self, status: str = "open") -> list[AlpacaOrder]:
        """Fetch orders by status."""

    def submit_bracket_order(
        self,
        symbol: str,
        qty: int,
        stop_loss_price: float,
        take_profit_price: float,
    ) -> str:
        """Submit a bracket order: market buy + OCO (stop-loss / take-profit).

        Uses OrderRequest with:
          order_class = OrderClass.BRACKET
          side = OrderSide.BUY
          type = OrderType.MARKET
          time_in_force = TimeInForce.DAY
          stop_loss = {"stop_price": stop_loss_price}
          take_profit = {"limit_price": take_profit_price}

        Returns the Alpaca parent order ID.
        Raises OrderError on submission failure.
        """

    def close_position(self, symbol: str) -> str | None:
        """Close a position by symbol.
        Alpaca automatically cancels open bracket legs when position is closed.
        Returns order ID or None if no position found.
        """
```

**Bracket order take-profit price:**

```
take_profit_price = entry_price * (1 + signal.stop_loss_pct * config.execution.take_profit_ratio)
```

With defaults (stop_loss_pct=5%, take_profit_ratio=2.0): take_profit at +10% from entry. This acts as an automatic profit ceiling. Strategy SELL signals (RSI overbought, trend reversal) close positions earlier during normal operation via `close_position()`.

**Why bracket orders matter:** Both stop-loss and take-profit legs persist on Alpaca's servers. If the bot crashes, positions are still protected. This is the most critical safety property.

#### `execution/engine.py`

Order management, position lifecycle, and broker reconciliation:

```python
class ExecutionEngine:
    def __init__(
        self,
        broker: AlpacaBroker,
        risk_manager: RiskManager,
        config: AppConfig,
        session_factory: sessionmaker,
    ) -> None: ...

    def reconcile(self) -> None:
        """Sync local state with broker on every tick.

        1. Fetch positions from Alpaca.
        2. For each broker position not in local state:
           → Add to local tracking, log WARNING (manual trade or state loss).
        3. For each local position not on broker:
           → Mark as closed (bracket stop/TP triggered while bot was down), log INFO.
        4. Update equity and position market values.
        """

    def process_signals(self, signals: list[Signal]) -> None:
        """Process strategy output. SELL signals first, then BUY signals.

        SELL signals:
        - If we hold the position → call broker.close_position(symbol)
        - If we don't hold it → ignore (log DEBUG)

        BUY signals:
        - If we already hold the symbol → skip
        - If there's already a pending order for this symbol → skip (idempotent)
        - Otherwise → risk_manager.evaluate() → if approved, submit bracket order
        - Log all approvals and rejections to orders table
        """

    def get_positions(self) -> list[Position]:
        """Return current tracked positions."""

    def get_equity(self) -> float:
        """Return current account equity from broker."""

    def save_snapshot(self, session: Session) -> None:
        """Persist a PortfolioSnapshot to the database."""
```

**Idempotency:** Before submitting a BUY, check if there's already a pending/accepted order for that symbol (query `orders` table or Alpaca open orders). Before submitting a SELL, check if there's already a close pending. Skip duplicates and log.

---

### 3.3 Application Orchestrator (`app.py`)

#### Tick Cycle

`APScheduler` fires `tick()` every N minutes (default 15) during market hours.

```python
def tick() -> None:
    """Single tick of the trading loop."""
    # 1. Reconcile: sync local positions with Alpaca broker state
    engine.reconcile()

    # 2. Snapshot: record current portfolio state to DB
    engine.save_snapshot(session)

    # 3. Refresh data: fetch latest bars for all strategy universes
    #    (uses existing BarCache — only fetches if cache is stale)
    universe_data = load_universe_data(strategies, cache, config)

    # 4. Evaluate: run all active strategies
    all_signals: list[Signal] = []
    for strategy in strategies:
        signals = strategy.evaluate(universe_data[strategy.name])
        all_signals.extend(signals)

    # 5. Execute: process signals (SELL first, then BUY with risk checks)
    engine.process_signals(all_signals)

    # 6. Log: tick summary
    log_tick_summary(all_signals, engine.get_positions())
```

#### Scheduler Configuration

```python
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

scheduler = BlockingScheduler()
scheduler.add_job(
    tick,
    CronTrigger(
        day_of_week="mon-fri",
        hour="9-15",          # 9:30 AM to 3:45 PM ET (last tick before close)
        minute="*/15",
        timezone="America/New_York",
    ),
)
```

> **Note on tick frequency vs daily bars:** The strategy uses daily bars which don't change intraday. Most ticks will see the same data and generate the same signals. This is handled by the execution engine's idempotency — duplicate signals are skipped. The frequent ticks are still valuable for: (a) position reconciliation, (b) portfolio snapshots, (c) detecting bracket fills that happened between ticks.

#### Startup

```python
def run(mode: str) -> None:
    # 1. Load config, initialize logging
    # 2. Auto-init DB (create tables if missing)
    # 3. Initialize broker, risk manager, execution engine
    # 4. Initial reconciliation (recover state from previous run)
    # 5. If mode == "live": prompt for "CONFIRM" input
    # 6. Configure APScheduler
    # 7. Register signal handlers (SIGINT, SIGTERM → graceful shutdown)
    # 8. Start scheduler (blocks)
```

#### Graceful Shutdown

On SIGINT/SIGTERM:

1. Stop the scheduler (no new ticks fire)
2. Wait for any in-progress tick to complete (APScheduler handles this)
3. Log final portfolio state
4. Exit cleanly

**Positions survive restarts.** Bracket orders live on Alpaca's servers — stop-loss and take-profit execute even when the bot is not running. On next startup, `reconcile()` recovers all position state from the broker.

---

### 3.4 CLI (`__main__.py`)

#### `bread run`

```
bread run --mode paper
```

Starts the paper trading bot. Runs until interrupted (Ctrl+C).

#### `bread status`

```
bread status
```

Queries Alpaca API and local DB, prints:

```
Account: equity=$10,234.50  cash=$8,234.50  buying_power=$8,234.50
Today: P&L=+$34.50 (+0.34%)  Drawdown from peak: 1.2%

Open Positions (2):
  SPY  qty=2   entry=$502.30  current=$510.15  P&L=+$15.70 (+1.6%)  stop=$477.19
  XLK  qty=5   entry=$198.40  current=$196.80  P&L=-$8.00 (-0.8%)   stop=$183.52
```

---

### 3.5 Paper → Live Switching

- Controlled by `config.mode` (single value: `"paper"` or `"live"`)
- Live mode reads `config/live.yaml` which can override any risk limits
- Live startup requires interactive confirmation:
  ```
  WARNING: LIVE TRADING MODE — real money at risk
  Type "CONFIRM" to proceed:
  ```
- All code paths identical between paper and live — only API credentials and risk limits differ

---

## Verification Criteria

All checks must pass before moving to Phase 4.

### Unit Tests

1. **Position sizer** — equity=$10K, risk_pct=0.5%, stop_loss_pct=5%, price=$100 → shares=10; also verify max_position_pct cap is applied
2. **Limits — max positions** — 5 positions held → 6th signal rejected with reason "max positions exceeded"
3. **Limits — daily loss** — simulate 1.5% equity loss → trading halted; new signals rejected with "daily loss limit"
4. **Limits — PDT guard** — 3 day trades in 5 days → 4th blocked; accounts >= $25K not blocked
5. **Limits — max drawdown** — 7% drawdown from peak → all trading halted
6. **Limits — asset class exposure** — 2 equity_broad positions at 20% each → 3rd equity_broad signal rejected with "asset class limit exceeded"
7. **Validator chain** — signal passes all validators → approved; signal fails any validator → rejected with specific reason
8. **Execution engine signals** — SELL signal with open position → close_position called; SELL signal without position → ignored; BUY signal for already-held symbol → skipped; duplicate BUY signal → skipped (idempotent)
9. **Reconciliation** — broker has position not in local state → added with warning; local position not on broker → marked closed

### Integration Tests (Alpaca Paper)

1. **Submit bracket order** — submit bracket order (buy SPY, stop-loss, take-profit) to Alpaca paper → order appears in Alpaca dashboard with bracket structure
2. **Position reconciliation** — manually create a position in paper account → engine reconciles and tracks it
3. **Full tick cycle** — trigger a tick → data refreshed, strategy evaluated, risk-checked, order submitted (or correctly rejected)
4. **Close position** — trigger SELL signal for held position → bracket legs cancelled, position closed via market sell

### End-to-End Test

1. `python -m bread run --mode paper` — starts without error
2. Scheduler fires `tick()` at correct intervals during market hours
3. Let run for 1+ market day — verify:
   - Logs show tick cycles executing
   - If signals generated: orders appear in Alpaca paper dashboard with bracket (stop-loss + take-profit) attached
   - If no signals: logs show "no signals" or rejection reasons
   - No unhandled exceptions
4. `python -m bread status` — shows account, positions, P&L
5. Ctrl+C → graceful shutdown, no orphaned processes
6. Restart → reconciliation recovers state, no duplicate orders

### Manual Checks

1. `ruff check src/` — clean
2. `mypy src/` — clean
3. Alpaca paper dashboard shows any submitted orders with correct bracket structure
4. Risk rejection logs are clear and include specific reasons
