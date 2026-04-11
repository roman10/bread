"""Single-strategy backtest runner — shared by `bread backtest` and `bread compare`.

Encapsulates the per-strategy steps of a backtest (resolve config -> load
strategy class -> fetch universe bars -> run engine) so callers can run one
or many strategies against the same shared resources (config, DB session
factory, data provider, universe registry).
"""

from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING

from bread.backtest.data_feed import HistoricalDataFeed
from bread.backtest.engine import BacktestEngine
from bread.backtest.models import BacktestResult
from bread.core.config import CONFIG_DIR, AppConfig
from bread.core.exceptions import BacktestError
from bread.data.cache import CachingDataProvider
from bread.data.universe import UniverseRegistry, resolve_strategy_universe
from bread.strategy.base import load_strategy_config
from bread.strategy.registry import get_strategy

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from bread.data.alpaca_data import AlpacaDataProvider

logger = logging.getLogger(__name__)


def run_strategy_backtest(
    strategy_name: str,
    start: date,
    end: date,
    *,
    config: AppConfig,
    session_factory: sessionmaker[Session],
    provider: AlpacaDataProvider,
    universe_registry: UniverseRegistry,
) -> BacktestResult:
    """Run a backtest for a single strategy and return its result.

    Pure-function wrapper: caller owns config loading, DB engine lifecycle,
    and result printing. Multiple strategies can be backtested in series by
    calling this function in a loop while reusing the same shared resources;
    bar fetches are deduplicated automatically via `MarketDataCache`.

    Raises:
        BacktestError: if the strategy is unknown, disabled, or its universe
            cannot be loaded.
    """
    # 1. Find strategy in config (must be enabled)
    strat_settings = None
    for s in config.strategies:
        if s.name == strategy_name and s.enabled:
            strat_settings = s
            break
    if strat_settings is None:
        available = [s.name for s in config.strategies if s.enabled]
        raise BacktestError(
            f"Unknown or disabled strategy '{strategy_name}'. Enabled: {available}"
        )

    # 2. Resolve strategy config + class + universe + instance.
    # The module-level `from bread.strategy.registry import get_strategy`
    # already triggers bread/strategy/__init__.py, which auto-imports every
    # strategy module and runs its @register decorator — no extra import
    # needed here.
    cfg_path = strat_settings.config_path or f"strategies/{strat_settings.name}.yaml"
    strategy_config_path = CONFIG_DIR / cfg_path

    strategy_cls = get_strategy(strategy_name)

    strat_cfg = load_strategy_config(strategy_config_path)
    resolved_universe = resolve_strategy_universe(
        strat_cfg, universe_registry, strategy_name
    )

    strat_instance = strategy_cls(  # type: ignore[call-arg]
        strategy_config_path,
        config.indicators,
        universe=resolved_universe,
    )

    # 3. Load universe bars (cached across strategies via MarketDataCache)
    with session_factory() as session:
        cached_provider = CachingDataProvider(provider, session)
        feed = HistoricalDataFeed(cached_provider, config)
        universe_data = feed.load_universe(strat_instance.universe, start, end)

    # 4. Run backtest
    bt = BacktestEngine(strat_instance, config)
    return bt.run(universe_data, start, end)
