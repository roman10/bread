"""Claude AI analyst strategy — LLM-powered technical analysis."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from bread.core.config import IndicatorSettings
from bread.core.exceptions import ClaudeError
from bread.core.models import Signal, SignalDirection
from bread.strategy.base import Strategy, load_strategy_config
from bread.strategy.registry import register

if TYPE_CHECKING:
    import pandas as pd

    from bread.ai.client import ClaudeClient

logger = logging.getLogger(__name__)


def _fmt_stddev(v: float) -> str:
    """Format Bollinger Band stddev to match indicators.py column naming."""
    return str(int(v)) if v == int(v) else str(v)


@register("claude_analyst")
class ClaudeAnalyst(Strategy):
    """Strategy that uses Claude AI to analyze technical indicators.

    Compresses enriched DataFrames into compact text summaries, sends a
    single batched CLI call, and converts structured recommendations into
    Signal objects.  On any Claude failure, returns no signals (fail-safe).
    """

    accepts_claude_client: ClassVar[bool] = True

    def __init__(
        self,
        config_path: Path,
        indicator_settings: IndicatorSettings,
        *,
        universe: list[str] | None = None,
        claude_client: ClaudeClient | None = None,
    ) -> None:
        cfg = load_strategy_config(config_path)

        self._universe: list[str] = universe if universe is not None else cfg.get("universe", [])
        analysis = cfg.get("analysis", {})
        self._atr_stop_mult: float = analysis.get("atr_stop_mult", 1.5)
        self._time_stop: int = analysis.get("time_stop_days", 15)

        self._claude = claude_client
        self._settings = indicator_settings

        # Derive column names from indicator settings
        self._col_atr = f"atr_{indicator_settings.atr_period}"
        self._col_rsi = f"rsi_{indicator_settings.rsi_period}"
        self._col_vol_sma = f"volume_sma_{indicator_settings.volume_sma_period}"

        self._sma_cols = [f"sma_{p}" for p in indicator_settings.sma_periods]
        self._ema_cols = [f"ema_{p}" for p in indicator_settings.ema_periods]

        sdv = _fmt_stddev(indicator_settings.bollinger_stddev)
        bp = indicator_settings.bollinger_period
        self._col_bb_lower = f"bb_lower_{bp}_{sdv}"
        self._col_bb_mid = f"bb_mid_{bp}_{sdv}"
        self._col_bb_upper = f"bb_upper_{bp}_{sdv}"

        self._return_cols = [f"return_{p}d" for p in indicator_settings.return_periods]

        # Required columns — core set for summary generation
        self._required_cols: set[str] = {
            "close",
            "volume",
            self._col_atr,
            self._col_rsi,
            self._col_vol_sma,
            *self._sma_cols,
        }

    @property
    def name(self) -> str:
        return "claude_analyst"

    @property
    def universe(self) -> list[str]:
        return list(self._universe)

    @property
    def min_history_days(self) -> int:
        return max(self._settings.sma_periods)

    @property
    def time_stop_days(self) -> int:
        return self._time_stop

    def evaluate(self, universe: dict[str, pd.DataFrame]) -> list[Signal]:
        """Evaluate all symbols via a single Claude CLI call."""
        if self._claude is None:
            return []

        # Build summaries for symbols that have data
        summaries: list[str] = []
        for symbol in self._universe:
            if symbol not in universe:
                continue
            df = universe[symbol]
            if df.empty:
                continue
            missing = self._required_cols - set(df.columns)
            if missing:
                logger.warning("Missing columns for %s: %s — skipping", symbol, missing)
                continue
            summaries.append(self._summarize_symbol(symbol, df))

        if not summaries:
            return []

        prompt = self._build_prompt(summaries, len(summaries))

        try:
            analysis = self._claude.analyze_technicals(prompt)
        except ClaudeError:
            logger.warning("Claude analysis failed — emitting no signals")
            return []

        # Convert recommendations to Signal objects
        now = datetime.now(UTC)
        signals: list[Signal] = []
        for rec in analysis.recommendations:
            if rec.action == "HOLD":
                continue
            if rec.symbol not in universe:
                continue
            df = universe[rec.symbol]
            close = float(df.iloc[-1]["close"])
            atr = float(df.iloc[-1][self._col_atr])
            stop_loss_pct = self._atr_stop_mult * atr / close
            if stop_loss_pct <= 0:
                logger.warning("Non-positive stop_loss_pct for %s — skipping", rec.symbol)
                continue

            try:
                direction = SignalDirection(rec.action)
            except ValueError:
                continue

            strength = rec.strength if direction == SignalDirection.BUY else 1.0
            signals.append(
                Signal(
                    symbol=rec.symbol,
                    direction=direction,
                    strength=strength,
                    stop_loss_pct=stop_loss_pct,
                    strategy_name=self.name,
                    reason=rec.reasoning,
                    timestamp=now,
                )
            )

        return signals

    def _summarize_symbol(self, symbol: str, df: pd.DataFrame) -> str:
        """Compress a DataFrame into a compact technical summary."""
        last = df.iloc[-1]
        close = float(last["close"])
        volume = float(last["volume"])
        rsi = float(last[self._col_rsi])
        atr = float(last[self._col_atr])
        vol_sma = float(last[self._col_vol_sma])

        # Price returns
        ret_parts: list[str] = []
        for col in self._return_cols:
            if col in df.columns:
                val = float(last[col])
                label = col.replace("return_", "")
                ret_parts.append(f"{label} {val:+.1%}")
        ret_str = " | ".join(ret_parts) if ret_parts else "n/a"

        # SMA values and trend
        sma_parts: list[str] = []
        above_all = True
        for col in self._sma_cols:
            val = float(last[col])
            period = col.split("_")[1]
            sma_parts.append(f"{period}={val:.2f}")
            if close <= val:
                above_all = False
        sma_str = ", ".join(sma_parts)
        trend_note = "above all SMAs" if above_all else "mixed"

        # EMA values
        ema_parts: list[str] = []
        for col in self._ema_cols:
            if col in df.columns:
                val = float(last[col])
                period = col.split("_")[1]
                ema_parts.append(f"{period}={val:.2f}")
        ema_str = ", ".join(ema_parts) if ema_parts else "n/a"

        # MACD
        macd_str = "n/a"
        if all(c in df.columns for c in ("macd", "macd_signal", "macd_hist")):
            m = float(last["macd"])
            ms = float(last["macd_signal"])
            mh = float(last["macd_hist"])
            macd_str = f"{m:.2f}, Signal: {ms:.2f}, Hist: {mh:+.2f}"

        # Bollinger Bands
        bb_str = "n/a"
        if all(c in df.columns for c in (self._col_bb_lower, self._col_bb_mid, self._col_bb_upper)):
            bl = float(last[self._col_bb_lower])
            bm = float(last[self._col_bb_mid])
            bu = float(last[self._col_bb_upper])
            bb_str = f"Lower={bl:.2f}, Mid={bm:.2f}, Upper={bu:.2f}"

        # Volume
        vol_ratio = volume / vol_sma if vol_sma > 0 else 0.0

        return (
            f"{symbol} — ${close:.2f}\n"
            f"  Price: {ret_str}\n"
            f"  RSI({self._settings.rsi_period}): {rsi:.1f} | "
            f"ATR({self._settings.atr_period}): ${atr:.2f} ({atr / close:.1%})\n"
            f"  SMA: {sma_str} ({trend_note})\n"
            f"  EMA: {ema_str}\n"
            f"  MACD: {macd_str}\n"
            f"  BB({self._settings.bollinger_period},"
            f"{_fmt_stddev(self._settings.bollinger_stddev)}): {bb_str}\n"
            f"  Volume: {volume / 1e6:.1f}M vs {vol_sma / 1e6:.1f}M avg ({vol_ratio:.2f}x)"
        )

    @staticmethod
    def _build_prompt(summaries: list[str], count: int) -> str:
        """Wrap technical summaries into a complete analysis prompt."""
        header = (
            f"Date: {date.today().isoformat()}\n"
            f"Analyze these {count} symbols for swing trading setups "
            f"(2-15 day hold):\n"
        )
        instructions = (
            "\nFor each symbol, recommend BUY, SELL, or HOLD.\n"
            "BUY: multiple technical factors align for entry. "
            "Rate strength 0.0-1.0.\n"
            "SELL: technical deterioration suggesting exit. Use strength 1.0.\n"
            "HOLD: no clear actionable setup.\n"
            "Only recommend BUY when conviction is high."
        )
        body = "\n".join(summaries)
        return f"{header}\n{body}\n{instructions}"
