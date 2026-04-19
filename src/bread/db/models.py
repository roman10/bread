"""SQLAlchemy ORM models."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class MarketDataCache(Base):
    __tablename__ = "market_data_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    timeframe: Mapped[str] = mapped_column(String, nullable=False)
    timestamp_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "timestamp_utc", name="uq_symbol_tf_ts"),
        Index("ix_symbol_tf_ts", "symbol", "timeframe", "timestamp_utc"),
    )


class SignalLog(Base):
    __tablename__ = "signals_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_name: Mapped[str] = mapped_column(String, nullable=False)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    direction: Mapped[str] = mapped_column(String, nullable=False)
    strength: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss_pct: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    signal_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_signals_strategy_symbol", "strategy_name", "symbol"),
    )


class OrderLog(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    broker_order_id: Mapped[str | None] = mapped_column(String, nullable=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    stop_loss_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_filled_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    filled_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    strategy_name: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    filled_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_orders_symbol_status", "symbol", "status"),
    )


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    equity: Mapped[float] = mapped_column(Float, nullable=False)
    cash: Mapped[float] = mapped_column(Float, nullable=False)
    positions_value: Mapped[float] = mapped_column(Float, nullable=False)
    open_positions: Mapped[int] = mapped_column(Integer, nullable=False)
    daily_pnl: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        Index("ix_snapshots_ts", "timestamp_utc"),
    )


class StrategySnapshot(Base):
    """Per-strategy equity snapshot — enables fair per-strategy equity curves.

    Written each tick alongside PortfolioSnapshot. Realized P&L accumulates
    from closed BUY/SELL pairs attributed to this strategy; unrealized comes
    from currently-open positions the strategy holds at last known price.
    Equity = realized + unrealized (deltas from a notional per-strategy
    baseline — the dashboard charts the curve shape, not absolute equity).
    """

    __tablename__ = "strategy_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    strategy_name: Mapped[str] = mapped_column(String, nullable=False)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False)
    unrealized_pnl: Mapped[float] = mapped_column(Float, nullable=False)
    equity: Mapped[float] = mapped_column(Float, nullable=False)
    open_positions: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        Index("ix_strategy_snapshots_strat_ts", "strategy_name", "timestamp_utc"),
    )


class ClaudeUsageLog(Base):
    __tablename__ = "claude_usage_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    called_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    model: Mapped[str] = mapped_column(String, nullable=False)
    use_case: Mapped[str] = mapped_column(String, nullable=False)
    prompt_length: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_claude_usage_called_at", "called_at_utc"),
    )


class EventAlertLog(Base):
    __tablename__ = "event_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    headline: Mapped[str] = mapped_column(String, nullable=False)
    details: Mapped[str] = mapped_column(String, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False, default="")
    scan_summary: Mapped[str] = mapped_column(String, nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    scanned_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_event_alerts_symbol_scanned", "symbol", "scanned_at_utc"),
        Index("ix_event_alerts_active", "is_active", "scanned_at_utc"),
    )
