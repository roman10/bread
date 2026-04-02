# Technology Decisions

## Language: Python

**Decision:** Python 3.11+

**Rationale:** For swing trading (3-15 day holds, 15-minute evaluation ticks), execution latency is irrelevant. The edge comes from strategy quality and iteration speed, not microsecond execution.

| Factor | Weight | Python | Rust | Go |
|--------|--------|--------|------|-----|
| Alpaca SDK maturity | High | Official `alpaca-py`, excellent | Community, thin | Community, thin |
| Data science ecosystem | High | pandas, numpy, pandas-ta | Polars (immature) | Weak |
| Strategy research | High | Jupyter notebooks | None | None |
| Development speed | High | Fast (startup priority) | 3-5x slower | 1.5-2x slower |
| Backtesting libraries | Medium | Many options | Build from scratch | Build from scratch |
| Execution latency | **Low** | ~50ms (fine for 15-min ticks) | ~1ms | ~5ms |
| Concurrency | Low | Adequate (asyncio) | Excellent | Excellent |

**When to reconsider:** If we add HFT or sub-second strategies, write that specific module in Rust via PyO3. The rest stays Python.

---

## Broker: Alpaca Markets

**Decision:** Alpaca Markets as primary broker

**Alternatives evaluated:**
- Interactive Brokers — more markets, steeper API learning curve, better for experienced devs
- Tastytrade — excellent for options, REST API, $0.10-0.35/contract
- Tradier — good REST API, $0.35/contract options, $10-35/month platform fee
- TD Ameritrade / Schwab — API discontinued/limited after merger, avoid
- Robinhood — no official stock API, TOS violation risk, avoid

**Why Alpaca:**
- $0 commission on stocks, ETFs, crypto
- $0 minimum deposit
- Free paper trading with real-time data
- Official Python SDK (`alpaca-py`) — best-in-class developer experience
- REST API (not proprietary like IBKR's TWS)
- Bracket orders for server-side stop-loss execution
- SIPC protection ($500K including $250K cash)

**Limitations:**
- US markets only (no international stocks)
- Limited crypto selection vs crypto-native exchanges
- Options support is newer/less mature

**Migration path:** If we outgrow Alpaca (need international markets, more sophisticated options), move to Interactive Brokers. The modular architecture (broker adapter pattern) makes this a contained change.

---

## Market Data: Alpaca + Finnhub

**Decision:** Alpaca for OHLCV price data, Finnhub for supplementary signals

**Alpaca Data (primary):**
- Free with trading account
- Real-time quotes and historical bars
- Sufficient for daily/intraday bars

**Finnhub (supplementary):**
- Free tier: 60 API calls/min
- News sentiment — filter out stocks with heavy negative news
- Earnings calendar — avoid entering before earnings
- Not used as primary trading signal, only as risk filter

**Alternatives considered:**
- Alpha Vantage — 25 calls/day (too restrictive)
- Polygon.io — 5 req/min free tier (too slow)
- Yahoo Finance — unreliable, breaks without warning, avoid for production

---

## Technology Stack

| Component | Choice | Why |
|-----------|--------|-----|
| Python | 3.11+ | Performance + typing support |
| Broker SDK | `alpaca-py` | Official, OOP, paper/live switch |
| Supplementary data | `finnhub-python` | Free tier, news + fundamentals |
| Data processing | `pandas`, `numpy` | Industry standard for OHLCV |
| Technical indicators | `pandas-ta` | Pure Python, no C compilation |
| Configuration | `pydantic` + `PyYAML` | Type-safe validation + human-readable |
| Database | SQLite via `SQLAlchemy` | Zero-ops, single file |
| Scheduling | `APScheduler` | In-process, cron-like triggers |
| HTTP | `httpx` | Modern async client |
| Logging | Standard `logging` module | Fewer dependencies, structured JSON output via a custom formatter is sufficient for this system |
| Testing | `pytest` + `pytest-asyncio` | Standard, good async support |
| Alerting | `apprise` | One library for Discord/email/Slack |
| CLI | `typer` | Simple CLI framework |
| Linting | `ruff` | Fast, replaces flake8+isort+black |
| Type checking | `mypy` | Static type safety |

**Why not Backtrader/VectorBT for backtesting?** Backtrader is unmaintained (since 2018). VectorBT is heavyweight. A lightweight custom backtester that reuses the same strategy interface eliminates impedance mismatch between backtest and live code — same `Strategy.evaluate()` runs in both modes.

---

## Key Constraints

### Pattern Day Trader (PDT) Rule
- Currently requires $25K minimum for day trading (4+ day trades in 5 business days)
- FINRA proposed lowering to ~$2K in mid-2026 (not finalized)
- **Our approach:** Swing trading (3-15 day holds) avoids PDT entirely
- PDT guard built into risk manager as safety net

### Realistic Return Expectations

| Capital | $1,000/month requires | Feasibility |
|---------|----------------------|-------------|
| $5,000 | 240% annual | Extremely difficult |
| $10,000 | 120% annual | Very difficult |
| $20,000 | 60% annual | Challenging |

**Conservative targets:** 2-3% monthly ($100-600/month on $5-20K). Scale capital as strategies prove profitable. $1,000/month is a stretch goal for $20K+ accounts with proven edge.
