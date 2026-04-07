"""OHLCV bar cache backed by SQLite."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import holidays
import pandas as pd
from sqlalchemy import func as sa_func
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


def is_market_open(now_et: datetime | None = None) -> bool:
    """Return True if NYSE is currently open (Mon-Fri 9:30-16:00 ET, excl. holidays)."""
    if now_et is None:
        now_et = datetime.now(_et)
    if now_et.weekday() >= 5 or now_et.date() in _nyse_holidays:
        return False
    return time(9, 30) <= now_et.time() < time(16, 0)


def last_completed_trading_day(as_of_utc: datetime) -> date:
    local_dt = as_of_utc.astimezone(_et)
    candidate = local_dt.date()

    if not (is_trading_day(candidate) and local_dt.time() >= time(16, 0)):
        candidate -= timedelta(days=1)

    while not is_trading_day(candidate):
        candidate -= timedelta(days=1)

    return candidate


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _upsert_bars(
    session: Session, df: pd.DataFrame, symbol: str, timeframe: str,
) -> None:
    """Upsert OHLCV bars into market_data_cache using ON CONFLICT DO UPDATE."""
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
    session.execute(stmt)
    session.commit()
    logger.info("Upserted %d bars for %s/%s", len(rows), symbol, timeframe)


def _rows_to_dataframe(results: Sequence[MarketDataCache]) -> pd.DataFrame:
    """Convert MarketDataCache rows to a provider-contract DataFrame."""
    if not results:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    records = [
        {
            "timestamp": r.timestamp_utc,
            "open": r.open,
            "high": r.high,
            "low": r.low,
            "close": r.close,
            "volume": r.volume,
        }
        for r in results
    ]

    df = pd.DataFrame.from_records(records)
    ts = pd.to_datetime(df["timestamp"])
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize("UTC")
    df["timestamp"] = ts
    return df.set_index("timestamp").sort_index()


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

    def get_bars_batch(
        self,
        symbols: list[str],
        timeframe: str | None = None,
        as_of_utc: datetime | None = None,
    ) -> dict[str, pd.DataFrame]:
        """Return cached bars for multiple symbols, batch-fetching stale ones."""
        symbols = [s.upper() for s in symbols]
        timeframe = timeframe or self._config.data.default_timeframe
        as_of_utc = as_of_utc or datetime.now(UTC)

        target_day = last_completed_trading_day(as_of_utc)

        stale = [s for s in symbols if self._is_stale(s, timeframe, target_day)]

        if stale:
            self._refresh_batch(stale, timeframe, target_day)

        result: dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            df = self._load_from_db(symbol, timeframe)
            if not df.empty:
                result[symbol] = df
            else:
                logger.warning("No cached data for %s after batch refresh", symbol)

        return result

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
        start, end = self._lookback_range(target_day)

        logger.info("Refreshing cache for %s/%s: %s to %s", symbol, timeframe, start, end)
        try:
            df = self._provider.get_bars(symbol, start, end, timeframe)
        except Exception as exc:
            raise CacheError(f"Failed to refresh cache for {symbol}: {exc}") from exc

        self._upsert(df, symbol, timeframe)

    def _refresh_batch(
        self, symbols: list[str], timeframe: str, target_day: date
    ) -> None:
        """Batch-fetch full lookback for multiple symbols and upsert."""
        start, end = self._lookback_range(target_day)

        logger.info(
            "Batch refreshing cache for %d symbols/%s: %s to %s",
            len(symbols), timeframe, start, end,
        )
        try:
            batch = self._provider.get_bars_batch(symbols, start, end, timeframe)
        except Exception as exc:
            raise CacheError(
                f"Failed to batch refresh cache for {symbols}: {exc}"
            ) from exc

        for symbol, df in batch.items():
            self._upsert(df, symbol, timeframe)

        missing = set(symbols) - set(batch.keys())
        if missing:
            logger.warning("Batch refresh returned no data for: %s", missing)

    def _lookback_range(self, target_day: date) -> tuple[date, date]:
        """Compute the (start, end) date range for a cache refresh."""
        longest_indicator = self._config.indicators.longest_window
        # Need lookback_days usable bars + warmup for longest indicator.
        # ~1.45 calendar days per trading day, with margin.
        trading_days_needed = self._config.data.lookback_days + longest_indicator
        start = target_day - timedelta(days=int(trading_days_needed * 1.5))
        return start, target_day

    def _upsert(self, df: pd.DataFrame, symbol: str, timeframe: str) -> None:
        _upsert_bars(self._session, df, symbol, timeframe)

    def _load_from_db(self, symbol: str, timeframe: str) -> pd.DataFrame:
        """Load all bars from SQLite for a symbol/timeframe."""
        stmt = (
            select(MarketDataCache)
            .where(
                MarketDataCache.symbol == symbol,
                MarketDataCache.timeframe == timeframe,
            )
            .order_by(MarketDataCache.timestamp_utc.asc())
        )
        results = self._session.execute(stmt).scalars().all()
        return _rows_to_dataframe(results)


# ---------------------------------------------------------------------------
# Caching DataProvider for backtests
# ---------------------------------------------------------------------------


class CachingDataProvider(DataProvider):
    """DataProvider wrapper that caches fetched bars in SQLite.

    Unlike BarCache (designed for live trading with "get latest" semantics),
    this implements the DataProvider interface with explicit date ranges,
    making repeated backtests hit the local DB instead of the Alpaca API.
    """

    def __init__(self, provider: DataProvider, session: Session) -> None:
        self._provider = provider
        self._session = session

    def get_bars(
        self,
        symbol: str,
        start: date,
        end: date,
        timeframe: str,
    ) -> pd.DataFrame:
        symbol = symbol.upper()
        if self._has_range(symbol, timeframe, start, end):
            logger.debug("Cache hit for %s/%s [%s, %s]", symbol, timeframe, start, end)
            return self._load_range(symbol, timeframe, start, end)

        logger.info("Cache miss for %s/%s, fetching from API", symbol, timeframe)
        df = self._provider.get_bars(symbol, start, end, timeframe)
        self._upsert(df, symbol, timeframe)
        return df

    def get_bars_batch(
        self,
        symbols: list[str],
        start: date,
        end: date,
        timeframe: str,
    ) -> dict[str, pd.DataFrame]:
        symbols = [s.upper() for s in symbols]
        result: dict[str, pd.DataFrame] = {}
        to_fetch: list[str] = []

        for s in symbols:
            if self._has_range(s, timeframe, start, end):
                df = self._load_range(s, timeframe, start, end)
                if not df.empty:
                    result[s] = df
                    continue
            to_fetch.append(s)

        if to_fetch:
            logger.info(
                "Cache: %d hit, %d to fetch from API", len(result), len(to_fetch),
            )
            fetched = self._provider.get_bars_batch(to_fetch, start, end, timeframe)
            for s, df in fetched.items():
                self._upsert(df, s, timeframe)
                result[s] = df

        return result

    def _has_range(self, symbol: str, timeframe: str, start: date, end: date) -> bool:
        """Check if cached bars cover the requested [start, end] range."""
        stmt = select(
            sa_func.min(MarketDataCache.timestamp_utc),
            sa_func.max(MarketDataCache.timestamp_utc),
        ).where(
            MarketDataCache.symbol == symbol,
            MarketDataCache.timeframe == timeframe,
        )
        row = self._session.execute(stmt).one()
        if row[0] is None:
            return False

        def _to_date(val: datetime | str) -> date:
            if isinstance(val, datetime):
                return val.date()
            return datetime.fromisoformat(str(val)).date()

        min_cached = _to_date(row[0])
        max_cached = _to_date(row[1])

        # Adjust for non-trading days (weekends/holidays) at range boundaries
        first_needed = start
        while not is_trading_day(first_needed) and first_needed <= end:
            first_needed += timedelta(days=1)

        last_needed = end
        while not is_trading_day(last_needed) and last_needed >= start:
            last_needed -= timedelta(days=1)

        return min_cached <= first_needed and max_cached >= last_needed

    def _load_range(
        self, symbol: str, timeframe: str, start: date, end: date,
    ) -> pd.DataFrame:
        """Load cached bars filtered to [start, end]."""
        start_dt = datetime(start.year, start.month, start.day, tzinfo=UTC)
        end_dt = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=UTC)

        stmt = (
            select(MarketDataCache)
            .where(
                MarketDataCache.symbol == symbol,
                MarketDataCache.timeframe == timeframe,
                MarketDataCache.timestamp_utc >= start_dt,
                MarketDataCache.timestamp_utc <= end_dt,
            )
            .order_by(MarketDataCache.timestamp_utc.asc())
        )
        results = self._session.execute(stmt).scalars().all()
        return _rows_to_dataframe(results)

    def _upsert(self, df: pd.DataFrame, symbol: str, timeframe: str) -> None:
        _upsert_bars(self._session, df, symbol, timeframe)
