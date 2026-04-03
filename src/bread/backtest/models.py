"""Backtest domain models — shared by engine, metrics, and tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from bread.core.models import SignalDirection


@dataclass
class Trade:
    symbol: str
    direction: SignalDirection
    entry_date: date
    entry_price: float
    exit_date: date | None = None
    exit_price: float | None = None
    shares: int = 0
    stop_loss_price: float | None = None
    pnl: float = 0.0
    exit_reason: str = ""
    _trading_days_held: int = field(default=0, repr=False)


@dataclass
class BacktestResult:
    trades: list[Trade]
    equity_curve: pd.Series
    metrics: dict[str, float | int]
    initial_capital: float
    final_equity: float
