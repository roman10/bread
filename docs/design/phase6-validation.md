# Phase 6: Validation (Week 7-9)

## Goal

Run the paper trading bot for 2-4 weeks under real market conditions. Identify bugs, tune parameters, and build confidence that the system behaves correctly before risking real money. This phase produces no new code features — it is purely operational validation and tuning.

---

## Scope

### 6.1 Paper Trading Campaign

- Run `python -m bread run --mode paper` continuously for 2-4 weeks
- Monitor daily via alerts and `bread status`
- Keep a manual log of observations and anomalies

### 6.2 Bug Fixes and Reliability

- Fix any bugs discovered during paper trading
- Handle edge cases:
  - Market holidays (no data available)
  - Pre-market / after-hours gaps
  - Alpaca API outages or rate limits
  - Partial fills
  - Split/dividend adjustments
- Improve error handling and recovery

### 6.3 Parameter Tuning

- Compare paper trading results against backtest expectations
- Tune strategy parameters if needed:
  - Entry/exit thresholds
  - Stop-loss distances (ATR multiplier)
  - Position sizing risk percentage
  - Time stop duration
- Re-run backtests with tuned parameters to confirm improvement

### 6.4 Operational Procedures

- Document startup/shutdown procedures
- Document how to manually intervene (cancel orders, close positions)
- Document how to restart after a max-drawdown halt
- Verify that bracket orders persist through bot restarts

---

## Verification Criteria

All criteria must be met before moving to Phase 7 (Go Live).

### System Reliability

1. Bot ran for 2+ weeks without unhandled crashes
2. All market days had successful tick cycles (check logs)
3. No missed trading sessions (scheduler fired on time)
4. Bot recovered gracefully from any Alpaca API errors
5. Bracket orders survived bot restarts (verified in Alpaca dashboard)

### Trading Behavior

1. Trades match strategy rules — manually review each trade against entry/exit criteria
2. Risk limits were respected — no position exceeded size limits; circuit breakers fired correctly if triggered
3. PDT guard worked — no 4th day trade in any rolling 5-day window (if account < $25K)
4. No phantom trades — every order in Alpaca matches a journal entry, and vice versa

### Performance Sanity

1. Paper P&L is within a reasonable range of backtest expectations (not identical, but same ballpark)
2. Win rate, average win/loss, holding period roughly match backtest
3. Max drawdown stayed within configured limits
4. No obviously broken trades (e.g., buying at ask and immediately hitting stop-loss)

### Operational Readiness

1. Alerts delivered reliably for all events
2. `bread status` accurately reflects real-time state
3. Manual intervention procedures tested (cancel an order, close a position)
4. Restart procedure tested (stop bot, restart, verify state consistency)
