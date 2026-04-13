"""Abstract strategy interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import pandas as pd
import yaml

from bread.core.exceptions import StrategyError
from bread.core.models import Signal


def load_strategy_config(config_path: Path) -> dict[str, Any]:
    """Load and validate a strategy YAML config file."""
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
    except Exception as exc:
        raise StrategyError(
            f"Failed to load strategy config: {config_path}: {exc}"
        ) from exc

    if not isinstance(cfg, dict):
        raise StrategyError(f"Invalid strategy config format in {config_path}")

    return cfg


class Strategy(ABC):
    _universe: list[str]
    _required_cols: set[str]
    accepts_claude_client: ClassVar[bool] = False  # set True in strategies that accept ClaudeClient

    @abstractmethod
    def evaluate(self, universe: dict[str, pd.DataFrame]) -> list[Signal]:
        """Evaluate the strategy on enriched OHLCV+indicator DataFrames.

        Args:
            universe: mapping of symbol -> DataFrame with OHLCV + indicator columns.
                      Each DataFrame has a UTC DatetimeIndex named 'timestamp',
                      sorted ascending, with indicator columns from compute_indicators().

        Returns:
            List of Signal objects. May be empty if no conditions are met.
        """
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique strategy identifier (e.g. 'etf_momentum')."""
        ...

    @property
    @abstractmethod
    def universe(self) -> list[str]:
        """List of symbols this strategy trades."""
        ...

    @property
    @abstractmethod
    def min_history_days(self) -> int:
        """Minimum number of trading days of history required for evaluation."""
        ...

    @property
    @abstractmethod
    def time_stop_days(self) -> int:
        """Number of trading bars to hold before a time-stop exit."""
        ...

    def _evaluate_universe(
        self,
        universe: dict[str, pd.DataFrame],
        evaluate_fn: Callable[[str, pd.DataFrame], Signal | None],
    ) -> list[Signal]:
        """Validate DataFrames and collect signals for each symbol.

        Iterates over the strategy's universe, validates columns, and calls
        the per-symbol evaluation function.
        """
        signals: list[Signal] = []

        for symbol in self._universe:
            if symbol not in universe:
                continue

            df = universe[symbol]
            if df.empty:
                raise StrategyError(f"Empty DataFrame for {symbol}")

            missing = self._required_cols - set(df.columns)
            if missing:
                raise StrategyError(
                    f"Missing indicator columns for {symbol}: {missing}"
                )

            signal = evaluate_fn(symbol, df)
            if signal is not None:
                signals.append(signal)

        return signals
