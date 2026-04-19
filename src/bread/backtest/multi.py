"""Multi-strategy backtest orchestrator.

Each strategy runs as an independent sub-account with its own cash and
positions — no cross-strategy borrowing, no ordering effects. Per-strategy
results are returned alongside an aggregated equity curve (sum of sub-books
date-aligned). This is the only way to fairly compare strategies before
committing real capital.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

import pandas as pd

from bread.backtest.metrics import compute_metrics
from bread.backtest.models import BacktestResult
from bread.backtest.runner import run_strategy_backtest
from bread.core.config import AppConfig
from bread.core.exceptions import BacktestError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from bread.data.alpaca_data import AlpacaDataProvider
    from bread.data.universe import UniverseRegistry

logger = logging.getLogger(__name__)


@dataclass
class MultiBacktestResult:
    """Result of running multiple strategies with per-strategy sub-accounts.

    `per_strategy[name]` is the isolated result for that strategy. `aggregate`
    is a portfolio view where the initial capital is the sum of sub-account
    capitals and the equity curve is the date-aligned sum of per-strategy
    curves. Trade lists are concatenated for cross-strategy summaries.
    """

    per_strategy: dict[str, BacktestResult]
    aggregate: BacktestResult
    failures: dict[str, str] = field(default_factory=dict)


def run_multi_strategy_backtest(
    strategy_names: list[str],
    start: date,
    end: date,
    *,
    config: AppConfig,
    session_factory: sessionmaker[Session],
    provider: AlpacaDataProvider,
    universe_registry: UniverseRegistry,
) -> MultiBacktestResult:
    """Backtest *strategy_names* in parallel sub-accounts, return combined + per-strategy.

    Capital is evenly split across strategies so each competes on the same
    footing. Strategies that raise ``BacktestError`` are recorded in
    ``failures`` and excluded from the aggregate.
    """
    if not strategy_names:
        raise BacktestError("strategy_names is empty")

    # Even-split sub-accounts so strategies compete on equal capital.
    per_strategy_capital = config.backtest.initial_capital / len(strategy_names)
    sub_config = config.model_copy(
        update={
            "backtest": config.backtest.model_copy(
                update={"initial_capital": per_strategy_capital},
            )
        }
    )

    per_strategy: dict[str, BacktestResult] = {}
    failures: dict[str, str] = {}

    for name in strategy_names:
        try:
            per_strategy[name] = run_strategy_backtest(
                name,
                start,
                end,
                config=sub_config,
                session_factory=session_factory,
                provider=provider,
                universe_registry=universe_registry,
            )
        except BacktestError as exc:
            failures[name] = str(exc)
            logger.warning("Strategy '%s' backtest failed: %s", name, exc)

    if not per_strategy:
        raise BacktestError(
            f"All strategies failed: {failures}" if failures else "No strategies ran"
        )

    aggregate = _aggregate(per_strategy, config.backtest.initial_capital)
    return MultiBacktestResult(
        per_strategy=per_strategy,
        aggregate=aggregate,
        failures=failures,
    )


def _aggregate(
    per_strategy: dict[str, BacktestResult],
    initial_capital: float,
) -> BacktestResult:
    """Sum per-strategy equity curves date-aligned to build a portfolio curve.

    Strategies with missing dates are forward-filled (the sub-account held
    its prior equity on that calendar day). Trade lists are concatenated so
    aggregate win rate / profit factor reflect all strategies together.
    """
    if not per_strategy:
        return BacktestResult(
            trades=[],
            equity_curve=pd.Series(dtype=float, name="equity"),
            metrics={},
            initial_capital=initial_capital,
            final_equity=initial_capital,
        )

    curves = pd.DataFrame(
        {name: r.equity_curve for name, r in per_strategy.items()}
    )
    # Sort index and forward-fill gaps so per-strategy curves with different
    # first-trade dates don't drop one another's history.
    curves = curves.sort_index().ffill().fillna(
        # Days before a strategy's first bar: seed with its sub-account capital
        {
            name: r.initial_capital
            for name, r in per_strategy.items()
        }
    )
    aggregate_curve = curves.sum(axis=1)
    aggregate_curve.name = "equity"
    aggregate_curve.index.name = "date"

    all_trades = [t for r in per_strategy.values() for t in r.trades]
    final_equity = (
        float(aggregate_curve.iloc[-1]) if len(aggregate_curve) > 0 else initial_capital
    )
    metrics = compute_metrics(all_trades, aggregate_curve, initial_capital)

    return BacktestResult(
        trades=all_trades,
        equity_curve=aggregate_curve,
        metrics=metrics,
        initial_capital=initial_capital,
        final_equity=final_equity,
    )
