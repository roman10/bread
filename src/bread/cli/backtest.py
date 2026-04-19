"""Backtest and strategy comparison commands."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import typer

from bread.cli._app import app
from bread.cli._helpers import format_pf, passes_promotion_gate
from bread.core.config import load_config
from bread.core.exceptions import BreadError
from bread.core.logging import setup_logging
from bread.data.alpaca_data import AlpacaDataProvider
from bread.db.database import get_engine, get_session_factory, init_db

if TYPE_CHECKING:
    from bread.backtest.multi import MultiBacktestResult


@app.command("backtest")
def backtest_cmd(
    strategy: list[str] = typer.Option(
        ...,
        "--strategy",
        help=(
            "Strategy name. Pass multiple --strategy flags to backtest several "
            "strategies as independent sub-accounts (fair isolation); results "
            "include per-strategy + aggregate."
        ),
    ),
    start: str = typer.Option(..., "--start", help="Start date YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="End date YYYY-MM-DD"),
) -> None:
    """Run a backtest for one or more strategies over a date range."""
    try:
        config = load_config()
        setup_logging(config.app.log_level)

        engine = get_engine(config.db.path)
        try:
            init_db(engine)
            session_factory = get_session_factory(engine)

            from bread.backtest.runner import run_strategy_backtest
            from bread.data.universe import UNIVERSE_CACHE_DIR, UniverseRegistry

            provider = AlpacaDataProvider(config)
            universe_registry = UniverseRegistry(config.universe_providers, UNIVERSE_CACHE_DIR)
            start_date = date.fromisoformat(start)
            end_date = date.fromisoformat(end)

            # Single strategy: keep the original one-shot output.
            if len(strategy) == 1:
                result = run_strategy_backtest(
                    strategy[0],
                    start_date,
                    end_date,
                    config=config,
                    session_factory=session_factory,
                    provider=provider,
                    universe_registry=universe_registry,
                )
                _print_single_result(strategy[0], start, end, result.metrics)
                return

            # Multi-strategy: per-strategy sub-accounts + aggregate portfolio row.
            from bread.backtest.multi import run_multi_strategy_backtest

            multi = run_multi_strategy_backtest(
                strategy,
                start_date,
                end_date,
                config=config,
                session_factory=session_factory,
                provider=provider,
                universe_registry=universe_registry,
            )
            _print_multi_result(strategy, start, end, multi)
        finally:
            engine.dispose()

    except BreadError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc


def _print_single_result(
    strategy: str, start: str, end: str, m: dict[str, float | int]
) -> None:
    typer.echo(f"Backtest: {strategy} | {start} to {end}")
    typer.echo("---")
    typer.echo(f"Total return:    {m['total_return_pct']:>8.2f}%")
    typer.echo(f"CAGR:            {m['cagr_pct']:>8.2f}%")
    typer.echo(f"Sharpe ratio:    {m['sharpe_ratio']:>8.2f}")
    typer.echo(f"Sortino ratio:   {m['sortino_ratio']:>8.2f}")
    typer.echo(f"Max drawdown:    {m['max_drawdown_pct']:>8.2f}%")
    typer.echo(f"Win rate:        {m['win_rate_pct']:>8.2f}%")
    typer.echo(f"Profit factor:   {m['profit_factor']:>8.2f}")
    typer.echo(f"Total trades:    {m['total_trades']:>8d}")
    typer.echo(f"Avg holding days:{m['avg_holding_days']:>8.2f}")


def _print_multi_result(
    strategies: list[str],
    start: str,
    end: str,
    multi: MultiBacktestResult,
) -> None:
    typer.echo(f"Multi-strategy backtest | {start} to {end}")
    typer.echo("=" * 92)
    header = (
        f"{'Strategy':<22} "
        f"{'Return%':>8} "
        f"{'CAGR%':>7} "
        f"{'Sharpe':>7} "
        f"{'MaxDD%':>7} "
        f"{'Win%':>6} "
        f"{'PF':>5} "
        f"{'Trades':>7}"
    )
    typer.echo(header)
    typer.echo("-" * 92)
    for name in strategies:
        if name in multi.failures:
            typer.echo(f"{name:<22} FAILED: {multi.failures[name]}")
            continue
        r = multi.per_strategy[name]
        m = r.metrics
        pf = m.get("profit_factor", 0.0)
        pf_str = "inf" if pf == float("inf") else f"{float(pf):.2f}"
        typer.echo(
            f"{name:<22} "
            f"{float(m['total_return_pct']):>8.2f} "
            f"{float(m['cagr_pct']):>7.2f} "
            f"{float(m['sharpe_ratio']):>7.2f} "
            f"{float(m['max_drawdown_pct']):>7.2f} "
            f"{float(m['win_rate_pct']):>6.2f} "
            f"{pf_str:>5} "
            f"{int(m['total_trades']):>7d}"
        )
    typer.echo("-" * 92)
    agg = multi.aggregate.metrics
    agg_pf = agg.get("profit_factor", 0.0)
    agg_pf_str = "inf" if agg_pf == float("inf") else f"{float(agg_pf):.2f}"
    typer.echo(
        f"{'AGGREGATE':<22} "
        f"{float(agg['total_return_pct']):>8.2f} "
        f"{float(agg['cagr_pct']):>7.2f} "
        f"{float(agg['sharpe_ratio']):>7.2f} "
        f"{float(agg['max_drawdown_pct']):>7.2f} "
        f"{float(agg['win_rate_pct']):>6.2f} "
        f"{agg_pf_str:>5} "
        f"{int(agg['total_trades']):>7d}"
    )
    typer.echo("=" * 92)


@app.command("compare")
def compare_cmd(
    start: str = typer.Option(..., "--start", help="Start date YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="End date YYYY-MM-DD"),
    strategies: str | None = typer.Option(
        None,
        "--strategies",
        help=(
            "Comma-separated strategy names (e.g. 'etf_momentum,bb_mean_reversion'). "
            "Omit or pass 'all' to compare every enabled strategy from config."
        ),
    ),
) -> None:
    """Backtest multiple strategies and print a side-by-side leaderboard.

    Sorted by Sharpe ratio descending. The 'Gate' column flags strategies
    that clear the promotion thresholds documented in
    docs/design/strategy-lifecycle-automation.md (Sharpe>=0.5, PF>=1.3,
    max DD<=12%, win rate>=40%, trades>=30).
    """
    try:
        config = load_config()
        setup_logging(config.app.log_level)

        # Resolve strategy list — default to all enabled strategies in the
        # current mode, matching what the live tick loop would actually run.
        if strategies is None or strategies.strip().lower() == "all":
            names = [s.name for s in config.strategies if s.enabled and config.mode in s.modes]
        else:
            names = [n.strip() for n in strategies.split(",") if n.strip()]
            available = {s.name for s in config.strategies if s.enabled}
            unknown = [n for n in names if n not in available]
            if unknown:
                typer.echo(
                    f"Error: unknown or disabled strategies: {unknown}. "
                    f"Enabled: {sorted(available)}",
                    err=True,
                )
                raise SystemExit(1)

        if not names:
            typer.echo("Error: no strategies to compare.", err=True)
            raise SystemExit(1)

        engine = get_engine(config.db.path)
        try:
            init_db(engine)
            session_factory = get_session_factory(engine)

            from bread.backtest.runner import run_strategy_backtest
            from bread.data.universe import UNIVERSE_CACHE_DIR, UniverseRegistry

            provider = AlpacaDataProvider(config)
            universe_registry = UniverseRegistry(config.universe_providers, UNIVERSE_CACHE_DIR)
            start_date = date.fromisoformat(start)
            end_date = date.fromisoformat(end)

            successes: list[tuple[str, dict[str, float | int]]] = []
            failures: list[tuple[str, str]] = []

            for name in names:
                typer.echo(f"  running {name}...", err=True)
                try:
                    result = run_strategy_backtest(
                        name,
                        start_date,
                        end_date,
                        config=config,
                        session_factory=session_factory,
                        provider=provider,
                        universe_registry=universe_registry,
                    )
                    successes.append((name, result.metrics))
                except BreadError as exc:
                    failures.append((name, str(exc)))
                except Exception as exc:  # noqa: BLE001
                    failures.append((name, f"{type(exc).__name__}: {exc}"))

            successes.sort(
                key=lambda r: (
                    float(r[1].get("sharpe_ratio", 0.0)),
                    float(r[1].get("total_return_pct", 0.0)),
                ),
                reverse=True,
            )

            typer.echo("")
            typer.echo(f"Strategy comparison | {start} to {end}")
            typer.echo("=" * 100)
            header = (
                f"{'Strategy':<22} "
                f"{'Return%':>8} "
                f"{'CAGR%':>7} "
                f"{'Sharpe':>7} "
                f"{'Sortino':>8} "
                f"{'MaxDD%':>7} "
                f"{'Win%':>6} "
                f"{'PF':>5} "
                f"{'Trades':>7} "
                f"{'AvgHold':>8} "
                f"{'Gate':>5}"
            )
            typer.echo(header)
            typer.echo("-" * 100)

            for name, m in successes:
                gate = "PASS" if passes_promotion_gate(m) else "FAIL"
                row = (
                    f"{name:<22} "
                    f"{float(m['total_return_pct']):>8.2f} "
                    f"{float(m['cagr_pct']):>7.2f} "
                    f"{float(m['sharpe_ratio']):>7.2f} "
                    f"{float(m['sortino_ratio']):>8.2f} "
                    f"{float(m['max_drawdown_pct']):>7.2f} "
                    f"{float(m['win_rate_pct']):>6.2f} "
                    f"{format_pf(float(m['profit_factor']))} "
                    f"{int(m['total_trades']):>7d} "
                    f"{float(m['avg_holding_days']):>8.2f} "
                    f"{gate:>5}"
                )
                typer.echo(row)

            typer.echo("=" * 100)
            passing = sum(1 for _, m in successes if passes_promotion_gate(m))
            typer.echo(f"{len(successes)} strategies compared, {passing} pass the promotion gate.")

            if failures:
                typer.echo("")
                typer.echo("Failures:")
                for name, err in failures:
                    typer.echo(f"  {name}: {err}", err=True)

            if not successes:
                raise SystemExit(1)
        finally:
            engine.dispose()

    except BreadError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc
