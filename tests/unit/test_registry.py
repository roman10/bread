"""Unit tests for strategy registry."""

from __future__ import annotations

import pytest

import bread.strategy.registry as registry_mod
from bread.strategy.base import Strategy
from bread.strategy.registry import get_strategy, list_strategies, register


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate each test from global registry state."""
    monkeypatch.setattr(registry_mod, "_REGISTRY", {})


class _DummyStrategy(Strategy):
    """Minimal concrete strategy for testing."""

    def evaluate(self, universe):  # type: ignore[override]
        return []

    @property
    def name(self) -> str:
        return "dummy"

    @property
    def universe(self) -> list[str]:
        return []

    @property
    def min_history_days(self) -> int:
        return 1

    @property
    def time_stop_days(self) -> int:
        return 1


class TestRegister:
    def test_register_adds_to_registry(self) -> None:
        register("example_strategy")(_DummyStrategy)
        assert "example_strategy" in registry_mod._REGISTRY

    def test_duplicate_raises_value_error(self) -> None:
        register("dup")(_DummyStrategy)
        with pytest.raises(ValueError, match="already registered"):
            register("dup")(_DummyStrategy)


class TestGetStrategy:
    def test_returns_registered_class(self) -> None:
        register("test_strat")(_DummyStrategy)
        assert get_strategy("test_strat") is _DummyStrategy

    def test_unknown_raises_key_error(self) -> None:
        with pytest.raises(KeyError, match="Unknown strategy"):
            get_strategy("nonexistent")


class TestListStrategies:
    def test_returns_registered_names(self) -> None:
        register("alpha")(_DummyStrategy)

        class _Dummy2(_DummyStrategy):
            pass

        register("beta")(_Dummy2)
        result = list_strategies()
        assert "alpha" in result
        assert "beta" in result

    def test_empty_when_none_registered(self) -> None:
        assert list_strategies() == []
