"""Unit tests for backtest metrics."""

from __future__ import annotations

import math
from datetime import date

import pandas as pd
import pytest

from bread.backtest.metrics import compute_metrics
from bread.backtest.models import Trade
from bread.core.models import SignalDirection


def _make_trade(
    pnl: float,
    entry_date: date = date(2024, 1, 2),
    exit_date: date = date(2024, 1, 16),
) -> Trade:
    return Trade(
        symbol="SPY",
        direction=SignalDirection.BUY,
        entry_date=entry_date,
        entry_price=100.0,
        exit_date=exit_date,
        exit_price=100.0 + pnl / 10,
        shares=10,
        pnl=pnl,
        exit_reason="test",
    )


class TestMetricsBasic:
    def test_total_return_pct(self) -> None:
        equity = pd.Series(
            [10000.0, 10050.0, 10100.0, 10050.0],
            index=[date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)],
        )
        trades = [_make_trade(100.0), _make_trade(-50.0)]
        m = compute_metrics(trades, equity, 10000.0)

        assert m["total_return_pct"] == pytest.approx(0.5, abs=0.01)
        assert m["total_trades"] == 2

    def test_win_rate(self) -> None:
        equity = pd.Series(
            [10000.0, 10100.0, 10050.0],
            index=[date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
        )
        trades = [_make_trade(100.0), _make_trade(-50.0)]
        m = compute_metrics(trades, equity, 10000.0)

        assert m["win_rate_pct"] == pytest.approx(50.0)

    def test_profit_factor(self) -> None:
        equity = pd.Series(
            [10000.0, 10100.0, 10050.0],
            index=[date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
        )
        trades = [_make_trade(100.0), _make_trade(-50.0)]
        m = compute_metrics(trades, equity, 10000.0)

        assert m["profit_factor"] == pytest.approx(2.0)

    def test_profit_factor_no_losers(self) -> None:
        equity = pd.Series(
            [10000.0, 10100.0, 10200.0],
            index=[date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
        )
        trades = [_make_trade(100.0), _make_trade(100.0)]
        m = compute_metrics(trades, equity, 10000.0)

        assert m["profit_factor"] == float("inf")

    def test_sharpe_ratio(self) -> None:
        equity = pd.Series(
            [10000.0, 10050.0, 10100.0, 10050.0],
            index=[date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)],
        )
        trades = [_make_trade(50.0)]
        m = compute_metrics(trades, equity, 10000.0)

        # Manually verify it's a reasonable number and not NaN
        assert isinstance(m["sharpe_ratio"], float)
        assert not math.isnan(m["sharpe_ratio"])

    def test_max_drawdown(self) -> None:
        # Peak at 10200, trough at 10000 => drawdown = 200/10200 * 100 ≈ 1.96%
        equity = pd.Series(
            [10000.0, 10200.0, 10000.0, 10100.0],
            index=[date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)],
        )
        trades = [_make_trade(100.0)]
        m = compute_metrics(trades, equity, 10000.0)

        expected_dd = (10200.0 - 10000.0) / 10200.0 * 100
        assert m["max_drawdown_pct"] == pytest.approx(expected_dd, abs=0.01)

    def test_avg_holding_days(self) -> None:
        t1 = _make_trade(100.0, entry_date=date(2024, 1, 2), exit_date=date(2024, 1, 12))  # 10 days
        t2 = _make_trade(-50.0, entry_date=date(2024, 1, 2), exit_date=date(2024, 1, 22))  # 20 days
        equity = pd.Series(
            [10000.0, 10050.0],
            index=[date(2024, 1, 2), date(2024, 1, 22)],
        )
        m = compute_metrics([t1, t2], equity, 10000.0)
        assert m["avg_holding_days"] == pytest.approx(15.0)


class TestMetricsEdgeCases:
    def test_single_equity_point(self) -> None:
        equity = pd.Series([10000.0], index=[date(2024, 1, 2)])
        m = compute_metrics([], equity, 10000.0)

        assert m["total_return_pct"] == 0.0
        assert m["sharpe_ratio"] == 0.0
        assert m["max_drawdown_pct"] == 0.0
        assert m["total_trades"] == 0

    def test_no_trades(self) -> None:
        equity = pd.Series(
            [10000.0, 10000.0, 10000.0],
            index=[date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
        )
        m = compute_metrics([], equity, 10000.0)

        assert m["total_trades"] == 0
        assert m["win_rate_pct"] == 0.0
        assert m["profit_factor"] == 0.0
        assert m["avg_holding_days"] == 0.0

    def test_no_nan_in_normal_case(self) -> None:
        equity = pd.Series(
            [10000.0, 10100.0, 10050.0, 10150.0],
            index=[date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)],
        )
        trades = [_make_trade(100.0), _make_trade(-50.0)]
        m = compute_metrics(trades, equity, 10000.0)

        for key, val in m.items():
            if isinstance(val, float):
                assert not math.isnan(val), f"{key} is NaN"

    def test_flat_equity_returns_zero_sharpe(self) -> None:
        equity = pd.Series(
            [10000.0, 10000.0, 10000.0, 10000.0],
            index=[date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)],
        )
        m = compute_metrics([], equity, 10000.0)
        assert m["sharpe_ratio"] == 0.0
        assert m["sortino_ratio"] == 0.0
        assert m["total_return_pct"] == 0.0

    def test_all_losing_trades(self) -> None:
        equity = pd.Series(
            [10000.0, 9800.0, 9600.0],
            index=[date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
        )
        trades = [_make_trade(-200.0), _make_trade(-200.0)]
        m = compute_metrics(trades, equity, 10000.0)

        assert m["win_rate_pct"] == 0.0
        assert m["profit_factor"] == 0.0
        assert m["total_return_pct"] < 0

    def test_zero_duration_trade(self) -> None:
        """Trade entered and exited on the same date."""
        t = _make_trade(50.0, entry_date=date(2024, 1, 2), exit_date=date(2024, 1, 2))
        equity = pd.Series(
            [10000.0, 10050.0],
            index=[date(2024, 1, 2), date(2024, 1, 3)],
        )
        m = compute_metrics([t], equity, 10000.0)
        assert m["avg_holding_days"] == 0.0
        assert m["total_trades"] == 1

    def test_negative_equity(self) -> None:
        """Drawdown still computable when equity dips (extreme scenario)."""
        equity = pd.Series(
            [10000.0, 5000.0, 8000.0],
            index=[date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
        )
        m = compute_metrics([], equity, 10000.0)
        assert m["max_drawdown_pct"] == pytest.approx(50.0)
        assert m["total_return_pct"] == pytest.approx(-20.0)
