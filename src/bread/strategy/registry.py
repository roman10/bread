"""Strategy registry — register and look up strategy classes by name."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bread.strategy.base import Strategy

_REGISTRY: dict[str, type[Strategy]] = {}


def register(name: str):  # type: ignore[no-untyped-def]
    """Class decorator factory to register a strategy under its canonical identifier."""

    def decorator(cls: type[Strategy]) -> type[Strategy]:
        if name in _REGISTRY:
            raise ValueError(f"Strategy already registered: {name}")
        _REGISTRY[name] = cls
        return cls

    return decorator


def get_strategy(name: str) -> type[Strategy]:
    """Look up a registered strategy class by name. Raises KeyError if not found."""
    if name not in _REGISTRY:
        raise KeyError(f"Unknown strategy: {name}. Available: {list(_REGISTRY.keys())}")
    return _REGISTRY[name]


def list_strategies() -> list[str]:
    """Return names of all registered strategies."""
    return list(_REGISTRY.keys())
