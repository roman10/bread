"""Typed indicator specifications.

Each spec carries the canonical column name produced by
`compute_indicators` for a given indicator/parameter combination, and a
`validate(settings)` method that asserts the active `IndicatorSettings`
will actually compute that column. This is the single source of truth
for column naming — `compute_indicators`, `get_indicator_columns`, and
strategies all derive their column names from these specs.
"""

from __future__ import annotations

from dataclasses import dataclass

from bread.core.config import IndicatorSettings
from bread.core.exceptions import StrategyError


def fmt_stddev(v: float) -> str:
    """Format a Bollinger stddev to match the column-naming convention."""
    return str(int(v)) if v == int(v) else str(v)


class IndicatorSpec:
    """Marker base. Subclasses expose `.column` and `.validate()`."""

    @property
    def column(self) -> str:
        raise NotImplementedError

    def validate(self, settings: IndicatorSettings) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class SMA(IndicatorSpec):
    period: int

    @property
    def column(self) -> str:
        return f"sma_{self.period}"

    def validate(self, settings: IndicatorSettings) -> None:
        if self.period not in settings.sma_periods:
            raise StrategyError(
                f"SMA period {self.period} not in indicator settings "
                f"{settings.sma_periods}"
            )


@dataclass(frozen=True)
class EMA(IndicatorSpec):
    period: int

    @property
    def column(self) -> str:
        return f"ema_{self.period}"

    def validate(self, settings: IndicatorSettings) -> None:
        if self.period not in settings.ema_periods:
            raise StrategyError(
                f"EMA period {self.period} not in indicator settings "
                f"{settings.ema_periods}"
            )


@dataclass(frozen=True)
class RSI(IndicatorSpec):
    period: int

    @property
    def column(self) -> str:
        return f"rsi_{self.period}"

    def validate(self, settings: IndicatorSettings) -> None:
        if self.period != settings.rsi_period:
            raise StrategyError(
                f"RSI period {self.period} != indicator setting "
                f"{settings.rsi_period}"
            )


@dataclass(frozen=True)
class ATR(IndicatorSpec):
    period: int

    @property
    def column(self) -> str:
        return f"atr_{self.period}"

    def validate(self, settings: IndicatorSettings) -> None:
        if self.period != settings.atr_period:
            raise StrategyError(
                f"ATR period {self.period} != indicator setting "
                f"{settings.atr_period}"
            )


@dataclass(frozen=True)
class VolumeSMA(IndicatorSpec):
    period: int

    @property
    def column(self) -> str:
        return f"volume_sma_{self.period}"

    def validate(self, settings: IndicatorSettings) -> None:
        if self.period != settings.volume_sma_period:
            raise StrategyError(
                f"Volume SMA period {self.period} != indicator setting "
                f"{settings.volume_sma_period}"
            )


@dataclass(frozen=True)
class ReturnPct(IndicatorSpec):
    period: int

    @property
    def column(self) -> str:
        return f"return_{self.period}d"

    def validate(self, settings: IndicatorSettings) -> None:
        if self.period not in settings.return_periods:
            raise StrategyError(
                f"Return period {self.period} not in indicator settings "
                f"{settings.return_periods}"
            )


def _validate_bb(period: int, stddev: float, settings: IndicatorSettings) -> None:
    if period != settings.bollinger_period:
        raise StrategyError(
            f"Bollinger period {period} != indicator setting "
            f"{settings.bollinger_period}"
        )
    if stddev != settings.bollinger_stddev:
        raise StrategyError(
            f"Bollinger stddev {stddev} != indicator setting "
            f"{settings.bollinger_stddev}"
        )


@dataclass(frozen=True)
class BBLower(IndicatorSpec):
    period: int
    stddev: float

    @property
    def column(self) -> str:
        return f"bb_lower_{self.period}_{fmt_stddev(self.stddev)}"

    def validate(self, settings: IndicatorSettings) -> None:
        _validate_bb(self.period, self.stddev, settings)


@dataclass(frozen=True)
class BBMid(IndicatorSpec):
    period: int
    stddev: float

    @property
    def column(self) -> str:
        return f"bb_mid_{self.period}_{fmt_stddev(self.stddev)}"

    def validate(self, settings: IndicatorSettings) -> None:
        _validate_bb(self.period, self.stddev, settings)


@dataclass(frozen=True)
class BBUpper(IndicatorSpec):
    period: int
    stddev: float

    @property
    def column(self) -> str:
        return f"bb_upper_{self.period}_{fmt_stddev(self.stddev)}"

    def validate(self, settings: IndicatorSettings) -> None:
        _validate_bb(self.period, self.stddev, settings)


# MACD parts have fixed column names — settings parameterize the math,
# not the name. compute_indicators always emits these three, so
# validate() is a no-op.
class _MacdPart(IndicatorSpec):
    _column_name: str = ""

    @property
    def column(self) -> str:
        return self._column_name

    def validate(self, settings: IndicatorSettings) -> None:  # noqa: ARG002
        return


class MACDLine(_MacdPart):
    _column_name = "macd"


class MACDSignal(_MacdPart):
    _column_name = "macd_signal"


class MACDHist(_MacdPart):
    _column_name = "macd_hist"


def specs_for_settings(settings: IndicatorSettings) -> list[IndicatorSpec]:
    """Return every spec that compute_indicators will produce for `settings`.

    Used by both compute_indicators (to know what columns to populate) and
    get_indicator_columns (to enumerate them).
    """
    specs: list[IndicatorSpec] = []
    specs.extend(SMA(p) for p in settings.sma_periods)
    specs.extend(EMA(p) for p in settings.ema_periods)
    specs.append(RSI(settings.rsi_period))
    specs.extend([MACDLine(), MACDSignal(), MACDHist()])
    specs.append(ATR(settings.atr_period))
    bp, sdv = settings.bollinger_period, settings.bollinger_stddev
    specs.extend([BBLower(bp, sdv), BBMid(bp, sdv), BBUpper(bp, sdv)])
    specs.append(VolumeSMA(settings.volume_sma_period))
    specs.extend(ReturnPct(p) for p in settings.return_periods)
    return specs
