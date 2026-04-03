"""Domain models for strategy output and execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class SignalDirection(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class Signal:
    symbol: str
    direction: SignalDirection
    strength: float  # 0.0 to 1.0
    stop_loss_pct: float  # e.g. 0.05 for 5%
    strategy_name: str
    reason: str  # human-readable explanation
    timestamp: datetime  # when the signal was generated

    def __post_init__(self) -> None:
        if not 0.0 <= self.strength <= 1.0:
            raise ValueError(f"strength must be in [0.0, 1.0], got {self.strength}")
        if self.stop_loss_pct <= 0:
            raise ValueError(f"stop_loss_pct must be > 0, got {self.stop_loss_pct}")


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: int
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    broker_order_id: str
    strategy_name: str
    entry_date: date
