"""Unit tests for backtest engine."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from bread.backtest.engine import BacktestEngine
from bread.core.config import AppConfig, BacktestSettings
from bread.core.exceptions import BacktestError, StrategyError
from bread.core.models import Signal, SignalDirection
from bread.strategy.base import Strategy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bars(
    start: str = "2024-01-02",
    periods: int = 20,
    base_close: float = 100.0,
    closes: list[float] | None = None,
    lows: list[float] | None = None,
) -> pd.DataFrame:
    """Create a simple OHLCV+indicator DataFrame."""
    dates = pd.bdate_range(start=start, periods=periods, tz="UTC")
    if closes is None:
        closes = [base_close + i * 0.5 for i in range(periods)]
    if lows is None:
        lows = [c - 2.0 for c in closes]

    n = len(closes)
    df = pd.DataFrame(
        {
            "open": [c - 0.5 for c in closes],
            "high": [c + 2.0 for c in closes],
            "low": lows[:n],
            "close": closes,
            "volume": [1_000_000] * n,
            "sma_200": [90.0] * n,
            "sma_20": [95.0] * n,
            "sma_50": [93.0] * n,
            "rsi_14": [50.0] * n,
            "atr_14": [2.0] * n,
            "volume_sma_20": [1_000_000] * n,
        },
        index=pd.DatetimeIndex(dates[:n], name="timestamp"),
    )
    return df


class MockStrategy(Strategy):
    """Configurable mock strategy for engine tests."""

    def __init__(
        self,
        signals_by_date: dict[date, list[Signal]] | None = None,
        record_max_dates: bool = False,
    ) -> None:
        self._signals_by_date = signals_by_date or {}
        self.record_max_dates = record_max_dates
        self.observed_max_dates: list[date] = []

    def evaluate(self, universe: dict[str, pd.DataFrame]) -> list[Signal]:
        if self.record_max_dates:
            for sym, df in universe.items():
                self.observed_max_dates.append(df.index[-1].date())

        # Return signals for the latest date in the universe
        if universe:
            latest = max(df.index[-1].date() for df in universe.values())
            return self._signals_by_date.get(latest, [])
        return []

    @property
    def name(self) -> str:
        return "mock_strat"

    @property
    def universe(self) -> list[str]:
        return ["SPY", "QQQ", "IWM", "DIA", "XLF", "XLK"]

    @property
    def min_history_days(self) -> int:
        return 1

    @property
    def time_stop_days(self) -> int:
        return 5


def _make_signal(
    symbol: str = "SPY",
    direction: SignalDirection = SignalDirection.BUY,
    strength: float = 0.5,
    stop_loss_pct: float = 0.05,
) -> Signal:
    return Signal(
        symbol=symbol,
        direction=direction,
        strength=strength,
        stop_loss_pct=stop_loss_pct,
        strategy_name="mock_strat",
        reason=f"{direction.value} test",
        timestamp=datetime.now(UTC),
    )


def _make_config(**overrides: float) -> AppConfig:
    return AppConfig(
        mode="paper",
        alpaca={"paper_api_key": "k", "paper_secret_key": "s"},
        backtest=BacktestSettings(
            initial_capital=overrides.get("initial_capital", 10000.0),
            commission_per_trade=overrides.get("commission_per_trade", 0.0),
            slippage_pct=overrides.get("slippage_pct", 0.0),
        ),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNoLookAhead:
    def test_strategy_never_sees_future_data(self) -> None:
        bars = _make_bars(periods=10)
        sim_dates = [d.date() for d in bars.index]
        start, end = sim_dates[0], sim_dates[-1]

        strat = MockStrategy(record_max_dates=True)
        engine = BacktestEngine(strat, _make_config())
        engine.run({"SPY": bars}, start, end)

        for i, obs in enumerate(strat.observed_max_dates):
            assert obs <= sim_dates[i], f"Look-ahead at step {i}: saw {obs}, sim was {sim_dates[i]}"


class TestPositionLimit:
    def test_sixth_buy_skipped(self) -> None:
        bars = _make_bars(periods=5)
        sim_dates = [d.date() for d in bars.index]

        # On first day, emit 6 BUY signals for 6 different symbols
        signals = [
            _make_signal(sym, strength=0.9 - i * 0.1)
            for i, sym in enumerate(["SPY", "QQQ", "IWM", "DIA", "XLF", "XLK"])
        ]
        strat = MockStrategy(signals_by_date={sim_dates[0]: signals})

        # Need bars for all 6 symbols
        universe = {sym: bars.copy() for sym in ["SPY", "QQQ", "IWM", "DIA", "XLF", "XLK"]}
        engine = BacktestEngine(strat, _make_config())
        result = engine.run(universe, sim_dates[0], sim_dates[-1])

        # Only 5 should have opened (all force-closed at end)
        entry_trades = [t for t in result.trades if t.direction == SignalDirection.BUY]
        symbols_traded = {t.symbol for t in entry_trades}
        assert len(symbols_traded) == 5
        assert "XLK" not in symbols_traded  # lowest strength


class TestStopLoss:
    def test_exit_at_stop_price(self) -> None:
        # Stop loss at entry_price * (1 - 0.05) = 100 * 0.95 = 95
        # Bar 3 has low = 94 which triggers stop
        closes = [100.0] * 10
        lows = [98.0, 98.0, 98.0, 94.0, 98.0, 98.0, 98.0, 98.0, 98.0, 98.0]
        bars = _make_bars(closes=closes, lows=lows)
        sim_dates = [d.date() for d in bars.index]

        strat = MockStrategy(signals_by_date={
            sim_dates[0]: [_make_signal("SPY", stop_loss_pct=0.05)],
        })

        engine = BacktestEngine(strat, _make_config(slippage_pct=0.0))
        result = engine.run({"SPY": bars}, sim_dates[0], sim_dates[-1])

        stop_trades = [t for t in result.trades if t.exit_reason == "stop_loss"]
        assert len(stop_trades) == 1
        assert stop_trades[0].exit_price == pytest.approx(95.0, abs=0.01)


class TestTimeStop:
    def test_exit_after_time_stop_days(self) -> None:
        bars = _make_bars(periods=10, base_close=100.0)
        sim_dates = [d.date() for d in bars.index]

        strat = MockStrategy(signals_by_date={
            sim_dates[0]: [_make_signal("SPY")],
        })
        # time_stop_days = 5, so exit on 6th bar (entry=day0, held 5 bars after)

        engine = BacktestEngine(strat, _make_config(slippage_pct=0.0))
        result = engine.run({"SPY": bars}, sim_dates[0], sim_dates[-1])

        time_stops = [t for t in result.trades if t.exit_reason == "time_stop"]
        assert len(time_stops) == 1
        assert time_stops[0].exit_date == sim_dates[5]


class TestForceCloseAtEnd:
    def test_open_positions_force_closed(self) -> None:
        bars = _make_bars(periods=3, base_close=100.0)
        sim_dates = [d.date() for d in bars.index]

        # Buy on first day, no sell signal ever, only 3 days so time_stop (5) won't trigger
        strat = MockStrategy(signals_by_date={
            sim_dates[0]: [_make_signal("SPY")],
        })

        engine = BacktestEngine(strat, _make_config(slippage_pct=0.0))
        result = engine.run({"SPY": bars}, sim_dates[0], sim_dates[-1])

        assert len(result.trades) == 1
        assert result.trades[0].exit_reason == "backtest_end"
        assert result.trades[0].exit_date == sim_dates[-1]


class TestSlippage:
    def test_entry_price_includes_slippage(self) -> None:
        bars = _make_bars(periods=5, base_close=100.0)
        sim_dates = [d.date() for d in bars.index]

        strat = MockStrategy(signals_by_date={
            sim_dates[0]: [_make_signal("SPY")],
        })

        engine = BacktestEngine(strat, _make_config(slippage_pct=0.001))
        result = engine.run({"SPY": bars}, sim_dates[0], sim_dates[-1])

        assert result.trades[0].entry_price == pytest.approx(100.0 * 1.001, abs=0.01)


class TestSellWithoutPosition:
    def test_sell_ignored_when_no_position(self) -> None:
        bars = _make_bars(periods=5)
        sim_dates = [d.date() for d in bars.index]

        strat = MockStrategy(signals_by_date={
            sim_dates[0]: [_make_signal("SPY", direction=SignalDirection.SELL)],
        })

        engine = BacktestEngine(strat, _make_config())
        result = engine.run({"SPY": bars}, sim_dates[0], sim_dates[-1])
        assert len(result.trades) == 0


class TestNoSameDayReentry:
    def test_no_reentry_after_exit_on_same_day(self) -> None:
        bars = _make_bars(periods=5, base_close=100.0)
        sim_dates = [d.date() for d in bars.index]

        # Day 0: buy. Day 2: sell + buy (re-entry attempt)
        strat = MockStrategy(signals_by_date={
            sim_dates[0]: [_make_signal("SPY")],
            sim_dates[2]: [
                _make_signal("SPY", direction=SignalDirection.SELL),
                _make_signal("SPY", direction=SignalDirection.BUY),
            ],
        })

        engine = BacktestEngine(strat, _make_config(slippage_pct=0.0))
        result = engine.run({"SPY": bars}, sim_dates[0], sim_dates[-1])

        # Should have exactly 1 closed trade (the sell on day 2)
        # and no re-entry on day 2
        spy_entries = [t for t in result.trades if t.entry_date == sim_dates[2]]
        assert len(spy_entries) == 0


class TestDeterministicOrdering:
    def test_stronger_signal_processed_first(self) -> None:
        # Use low price so capital_per_position (10000/5=2000) affords shares
        bars_a = _make_bars(periods=5, base_close=100.0)
        bars_b = _make_bars(periods=5, base_close=100.0)
        sim_dates = [d.date() for d in bars_a.index]

        strat = MockStrategy(signals_by_date={
            sim_dates[0]: [
                _make_signal("QQQ", strength=0.3),
                _make_signal("SPY", strength=0.8),
            ],
        })

        engine = BacktestEngine(strat, _make_config(slippage_pct=0.0))
        result = engine.run({"SPY": bars_a, "QQQ": bars_b}, sim_dates[0], sim_dates[-1])

        # Both should open; force-close preserves insertion order (strongest first)
        entries = [t for t in result.trades if t.entry_date == sim_dates[0]]
        assert len(entries) == 2
        assert entries[0].symbol == "SPY"  # stronger signal processed first

    def test_tie_broken_by_symbol_name(self) -> None:
        bars_a = _make_bars(periods=5, base_close=100.0)
        bars_b = _make_bars(periods=5, base_close=100.0)
        sim_dates = [d.date() for d in bars_a.index]

        strat = MockStrategy(signals_by_date={
            sim_dates[0]: [
                _make_signal("SPY", strength=0.5),
                _make_signal("QQQ", strength=0.5),
            ],
        })

        engine = BacktestEngine(strat, _make_config(slippage_pct=0.0))
        result = engine.run({"SPY": bars_a, "QQQ": bars_b}, sim_dates[0], sim_dates[-1])

        entries = [t for t in result.trades if t.entry_date == sim_dates[0]]
        assert len(entries) == 2
        assert entries[0].symbol == "QQQ"  # alphabetically first at same strength


class TestInvalidSignal:
    def test_invalid_strength_raises(self) -> None:
        bars = _make_bars(periods=5)
        sim_dates = [d.date() for d in bars.index]

        bad_signal = Signal(
            symbol="SPY",
            direction=SignalDirection.BUY,
            strength=0.5,  # valid at creation
            stop_loss_pct=0.05,
            strategy_name="wrong_name",  # mismatch!
            reason="test",
            timestamp=datetime.now(UTC),
        )
        strat = MockStrategy(signals_by_date={sim_dates[0]: [bad_signal]})

        engine = BacktestEngine(strat, _make_config())
        with pytest.raises(StrategyError, match="strategy_name"):
            engine.run({"SPY": bars}, sim_dates[0], sim_dates[-1])


class TestEmptyUniverse:
    def test_empty_raises_backtest_error(self) -> None:
        strat = MockStrategy()
        engine = BacktestEngine(strat, _make_config())
        with pytest.raises(BacktestError, match="empty"):
            engine.run({}, date(2024, 1, 1), date(2024, 12, 31))
