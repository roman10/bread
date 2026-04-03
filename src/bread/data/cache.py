"""OHLCV bar cache backed by SQLite."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import holidays
import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from bread.core.config import AppConfig
from bread.core.exceptions import CacheError
from bread.data.provider import DataProvider
from bread.db.models import MarketDataCache

logger = logging.getLogger(__name__)

_nyse_holidays = holidays.NYSE()  # type: ignore[attr-defined]
_et = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# NYSE trading-day helpers
# ---------------------------------------------------------------------------


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in _nyse_holidays


def last_completed_trading_day(as_of_utc: datetime) -> date:
    local_dt = as_of_utc.astimezone(_et)
    candidate = local_dt.date()

    if not (is_trading_day(candidate) and local_dt.time() >= time(16, 0)):
        candidate -= timedelta(days=1)

    while not is_trading_day(candidate):
        candidate -= timedelta(days=1)

    return candidate


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class BarCache:
    def __init__(
        self,
        session: Session,
        provider: DataProvider,
        config: AppConfig,
    ) -> None:
        self._session = session
        self._provider = provider
        self._config = config

    def get_bars(
        self,
        symbol: str,
        timeframe: str | None = None,
        as_of_utc: datetime | None = None,
    ) -> pd.DataFrame:
        """Return cached bars, fetching/refreshing as needed."""
        symbol = symbol.upper()
        timeframe = timeframe or self._config.data.default_timeframe
        as_of_utc = as_of_utc or datetime.now(UTC)

        target_day = last_completed_trading_day(as_of_utc)

        if self._is_stale(symbol, timeframe, target_day):
            self._refresh(symbol, timeframe, target_day)

        return self._load_from_db(symbol, timeframe)

    def _is_stale(self, symbol: str, timeframe: str, target_day: date) -> bool:
        """Check whether cached data covers the target trading day."""
        stmt = (
            select(MarketDataCache.timestamp_utc)
            .where(
                MarketDataCache.symbol == symbol,
                MarketDataCache.timeframe == timeframe,
            )
            .order_by(MarketDataCache.timestamp_utc.desc())
            .limit(1)
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        if row is None:
            logger.debug("Cache miss for %s/%s", symbol, timeframe)
            return True

        # SQLAlchemy returns datetime for DateTime columns; SQLite raw may return str
        if isinstance(row, datetime):
            latest = row.date()
        else:
            latest = datetime.fromisoformat(str(row)).date()

        is_stale = latest < target_day
        if is_stale:
            logger.debug(
                "Cache stale for %s/%s: latest=%s target=%s",
                symbol, timeframe, latest, target_day,
            )
        else:
            logger.debug("Cache hit for %s/%s", symbol, timeframe)
        return is_stale

    def _refresh(self, symbol: str, timeframe: str, target_day: date) -> None:
        """Fetch full lookback from provider and upsert into cache."""
        longest_indicator = self._config.indicators.longest_window
        # Need lookback_days usable bars + warmup for longest indicator.
        # ~1.45 calendar days per trading day, with margin.
        trading_days_needed = self._config.data.lookback_days + longest_indicator
        start = target_day - timedelta(days=int(trading_days_needed * 1.5))
        end = target_day

        logger.info("Refreshing cache for %s/%s: %s to %s", symbol, timeframe, start, end)
        try:
            df = self._provider.get_bars(symbol, start, end, timeframe)
        except Exception as exc:
            raise CacheError(f"Failed to refresh cache for {symbol}: {exc}") from exc

        self._upsert(df, symbol, timeframe)

    def _upsert(self, df: pd.DataFrame, symbol: str, timeframe: str) -> None:
        """Upsert bars into market_data_cache using ON CONFLICT DO UPDATE."""
        now_utc = datetime.now(UTC)
        rows = []
        for ts, row in df.iterrows():
            rows.append(
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "timestamp_utc": ts,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": int(row["volume"]),
                    "fetched_at_utc": now_utc,
                }
            )

        if not rows:
            return

        stmt = sqlite_insert(MarketDataCache).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "timeframe", "timestamp_utc"],
            set_={
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
                "fetched_at_utc": stmt.excluded.fetched_at_utc,
            },
        )
        self._session.execute(stmt)
        self._session.commit()
        logger.info("Upserted %d bars for %s/%s", len(rows), symbol, timeframe)

    def _load_from_db(self, symbol: str, timeframe: str) -> pd.DataFrame:
        """Load bars from SQLite and return as provider-contract DataFrame."""
        stmt = (
            select(MarketDataCache)
            .where(
                MarketDataCache.symbol == symbol,
                MarketDataCache.timeframe == timeframe,
            )
            .order_by(MarketDataCache.timestamp_utc.asc())
        )
        results = self._session.execute(stmt).scalars().all()

        if not results:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        records = []
        for r in results:
            records.append(
                {
                    "timestamp": r.timestamp_utc,
                    "open": r.open,
                    "high": r.high,
                    "low": r.low,
                    "close": r.close,
                    "volume": r.volume,
                }
            )

        df = pd.DataFrame.from_records(records)
        ts = pd.to_datetime(df["timestamp"])
        if ts.dt.tz is None:
            ts = ts.dt.tz_localize("UTC")
        df["timestamp"] = ts
        df = df.set_index("timestamp")
        df = df.sort_index()
        return df
