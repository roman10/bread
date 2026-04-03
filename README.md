# Bread

Algorithmic swing trading system for US-listed ETFs, built on the Alpaca Markets API.

Targets ~20% annual returns on $5K-$20K capital with 4-8 trades per month and 3-15 day holding periods.

## Prerequisites

- Python 3.11+
- [Alpaca Markets](https://alpaca.markets/) account (free paper trading account works)

## Installation

```bash
# Clone and enter the project
git clone <repo-url> && cd bread

# Create virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# Install with all extras (trading + dashboard + dev tools)
pip install -e ".[dashboard,dev]"
```

## Configuration

### 1. API Keys

```bash
cp .env.example .env
```

Edit `.env` and add your Alpaca paper trading credentials:

```
ALPACA_PAPER_API_KEY=your-paper-api-key
ALPACA_PAPER_SECRET_KEY=your-paper-secret-key
```

### 2. Trading Parameters

All tunable parameters live in YAML config files under `config/`:

| File | Purpose |
|------|---------|
| `config/default.yaml` | Base config: risk limits, tick interval, data settings |
| `config/paper.yaml` | Paper trading overrides (e.g., enable alerts) |
| `config/live.yaml` | Live trading overrides (gitignored, you create this) |
| `config/strategies/etf_momentum.yaml` | Strategy-specific parameters (universe, entry/exit rules) |

Key defaults in `config/default.yaml`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `risk.max_positions` | 5 | Max simultaneous open positions |
| `risk.max_position_pct` | 0.20 | Max 20% of equity in one position |
| `risk.max_daily_loss_pct` | 0.015 | Stop trading if daily loss exceeds 1.5% |
| `risk.max_drawdown_pct` | 0.07 | Halt all trading if drawdown exceeds 7% |
| `execution.tick_interval_minutes` | 15 | How often the bot evaluates signals during market hours |

### 3. Alerts (Optional)

To receive trade notifications on Discord:

1. In Discord: Channel Settings > Integrations > Webhooks > New Webhook
2. Copy the webhook URL
3. Add it to `config/paper.yaml`:

```yaml
alerts:
  enabled: true
  urls:
    - "discord://WEBHOOK_ID/WEBHOOK_TOKEN"
```

Supports Discord, Slack, and email via [Apprise](https://github.com/caronc/apprise).

## Quick Start

```bash
# 1. Initialize the database
python -m bread db init

# 2. Verify data pipeline works
python -m bread fetch SPY

# 3. Check account connectivity
python -m bread status

# 4. Run a backtest
python -m bread backtest --strategy etf_momentum --start 2024-01-01 --end 2025-12-31

# 5. Start paper trading
python -m bread run --mode paper
```

## CLI Commands

```
python -m bread <command>
```

| Command | Description |
|---------|-------------|
| `db init` | Create/migrate the SQLite database |
| `fetch <SYMBOL>` | Fetch daily bars, cache them, compute indicators |
| `backtest --strategy <name> --start <YYYY-MM-DD> --end <YYYY-MM-DD>` | Run a historical backtest |
| `run --mode paper` | Start the trading bot (paper or live) |
| `status` | Show account equity, positions, risk limits, open orders |
| `journal --days 30 [--strategy <name>] [--symbol <SYM>]` | Display completed trade history |
| `dashboard [--port 8050] [--debug]` | Launch the web monitoring dashboard |

You can also use the installed entry point directly: `bread <command>`.

## Running the Trading Bot

```bash
python -m bread run --mode paper
```

The bot runs a **tick cycle every 15 minutes** during US market hours (9:30 AM - 4:00 PM ET, Mon-Fri):

1. **Reconcile** - Sync local state with Alpaca broker
2. **Snapshot** - Record portfolio equity to database
3. **Refresh data** - Fetch latest OHLCV bars for all symbols
4. **Compute indicators** - SMA, RSI, MACD, ATR, Bollinger Bands
5. **Evaluate strategies** - Generate buy/sell signals
6. **Risk check** - Validate signals against position limits, drawdown, PDT rules
7. **Execute** - Submit bracket orders (with server-side stop-loss)
8. **Alert** - Notify via Discord/Slack/email

Stop with `Ctrl+C` - the bot shuts down gracefully (cancels pending orders, saves final snapshot).

## Monitoring

### CLI Status Check

```bash
python -m bread status
```

Shows account equity, cash, buying power, open positions with unrealized P&L, risk limit usage, and open orders.

### Trade Journal

```bash
# Last 30 days
python -m bread journal

# Filter by strategy or symbol
python -m bread journal --strategy etf_momentum --symbol SPY --days 60
```

Shows completed round-trip trades with entry/exit prices, P&L, holding period, and a summary with win rate.

### Web Dashboard

```bash
# Install dashboard dependencies (if not already installed)
pip install -e ".[dashboard]"

# Launch (default port 8050)
python -m bread dashboard

# Custom port or debug mode (auto-reload on code changes)
python -m bread dashboard --port 8080 --debug
```

Open http://localhost:8050 in your browser. The dashboard uses a dark theme (Darkly Bootstrap) and has two pages accessible from the top navigation bar.

The navbar shows a **PAPER** or **LIVE** badge indicating the trading mode, and a green **Connected** dot confirming the Alpaca API connection is healthy.

#### Portfolio Page (`/`)

The home page gives a real-time overview of your account:

- **KPI cards** at the top — Equity, Daily P&L (with percentage), Buying Power, and Drawdown (color-coded: green < 2%, yellow 2-5%, red > 5%)
- **Equity curve** — 90-day chart of portfolio value with green fill area
- **Drawdown chart** — Visualizes drawdown from peak equity over time
- **Open Positions table** — Sortable grid showing each position's symbol, quantity, entry price, current price, unrealized P&L (color-coded green/red), and market value
- **Open Orders table** — Pending orders with symbol, side, quantity, type, status, and submission time

#### Trades Page (`/trades`)

The trades page shows your completed round-trip trade history with interactive filters:

- **Strategy filter** — Dropdown to view trades from a specific strategy (e.g., `etf_momentum`)
- **Symbol filter** — Text input to filter by ticker (e.g., `SPY`)
- **Lookback slider** — Adjust the time window: 7, 30, 90, 180, or 365 days
- **P&L period toggle** — Switch the P&L bar chart between Daily, Weekly, or Monthly aggregation

Below the filters:

- **KPI cards** — Total P&L, Win Rate, Expectancy (avg profit per trade), and Trade Count
- **P&L bar chart** — Green/red bars showing profit and loss per period
- **Trade Journal table** — Paginated grid (25 rows/page) with exit date, symbol, quantity, entry/exit prices, P&L, P&L %, holding days, strategy name, and exit reason. Sortable by any column.

#### Running Alongside the Trading Bot

The dashboard is read-only — it queries the database and Alpaca API but never places trades. Run it in a separate terminal alongside the trading bot:

```bash
# Terminal 1: trading bot
python -m bread run --mode paper

# Terminal 2: dashboard
python -m bread dashboard
```

#### Auto-Refresh

The dashboard refreshes automatically:
- **Every 30 seconds** during US market hours (Mon-Fri, 9:30 AM - 4:00 PM ET)
- **Every 5 minutes** outside market hours

No manual refresh needed — data updates are pushed to all charts and tables on each interval.

### Alerts

When enabled, the system sends notifications for:

- **Trade execution** - "BUY 100 SPY @ $450.50"
- **Daily summary** - End-of-day P&L, trade count, equity
- **Risk breach** - Daily loss limit or drawdown circuit breaker triggered
- **System errors** - Critical exceptions

## Strategy: ETF Momentum

The default strategy trades 10 liquid ETFs: SPY, QQQ, IWM, DIA, XLF, XLK, XLE, XLV, GLD, TLT.

**Entry** (all conditions must be true):
- Price above 200-day SMA (long-term uptrend)
- RSI(14) bounces from below 30 back above 30 (oversold reversal)
- SMA(20) > SMA(50) (intermediate trend confirmation)
- Volume above 20-day average (participation check)

**Exit** (any one triggers):
- RSI(14) > 70 (overbought, take profit)
- Stop-loss at 1.5x ATR below entry (submitted as bracket order to Alpaca)
- Holding period exceeds 15 trading days (time stop)
- SMA(20) crosses below SMA(50) (trend reversal)

**Position sizing**: Fixed fractional - risks 0.5% of equity per trade, capped at 20% of equity per position.

## Risk Management

Seven circuit breakers run before every trade:

| Limit | Default | Effect |
|-------|---------|--------|
| Max positions | 5 | Rejects new buys |
| Concentration | 20% equity | Rejects oversized positions |
| Asset class exposure | 40% equity | Prevents sector overconcentration |
| Daily loss | 1.5% | Halts all trading for the day |
| Weekly loss | 3.0% | Halts all trading for the week |
| Max drawdown | 7.0% | Halts trading until manual restart |
| PDT guard | 3 day trades / 5 days | Prevents pattern day trader violation (accounts < $25K) |

Stop-loss orders are submitted as **server-side bracket orders** to Alpaca, so they execute even if the bot crashes.

## Backtesting

```bash
python -m bread backtest --strategy etf_momentum --start 2024-01-01 --end 2025-12-31
```

Uses the same `Strategy` interface as live trading - no code differences between backtest and production.

Output includes: total return, CAGR, Sharpe ratio, Sortino ratio, max drawdown, win rate, profit factor, trade count, and average holding period.

## Project Structure

```
bread/
├── config/                     # YAML configuration
│   ├── default.yaml            # Base config (all parameters)
│   ├── paper.yaml              # Paper trading overrides
│   └── strategies/
│       └── etf_momentum.yaml   # Strategy parameters
├── src/bread/
│   ├── __main__.py             # CLI entry point
│   ├── app.py                  # Trading bot orchestrator
│   ├── core/                   # Config, models, exceptions, logging
│   ├── data/                   # Market data pipeline (Alpaca + cache)
│   ├── strategy/               # Strategy framework + ETF Momentum
│   ├── backtest/               # Backtesting engine + metrics
│   ├── risk/                   # Position sizing, limits, validators
│   ├── execution/              # Order management + Alpaca broker
│   ├── monitoring/             # Trade journal, P&L tracker, alerts
│   ├── dashboard/              # Dash web UI
│   └── db/                     # SQLAlchemy models + database setup
├── tests/                      # Unit tests (258 tests)
├── docs/                       # Design documentation
├── data/                       # SQLite database (auto-created)
├── pyproject.toml              # Dependencies and build config
└── .env.example                # API key template
```

## Development

```bash
# Run tests
pytest

# Lint and format
ruff check src/ tests/
ruff format src/ tests/

# Type check
mypy src/bread/
```

## Adding a New Strategy

1. Create `src/bread/strategy/my_strategy.py`:

```python
import pandas as pd

from bread.core.models import Signal
from bread.strategy.base import Strategy
from bread.strategy.registry import register

@register("my_strategy")
class MyStrategy(Strategy):
    @property
    def name(self) -> str:
        return "my_strategy"

    @property
    def universe(self) -> list[str]:
        return ["SPY", "QQQ"]

    @property
    def min_history_days(self) -> int:
        return 200

    @property
    def time_stop_days(self) -> int:
        return 15

    def evaluate(self, universe: dict[str, pd.DataFrame]) -> list[Signal]:
        # Your signal logic here
        ...
```

2. Create `config/strategies/my_strategy.yaml` with strategy parameters
3. Add the strategy to `config/default.yaml` under the `strategies:` list
