"""Integration test — full ETF Momentum backtest with real Alpaca data."""

from __future__ import annotations

import math
from datetime import date

import pytest

from bread.backtest.data_feed import HistoricalDataFeed
from bread.backtest.engine import BacktestEngine
from bread.core.config import CONFIG_DIR, load_config
from bread.data.alpaca_data import AlpacaDataProvider
from bread.strategy.etf_momentum import EtfMomentum


@pytest.mark.integration
class TestFullBacktest:
    def test_etf_momentum_2024(self) -> None:
        config = load_config()
        provider = AlpacaDataProvider(config)

        strategy_config_path = CONFIG_DIR / "strategies" / "etf_momentum.yaml"
        strat = EtfMomentum(strategy_config_path, config.indicators)

        feed = HistoricalDataFeed(provider, config)
        start = date(2024, 1, 1)
        end = date(2024, 12, 31)
        universe_data = feed.load_universe(strat.universe, start, end)

        assert len(universe_data) > 0, "No symbols loaded"

        engine = BacktestEngine(strat, config)
        result = engine.run(universe_data, start, end)

        # Basic sanity checks
        assert result.metrics["total_trades"] > 0
        assert len(result.trades) > 0
        assert len(result.equity_curve) > 0

        # No NaN in metrics
        for key, val in result.metrics.items():
            if isinstance(val, float) and key != "profit_factor":
                assert not math.isnan(val), f"{key} is NaN"

        # profit_factor may be inf only if no losing trades
        pf = result.metrics["profit_factor"]
        if math.isinf(pf):
            losing = [t for t in result.trades if t.pnl <= 0]
            assert len(losing) == 0, "profit_factor is inf but there are losing trades"
