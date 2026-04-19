"""Helpers shared across CLI command modules."""

from __future__ import annotations

import logging
import math
import threading
from typing import TYPE_CHECKING

import typer

from bread.core.config import load_config

if TYPE_CHECKING:
    from bread.core.config import AppConfig

logger = logging.getLogger(__name__)


# Promotion thresholds from docs/design/strategy-lifecycle-automation.md:144-156.
# A strategy "passes the gate" when all five hold; gives an instant
# "which of these clear the bar" answer alongside the comparison table.
GATE_MIN_SHARPE = 0.5
GATE_MIN_PROFIT_FACTOR = 1.3
GATE_MAX_DRAWDOWN_PCT = 12.0
GATE_MIN_WIN_RATE_PCT = 40.0
GATE_MIN_TRADES = 30


def passes_promotion_gate(metrics: dict[str, float | int]) -> bool:
    return (
        float(metrics.get("sharpe_ratio", 0.0)) >= GATE_MIN_SHARPE
        and float(metrics.get("profit_factor", 0.0)) >= GATE_MIN_PROFIT_FACTOR
        and float(metrics.get("max_drawdown_pct", 0.0)) <= GATE_MAX_DRAWDOWN_PCT
        and float(metrics.get("win_rate_pct", 0.0)) >= GATE_MIN_WIN_RATE_PCT
        and int(metrics.get("total_trades", 0)) >= GATE_MIN_TRADES
    )


def format_pf(value: float) -> str:
    """Render profit factor with a sensible cap so the column stays narrow."""
    if math.isinf(value):
        return "  inf"
    return f"{value:>5.2f}"


def load_strategy_universes(config: AppConfig) -> dict[str, list[str]]:
    """Return {strategy_name: [symbols]} by reading each strategy's YAML.

    Per-strategy `universe` lives in the strategy's own YAML, not on
    StrategySettings. We read them directly rather than instantiating the
    Strategy classes (which would pull indicator deps).
    """
    import yaml

    from bread.core.config import CONFIG_DIR

    out: dict[str, list[str]] = {}
    for s in config.strategies:
        rel = s.config_path or f"strategies/{s.name}.yaml"
        path = CONFIG_DIR / rel
        try:
            with open(path) as f:
                cfg = yaml.safe_load(f) or {}
        except FileNotFoundError:
            logger.warning("Strategy config missing: %s", path)
            continue
        universe = cfg.get("universe")
        if isinstance(universe, list):
            out[s.name] = [str(sym).upper() for sym in universe]
    return out


def infer_strategy_from_symbol(
    universes: dict[str, list[str]],
    symbol: str,
) -> str:
    """Return the single strategy owning `symbol`, else 'legacy'.

    Every current strategy shares the same universe, so this falls through
    to 'legacy' in practice — but the logic costs nothing and gives a
    correct attribution if a future strategy owns a unique symbol.
    """
    sym = symbol.upper()
    owners = [name for name, uni in universes.items() if sym in uni]
    return owners[0] if len(owners) == 1 else "legacy"


def start_dashboard_thread(port: int) -> None:
    """Start the dashboard in a background daemon thread if deps are available."""
    try:
        from bread.dashboard.app import create_app
    except ImportError:
        return  # dash deps not installed — silently skip

    config = load_config()
    dash_app = create_app(config)

    def _serve() -> None:
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
        try:
            dash_app.run(host="0.0.0.0", port=port, debug=False)
        except OSError:
            logger.warning("Dashboard port %d in use — skipping auto-start", port)

    thread = threading.Thread(target=_serve, daemon=True, name="dashboard")
    thread.start()
    # Server binds 0.0.0.0 so Tailscale peers can reach it; the user
    # clicks localhost (or the VM's Tailscale IP remotely).
    typer.echo(f"Dashboard: http://localhost:{port}")
