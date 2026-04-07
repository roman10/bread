# Strategy Lifecycle Automation: Assessment & Recommendation

## Context

Analysis of automating a full strategy lifecycle pipeline: generate strategies -> backtest -> paper trade winners -> promote to live -> retire underperformers -> loop.

**Current project state**: Phase 6 (validation) just started — paper trading began ~4 days ago. Phase 7 (go live) plans careful capital scaling from $1K to $20K. The system has 10 implemented strategies, a solid backtest engine, comprehensive risk management, and a monitoring dashboard.

---

## Honest Assessment: Full Automation Is Premature

After thorough analysis of the codebase, the math, and the constraints, **we recommend against building the full automated pipeline at this stage.** Here's why:

### 1. The Overfitting Problem Is Fatal at This Scale

With 10 strategy templates and parameter sweeps, you'd generate ~100-1000+ variants per round. Even with walk-forward validation, the **multiple testing bias** is severe:

- The backtest engine produces ~48-96 trades per strategy per year (swing trading frequency)
- With 1,000 variants tested, ~50 will pass a 5% significance threshold **by pure luck**
- Walk-forward validation helps but doesn't solve this — a variant can get lucky in out-of-sample windows too, especially with only 20-30 trades per fold
- The Deflated Sharpe Ratio (Lopez de Prado) shows that a Sharpe of 1.5 from the best of 1,000 tests may have a true Sharpe near zero
- **Expected false discovery rate: ~80%** (if 1% of variants have real edge, and 5% of all variants pass screening, 40 of 50 "winners" are false positives)

Compare to manually curating strategies with economic rationale: `risk_off_rotation` embodies a clear regime thesis (one "test"). Sweeping 100 parameter combos of that thesis is 100 tests with approximately zero incremental edge.

### 2. Capital Constraints Kill Multi-Strategy Parallelism

At $5-20K with max 5 positions:
- Each position is $1K-$4K (fixed fractional sizing with 0.5% risk per trade)
- Running 3+ strategies simultaneously fragments capital to meaningless levels
- Position sizes below ~$500 are pure noise (slippage dominates returns)
- **At $10K, you can meaningfully run exactly 1 strategy at a time**

The pipeline's implicit model of "N strategies running simultaneously" directly contradicts the capital constraints.

### 3. Paper Trading Sample Sizes Are Statistically Meaningless

For strategy *validation* (not system validation):
- 4-8 weeks of paper = 5-15 swing trades
- To distinguish 55% win rate from 50% with 80% statistical power requires ~783 trades
- Even 60% vs 50% requires ~196 trades (2-4 years of swing trading)
- With 8 trades, you literally cannot tell if a strategy works or got lucky

Paper trading IS valuable for **system validation** (bugs, bracket order persistence, reconciliation) — which is exactly what Phase 6 already does. It's not meaningful for strategy selection.

### 4. Complexity vs Benefit Is Deeply Negative

Building the pipeline requires:
- Parameter sweep generator
- Walk-forward validation engine (major backtest extension)
- Multi-strategy paper scheduling & orchestration
- Promotion/retirement decision engine with state management
- New DB tables (StrategyVariant, PipelineRun, StrategyPerformanceLog)
- Multiple testing correction implementation
- Pipeline monitoring (who monitors the monitor?)

**Estimated effort**: 100+ hours of development + ongoing maintenance.
**Required incremental return to break even**: The pipeline must improve returns by 10-20% relative to manual review. There's no evidence automated strategy rotation outperforms careful manual curation at this scale.

**What can go wrong**:
- Cascading retirements during volatility spikes (all strategies retired simultaneously, weeks of downtime)
- Bugs in the backtest engine amplified across 1,000 variants
- Parameter churn burning capital through missed opportunities
- Always fighting the last war (new variants fit to recent regime, fail when it shifts again)

### 5. It's the Wrong Time

Paper trading started 4 days ago. The system hasn't yet proven that *any* strategy works in production. Building infrastructure to rotate strategies before validating the base system is textbook premature optimization.

---

## Recommended Path: Pragmatic Strategy Evolution

Instead of full automation, here's a phased approach that captures the intent with 5% of the complexity:

### Phase A: Execute the Current Plan (Now — Weeks 7-12)

Finish Phase 6 validation and Phase 7 go-live exactly as designed. This is the highest-value work right now.

### Phase B: Curate 2-3 Strategies With Distinct Economic Rationales (During Phase 6)

From the 10 existing strategies, select based on **economic thesis diversity**, not backtest performance:

| Slot | Strategy | Rationale | When It Works |
|------|----------|-----------|---------------|
| Primary | `etf_momentum` | Trend-following (momentum premium) | Trending markets |
| Defensive | `risk_off_rotation` | Regime-aware equity/safe-haven rotation | All regimes |
| Alternate | `bb_mean_reversion` | Mean reversion (different from momentum) | Range-bound markets |

Run backtests on each across 2020-2025 (COVID crash, 2022 bear, 2023-2025 bull). If all three show positive returns across diverse conditions, you have stronger diversification than any automated pipeline could provide — and it's diversification by *thesis*, not by parameter.

### Phase C: "Single Active + On-Deck" Model (Phase 7, Live)

At $5-10K, run **ONE strategy at a time** with full capital. Keep 1-2 as on-deck alternatives (running in paper alongside). Switch manually when:

| Trigger | Action |
|---------|--------|
| Active strategy has 3 consecutive losing months | Switch to on-deck alternative |
| Max drawdown (5%) hit | Pause, switch to defensive strategy (`risk_off_rotation`) |
| Clear regime change (e.g., SPY breaks below 200-day SMA) | Switch to regime-appropriate strategy |
| Quarterly review shows Sharpe < 0.3 over trailing 90 days | Evaluate alternatives |

This requires **zero new infrastructure** — just toggling `enabled: true/false` in `config/default.yaml`.

### Phase D: Adaptive Parameters Within Existing Strategies (Future, Phase 8+)

Instead of generating new strategy variants, make existing parameters adaptive:

- **RSI thresholds**: Use the 20th/80th percentile of RSI over trailing 60 days instead of fixed 30/70
- **ATR stop multiplier**: Widen stops when volatility is elevated (ATR > 1.5x its 60-day median)
- **Time stops**: Shorten in high-volatility regimes, lengthen in low-volatility

This is a small, testable change to each strategy class. One parameter at a time. Measurable impact.

### Phase E: Ensemble Signal Weighting (Future, Phase 8+)

Run all enabled strategies but weight signals by recent performance:

```
adjusted_strength = signal.strength * strategy_weight
strategy_weight = f(trailing_30d_sharpe)  # e.g., softmax across strategies
```

The execution engine already sorts BUY signals by strength. Multiplying by a strategy-level weight is a ~20-line change to `process_signals()`. Strategies that are working get priority; strategies that aren't get deprioritized naturally. No new variants, no backtesting pipeline, no paper trading queue.

### Phase F: Lightweight Backtest Comparison Tool (Future)

If you still want some automation later, build a simple **quarterly comparison** tool:

```bash
bread compare --strategies etf_momentum,bb_mean_reversion,risk_off_rotation \
              --start 2023-01-01 --end 2025-12-31
```

This runs backtests for the 2-3 curated strategies, produces a side-by-side metrics table, and lets you make an informed manual decision. No parameter sweeps, no variant generation — just comparing your curated strategies on recent data.

---

## If You Still Want to Build the Pipeline Later (Phase 9+)

After 6+ months of live trading with real capital, if you've validated the base system and want to experiment, here are the guardrails that would make it less dangerous:

### Backtest Selection Criteria (Minimum Thresholds)

| Metric | Gate | Rationale |
|--------|------|-----------|
| `total_trades` | >= 30 | Statistical minimum for inference |
| `sharpe_ratio` | >= 0.5 + 0.1*ln(N) where N=variants tested | Bonferroni-style multiple testing correction |
| `profit_factor` | >= 1.3 | Meaningful edge above costs |
| `max_drawdown_pct` | <= 12% | Must survive within 2x the live limit |
| `win_rate_pct` | >= 40% | Floor for swing trading |
| `cagr_pct` | >= 8% | Must meaningfully beat risk-free |
| Walk-forward validation Sharpe | >= 60% of training Sharpe, absolute >= 0.3 | Out-of-sample confirmation |
| Correlation with existing live strategy | < 0.85 | Avoid near-duplicates |

### Paper Trading Promotion Criteria

| Criterion | Threshold |
|-----------|-----------|
| Minimum duration | >= 30 trading days |
| Minimum trades | >= 8 round-trips |
| Win rate deviation from backtest | Within 15 percentage points |
| Paper profit factor | >= 1.0 |
| Paper max drawdown | <= 10% |

### Live Retirement Criteria

| Trigger | Threshold |
|---------|-----------|
| Strategy drawdown | >= 5% (matches live config) |
| Rolling 60-day Sharpe | < 0.3 |
| Consecutive losing trades | >= 5 (triggers manual review) |
| 60-day expectancy | Negative (avg P&L per trade < $0) |

### Risk Controls for Live

The existing risk system is well-designed. Additional controls for automated strategy rotation:

1. **Cooling-off period**: After retiring a strategy, wait 1 trading day before deploying replacement (prevents whipsawing)
2. **Max retirements per month**: 2 (prevents cascading failures)
3. **Fallback strategy**: Always keep `risk_off_rotation` as the "last resort" — it's the most defensive
4. **Human approval gate for live promotion**: Even with automation, require manual confirmation before any strategy goes live with real money

---

## Summary

| Approach | Risk | Effort | Expected Value |
|----------|------|--------|----------------|
| Full automated pipeline now | Very High (overfitting, complexity) | 100+ hours | Negative |
| Curate 2-3 strategies + manual switching | Low | 4-8 hours | Positive |
| Adaptive parameters (Phase 8) | Low-Medium | 20-30 hours | Positive |
| Ensemble weighting (Phase 8) | Low | 10-15 hours | Positive |
| Full pipeline (Phase 9+, if proven) | Medium | 80+ hours | Uncertain |

**Bottom line**: The idea is sound in theory but premature and statistically dangerous at $5-20K with <100 trades/year. Focus on proving the base system works first (Phases 6-7), then add lightweight intelligence (adaptive params, ensemble weighting) that doesn't multiply testing bias. Revisit full automation only after 6+ months of live data confirms real edge exists.
