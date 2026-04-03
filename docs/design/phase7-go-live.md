# Phase 7: Go Live (Week 10+)

## Goal

Transition from paper trading to live trading with real money. Start with minimal capital and scale gradually. This phase is about operational discipline, not new features.

---

## Scope

### 7.1 Pre-Launch Checklist

- [ ] Phase 6 validation criteria all met
- [ ] Live Alpaca account funded and API keys generated
- [ ] `config/live.yaml` configured with live API keys and stricter risk limits
- [ ] Alert destinations verified (Discord/email working)
- [ ] Manual intervention procedures documented and tested
- [ ] Emergency shutdown procedure documented (kill bot + cancel all orders in Alpaca dashboard)

### 7.2 Capital Scaling Plan

| Stage | Capital | Duration | Criteria to Advance |
|-------|---------|----------|-------------------|
| 1 | $1,000 | 2 weeks | No bugs, P&L tracking accurate |
| 2 | $5,000 | 4 weeks | Annualized return on pace for ~20%, all risk limits respected |
| 3 | $10,000 | 4 weeks | Drawdown < 5%, Sharpe > 1.0, annualized ~20% |
| 4 | $20,000 | Ongoing | Full target capital, ~$4K/year at 20% annual |

### 7.3 Live Mode Differences

- Reads `config/live.yaml` for live API keys
- Stricter risk limits than paper defaults:
  - Max daily loss: 1% (vs 1.5% paper)
  - Max weekly loss: 2% (vs 3% paper)
  - Max drawdown: 5% (vs 7% paper)
- Requires typing "CONFIRM" on startup
- All alerts set to high priority

### 7.4 Daily Operations

- Check `bread status` at market open and close
- Review alerts throughout the day
- Weekly review of trade journal and P&L metrics
- Monthly backtest refresh with latest data to confirm strategy still viable

### 7.5 Scaling Decision Framework

**Advance to next capital stage when:**
- Minimum duration met
- No system bugs or reliability issues
- P&L within expected range (positive or acceptably small loss)
- Risk limits never breached unintentionally
- No manual interventions needed (clean automated operation)

**Reduce capital or pause when:**
- Max drawdown hit
- Multiple system errors in a week
- P&L significantly worse than backtest expectations
- Market regime change detected (strategy assumptions may not hold)

---

## Verification Criteria

### Stage 1 ($1,000) — After 2 Weeks

1. All orders executed correctly with bracket stops
2. P&L tracking matches Alpaca account statement
3. No unintended trades or order errors
4. Alerts delivered for all events
5. Position sizes correct given reduced capital

### Stage 2 ($5,000) — After 4 Weeks

1. All Stage 1 criteria still hold
2. Risk limits scale correctly with increased capital
3. Multiple concurrent positions managed correctly
4. Sector concentration limits respected

### Stage 3 ($10,000) — After 4 Weeks

1. Drawdown < 5% over the period
2. Sharpe ratio > 1.0 (annualized)
3. Annualized return tracking toward ~20%
4. System ran autonomously with minimal manual intervention
5. Ready for full capital deployment

### Ongoing Monitoring

1. Monthly backtest comparison — paper vs live vs backtest results
2. Quarterly strategy review — is ETF momentum still effective?
3. Annual infrastructure review — dependency updates, API changes
