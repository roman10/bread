"""Backtest engine — simulates strategy execution over historical data."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from bread.core.config import AppConfig
from bread.core.exceptions import BacktestError, StrategyError
from bread.core.models import SignalDirection
from bread.strategy.base import Strategy

logger = logging.getLogger(__name__)

MAX_POSITIONS = 5


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


class BacktestEngine:
    def __init__(self, strategy: Strategy, config: AppConfig) -> None:
        self._strategy = strategy
        self._config = config
        self._slippage_pct = config.backtest.slippage_pct
        self._commission = config.backtest.commission_per_trade
        self._initial_capital = config.backtest.initial_capital

    def run(
        self,
        universe_data: dict[str, pd.DataFrame],
        start: date,
        end: date,
    ) -> BacktestResult:
        """Run the backtest over the date range."""
        if not universe_data:
            raise BacktestError("universe_data is empty — no symbols to backtest")

        # Collect sorted union of all bar dates in [start, end]
        all_dates: set[date] = set()
        for df in universe_data.values():
            dates = df.index.date  # type: ignore[attr-defined]
            all_dates.update(d for d in dates if start <= d <= end)

        sim_dates = sorted(all_dates)
        if not sim_dates:
            raise BacktestError(f"No trading dates found in [{start}, {end}]")

        logger.info(
            "Backtest: %s | %s to %s | %d symbols, %d trading days",
            self._strategy.name, start, end, len(universe_data), len(sim_dates),
        )

        cash = self._initial_capital
        positions: dict[str, Trade] = {}  # symbol -> open Trade
        closed_trades: list[Trade] = []
        equity_points: dict[date, float] = {}
        last_known_close: dict[str, float] = {}

        for sim_date in sim_dates:
            exited_today: set[str] = set()

            # Step 1: Slice data up to current date (no look-ahead)
            sliced: dict[str, pd.DataFrame] = {}
            current_bars: dict[str, pd.Series] = {}
            for symbol, df in universe_data.items():
                mask = df.index.date <= sim_date  # type: ignore[attr-defined]
                s = df.loc[mask]
                if s.empty:
                    continue
                sliced[symbol] = s
                bar_date = s.index[-1].date()
                if bar_date == sim_date:
                    current_bars[symbol] = s.iloc[-1]
                    last_known_close[symbol] = float(s.iloc[-1]["close"])

            # Step 2: Check exits for open positions with a bar today
            for symbol in list(positions):
                if symbol not in current_bars:
                    # No bar today — carry forward for equity, skip exit checks
                    continue

                trade = positions[symbol]
                bar = current_bars[symbol]

                # Increment trading days held (entry day doesn't count)
                if trade.entry_date != sim_date:
                    trade._trading_days_held += 1

                # Stop loss check
                if trade.stop_loss_price is not None and float(bar["low"]) <= trade.stop_loss_price:
                    cash = self._close_position(
                        trade, sim_date, trade.stop_loss_price, "stop_loss",
                        cash, closed_trades,
                    )
                    del positions[symbol]
                    exited_today.add(symbol)
                    continue

                # Time stop check
                if trade._trading_days_held >= self._strategy.time_stop_days:
                    cash = self._close_position(
                        trade, sim_date, float(bar["close"]), "time_stop",
                        cash, closed_trades,
                    )
                    del positions[symbol]
                    exited_today.add(symbol)
                    continue

            # Step 3: Evaluate strategy
            signals = self._strategy.evaluate(sliced)

            # Step 4: Validate signals
            for sig in signals:
                if sig.strategy_name != self._strategy.name:
                    raise StrategyError(
                        f"Signal strategy_name '{sig.strategy_name}' != '{self._strategy.name}'"
                    )
                if not 0.0 <= sig.strength <= 1.0:
                    raise StrategyError(f"Invalid signal strength: {sig.strength}")
                if sig.stop_loss_pct <= 0:
                    raise StrategyError(f"Invalid stop_loss_pct: {sig.stop_loss_pct}")
                if sig.symbol not in sliced:
                    raise StrategyError(f"Signal symbol '{sig.symbol}' not in sliced universe")

            # Step 5: Apply SELL signals
            for sig in signals:
                if sig.direction != SignalDirection.SELL:
                    continue
                if sig.symbol not in positions:
                    logger.debug("SELL signal for %s ignored — no open position", sig.symbol)
                    continue
                if sig.symbol not in current_bars:
                    continue
                trade = positions[sig.symbol]
                close_price = float(current_bars[sig.symbol]["close"])
                cash = self._close_position(
                    trade, sim_date, close_price, sig.reason,
                    cash, closed_trades,
                )
                del positions[sig.symbol]
                exited_today.add(sig.symbol)

            # Step 6: Process BUY signals
            buy_signals = [
                s for s in signals
                if s.direction == SignalDirection.BUY
                and s.symbol not in positions
                and s.symbol not in exited_today
            ]
            buy_signals.sort(key=lambda s: (-s.strength, s.symbol))

            equity_before_entries = cash + sum(
                t.shares * last_known_close.get(sym, t.entry_price)
                for sym, t in positions.items()
            )
            capital_per_position = equity_before_entries * (1.0 / MAX_POSITIONS)

            for sig in buy_signals:
                if len(positions) >= MAX_POSITIONS:
                    logger.debug("Position limit reached, skipping BUY for %s", sig.symbol)
                    break
                if sig.symbol in positions:
                    continue
                if sig.symbol not in current_bars:
                    continue

                close_price = float(current_bars[sig.symbol]["close"])
                entry_price = close_price * (1.0 + self._slippage_pct)
                shares = math.floor(capital_per_position / entry_price)

                if shares <= 0:
                    logger.debug("Zero shares for %s, skipping", sig.symbol)
                    continue

                cost = shares * entry_price + self._commission
                if cost > cash:
                    logger.debug("Insufficient cash for %s", sig.symbol)
                    continue

                cash -= cost
                stop_loss_price = entry_price * (1.0 - sig.stop_loss_pct)

                trade = Trade(
                    symbol=sig.symbol,
                    direction=SignalDirection.BUY,
                    entry_date=sim_date,
                    entry_price=entry_price,
                    shares=shares,
                    stop_loss_price=stop_loss_price,
                )
                positions[sig.symbol] = trade
                logger.info(
                    "ENTRY symbol=%s shares=%d price=%.2f stop=%.2f",
                    sig.symbol, shares, entry_price, stop_loss_price,
                )

            # Step 7: Record equity
            position_value = sum(
                t.shares * last_known_close.get(sym, t.entry_price)
                for sym, t in positions.items()
            )
            equity_points[sim_date] = cash + position_value

        # Force-close open positions at end
        for symbol in list(positions):
            trade = positions[symbol]
            close_price = last_known_close.get(symbol, trade.entry_price)
            cash = self._close_position(
                trade, sim_dates[-1], close_price, "backtest_end",
                cash, closed_trades,
            )
            del positions[symbol]

        # Update final equity point to reflect force-close commissions
        if sim_dates:
            equity_points[sim_dates[-1]] = cash

        # Build equity curve
        equity_curve = pd.Series(equity_points, name="equity")
        equity_curve.index.name = "date"

        # Compute metrics
        from bread.backtest.metrics import compute_metrics

        metrics = compute_metrics(closed_trades, equity_curve, self._initial_capital)

        final_equity = (
            float(equity_curve.iloc[-1]) if len(equity_curve) > 0 else self._initial_capital
        )

        logger.info(
            "Backtest complete: %d trades, total_return=%.2f%%",
            len(closed_trades), metrics.get("total_return_pct", 0.0),
        )

        return BacktestResult(
            trades=closed_trades,
            equity_curve=equity_curve,
            metrics=metrics,
            initial_capital=self._initial_capital,
            final_equity=final_equity,
        )

    def _close_position(
        self,
        trade: Trade,
        exit_date: date,
        exit_price: float,
        exit_reason: str,
        cash: float,
        closed_trades: list[Trade],
    ) -> float:
        """Close a position and return updated cash."""
        trade.exit_date = exit_date
        trade.exit_price = exit_price
        trade.exit_reason = exit_reason
        trade.pnl = (exit_price - trade.entry_price) * trade.shares - 2 * self._commission
        cash += trade.shares * exit_price - self._commission
        closed_trades.append(trade)
        logger.info(
            "EXIT symbol=%s price=%.2f pnl=%.2f reason=%s",
            trade.symbol, exit_price, trade.pnl, exit_reason,
        )
        return cash
