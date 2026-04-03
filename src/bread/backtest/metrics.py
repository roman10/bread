"""Backtest performance metrics."""

from __future__ import annotations

import math

import pandas as pd

from bread.backtest.engine import Trade


def compute_metrics(
    trades: list[Trade],
    equity_curve: pd.Series,
    initial_capital: float,
) -> dict[str, float | int]:
    """Compute backtest performance metrics."""
    total_trades = len(trades)

    if len(equity_curve) < 2:
        return {
            "total_return_pct": 0.0,
            "cagr_pct": 0.0,
            "sharpe_ratio": 0.0,
            "sortino_ratio": 0.0,
            "max_drawdown_pct": 0.0,
            "win_rate_pct": 0.0,
            "profit_factor": 0.0,
            "total_trades": total_trades,
            "avg_holding_days": 0.0,
        }

    final_equity = float(equity_curve.iloc[-1])

    # Total return
    total_return_pct = (final_equity - initial_capital) / initial_capital * 100

    # CAGR
    first_date = equity_curve.index[0]
    last_date = equity_curve.index[-1]
    calendar_days = (last_date - first_date).days
    if calendar_days > 0 and final_equity > 0 and initial_capital > 0:
        cagr_pct = (math.pow(final_equity / initial_capital, 365.25 / calendar_days) - 1) * 100
    else:
        cagr_pct = 0.0

    # Daily returns
    daily_returns = equity_curve.pct_change().dropna()

    # Sharpe ratio (annualized)
    if len(daily_returns) > 0 and daily_returns.std() > 0:
        sharpe_ratio = float(daily_returns.mean() / daily_returns.std() * math.sqrt(252))
    else:
        sharpe_ratio = 0.0

    # Sortino ratio (annualized)
    negative_returns = daily_returns[daily_returns < 0]
    if len(negative_returns) > 0 and negative_returns.std() > 0:
        sortino_ratio = float(daily_returns.mean() / negative_returns.std() * math.sqrt(252))
    else:
        sortino_ratio = 0.0

    # Max drawdown
    cumulative_max = equity_curve.cummax()
    drawdown = (cumulative_max - equity_curve) / cumulative_max
    max_drawdown_pct = float(drawdown.max()) * 100

    # Trade-based metrics
    if total_trades > 0:
        winning = [t for t in trades if t.pnl > 0]
        losing = [t for t in trades if t.pnl <= 0]
        win_rate_pct = len(winning) / total_trades * 100

        winning_pnl = sum(t.pnl for t in winning)
        losing_pnl = sum(t.pnl for t in losing)

        if losing_pnl < 0:
            profit_factor = winning_pnl / abs(losing_pnl) if winning_pnl > 0 else 0.0
        elif winning_pnl > 0:
            profit_factor = float("inf")
        else:
            profit_factor = 0.0

        avg_holding_days = sum(
            (t.exit_date - t.entry_date).days
            for t in trades
            if t.exit_date is not None
        ) / total_trades
    else:
        win_rate_pct = 0.0
        profit_factor = 0.0
        avg_holding_days = 0.0

    return {
        "total_return_pct": total_return_pct,
        "cagr_pct": cagr_pct,
        "sharpe_ratio": sharpe_ratio,
        "sortino_ratio": sortino_ratio,
        "max_drawdown_pct": max_drawdown_pct,
        "win_rate_pct": win_rate_pct,
        "profit_factor": profit_factor,
        "total_trades": total_trades,
        "avg_holding_days": avg_holding_days,
    }
