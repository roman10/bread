"""Tests for multi-strategy backtest orchestration.

Each strategy must run as an isolated sub-account: per-strategy equity, no
cross-strategy borrowing, and aggregate equity = sum of sub-books date-aligned.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from bread.backtest.models import BacktestResult
from bread.backtest.multi import run_multi_strategy_backtest
from bread.core.exceptions import BacktestError


def _result(
    capital: float,
    equity_points: dict,  # type: ignore[type-arg]
    trades: int = 0,
    return_pct: float = 0.0,
) -> BacktestResult:
    curve = pd.Series(equity_points, name="equity")
    curve.index = pd.to_datetime(list(equity_points)).date
    curve.index.name = "date"  # type: ignore[attr-defined]
    return BacktestResult(
        trades=[MagicMock(pnl=100.0) for _ in range(trades)],
        equity_curve=curve,
        metrics={
            "total_return_pct": return_pct,
            "cagr_pct": 0.0,
            "sharpe_ratio": 0.5,
            "sortino_ratio": 0.5,
            "max_drawdown_pct": 0.0,
            "win_rate_pct": 50.0,
            "profit_factor": 1.2,
            "total_trades": trades,
            "avg_holding_days": 5.0,
        },
        initial_capital=capital,
        final_equity=curve.iloc[-1],
    )


@patch("bread.backtest.multi.run_strategy_backtest")
def test_per_strategy_sub_accounts_split_capital(
    mock_runner: MagicMock, tmp_path
) -> None:
    """Two strategies each get initial_capital / 2 — fair isolation."""
    from datetime import date as _d

    from bread.core.config import AppConfig, BacktestSettings

    captured_capitals: list[float] = []

    def side_effect(name, start, end, *, config, **_kw):
        captured_capitals.append(config.backtest.initial_capital)
        return _result(
            capital=config.backtest.initial_capital,
            equity_points={
                "2024-01-01": config.backtest.initial_capital,
                "2024-01-02": config.backtest.initial_capital * 1.10,
            },
            trades=3,
            return_pct=10.0,
        )

    mock_runner.side_effect = side_effect

    cfg = AppConfig(
        mode="paper",
        backtest=BacktestSettings(initial_capital=10_000.0),
        alpaca={
            "paper_api_key": "x",
            "paper_secret_key": "y",
        },
    )

    multi = run_multi_strategy_backtest(
        ["strat_a", "strat_b"],
        _d(2024, 1, 1), _d(2024, 1, 2),
        config=cfg,
        session_factory=MagicMock(),
        provider=MagicMock(),
        universe_registry=MagicMock(),
    )

    assert captured_capitals == [5_000.0, 5_000.0]
    assert set(multi.per_strategy) == {"strat_a", "strat_b"}
    # Aggregate equity curve = sum of sub-accounts at each date
    agg = multi.aggregate.equity_curve
    assert float(agg.iloc[0]) == pytest.approx(10_000.0)
    assert float(agg.iloc[-1]) == pytest.approx(11_000.0)  # 5500 * 2


@patch("bread.backtest.multi.run_strategy_backtest")
def test_failed_strategy_recorded_others_run(
    mock_runner: MagicMock,
) -> None:
    """A failing strategy does not abort the whole multi-run."""
    from datetime import date as _d

    from bread.core.config import AppConfig, BacktestSettings

    def side_effect(name, *args, **kwargs):
        if name == "broken":
            raise BacktestError("no bars available")
        cap = kwargs["config"].backtest.initial_capital
        return _result(
            capital=cap,
            equity_points={"2024-01-01": cap, "2024-01-02": cap * 1.05},
        )

    mock_runner.side_effect = side_effect

    cfg = AppConfig(
        mode="paper",
        backtest=BacktestSettings(initial_capital=10_000.0),
        alpaca={"paper_api_key": "x", "paper_secret_key": "y"},
    )

    multi = run_multi_strategy_backtest(
        ["good", "broken"],
        _d(2024, 1, 1), _d(2024, 1, 2),
        config=cfg,
        session_factory=MagicMock(),
        provider=MagicMock(),
        universe_registry=MagicMock(),
    )

    assert "good" in multi.per_strategy
    assert multi.failures == {"broken": "no bars available"}


@patch("bread.backtest.multi.run_strategy_backtest")
def test_all_strategies_failed_raises(mock_runner: MagicMock) -> None:
    from datetime import date as _d

    from bread.core.config import AppConfig, BacktestSettings

    mock_runner.side_effect = BacktestError("boom")
    cfg = AppConfig(
        mode="paper",
        backtest=BacktestSettings(initial_capital=10_000.0),
        alpaca={"paper_api_key": "x", "paper_secret_key": "y"},
    )
    with pytest.raises(BacktestError, match="All strategies failed"):
        run_multi_strategy_backtest(
            ["a", "b"],
            _d(2024, 1, 1), _d(2024, 1, 2),
            config=cfg,
            session_factory=MagicMock(),
            provider=MagicMock(),
            universe_registry=MagicMock(),
        )


def test_empty_strategy_list_raises() -> None:
    from datetime import date as _d

    from bread.core.config import AppConfig, BacktestSettings

    cfg = AppConfig(
        mode="paper",
        backtest=BacktestSettings(initial_capital=10_000.0),
        alpaca={"paper_api_key": "x", "paper_secret_key": "y"},
    )
    with pytest.raises(BacktestError):
        run_multi_strategy_backtest(
            [],
            _d(2024, 1, 1), _d(2024, 1, 2),
            config=cfg,
            session_factory=MagicMock(),
            provider=MagicMock(),
            universe_registry=MagicMock(),
        )


@patch("bread.backtest.multi.run_strategy_backtest")
def test_aggregate_equity_matches_sum_of_independent_runs(
    mock_runner: MagicMock,
) -> None:
    """Multi-run's aggregate = independent single-runs summed date-aligned.

    This is the core fairness claim: running strategies together with
    sub-accounts is equivalent to running them independently and merging.
    """
    from datetime import date as _d

    from bread.core.config import AppConfig, BacktestSettings

    def side_effect(name, start, end, *, config, **_kw):
        cap = config.backtest.initial_capital
        if name == "a":
            # +20% over 3 days
            return _result(cap, {"2024-01-01": cap, "2024-01-03": cap * 1.20})
        return _result(cap, {"2024-01-02": cap, "2024-01-03": cap * 0.90})

    mock_runner.side_effect = side_effect

    cfg = AppConfig(
        mode="paper",
        backtest=BacktestSettings(initial_capital=10_000.0),
        alpaca={"paper_api_key": "x", "paper_secret_key": "y"},
    )

    multi = run_multi_strategy_backtest(
        ["a", "b"],
        _d(2024, 1, 1), _d(2024, 1, 3),
        config=cfg,
        session_factory=MagicMock(),
        provider=MagicMock(),
        universe_registry=MagicMock(),
    )

    final = float(multi.aggregate.equity_curve.iloc[-1])
    # a: 5000 -> 6000 (+20%), b: 5000 -> 4500 (-10%). Sum = 10500.
    assert final == pytest.approx(10_500.0)
