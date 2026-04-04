"""Tests for monitoring.journal."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bread.db.database import init_db
from bread.db.models import OrderLog
from bread.monitoring.journal import get_journal, get_journal_summary


def _make_sf():
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    return sessionmaker(bind=engine)


def _fill(
    sf,
    symbol: str,
    side: str,
    qty: int,
    price: float,
    filled_at: datetime,
    strategy: str = "etf_momentum",
    reason: str = "test",
):
    with sf() as session:
        session.add(OrderLog(
            broker_order_id=f"{side}-{symbol}-{filled_at.timestamp()}",
            symbol=symbol,
            side=side,
            qty=qty,
            status="FILLED",
            filled_price=price,
            strategy_name=strategy,
            reason=reason,
            created_at_utc=filled_at,
            filled_at_utc=filled_at,
        ))
        session.commit()


class TestGetJournal:
    def test_empty_db_returns_empty(self) -> None:
        sf = _make_sf()
        with sf() as session:
            assert get_journal(session) == []

    def test_pairs_buy_sell_round_trip(self) -> None:
        sf = _make_sf()
        t1 = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
        t2 = datetime(2026, 3, 5, 14, 0, tzinfo=UTC)
        _fill(sf, "SPY", "BUY", 10, 500.0, t1)
        _fill(sf, "SPY", "SELL", 10, 510.0, t2)

        with sf() as session:
            entries = get_journal(session)

        assert len(entries) == 1
        e = entries[0]
        assert e.symbol == "SPY"
        assert e.entry_price == 500.0
        assert e.exit_price == 510.0
        assert e.qty == 10
        assert e.pnl == 100.0  # (510 - 500) * 10
        assert abs(e.pnl_pct - 2.0) < 0.01  # 2%
        assert e.hold_days == 4

    def test_unpaired_buy_excluded(self) -> None:
        sf = _make_sf()
        t1 = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
        _fill(sf, "SPY", "BUY", 10, 500.0, t1)
        # No sell — still open

        with sf() as session:
            entries = get_journal(session)

        assert len(entries) == 0

    def test_orphan_sell_skipped(self) -> None:
        sf = _make_sf()
        t1 = datetime(2026, 3, 5, 14, 0, tzinfo=UTC)
        _fill(sf, "SPY", "SELL", 10, 510.0, t1)
        # No preceding buy

        with sf() as session:
            entries = get_journal(session)

        assert len(entries) == 0

    def test_filter_by_strategy(self) -> None:
        sf = _make_sf()
        t1 = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
        t2 = datetime(2026, 3, 5, 14, 0, tzinfo=UTC)
        _fill(sf, "SPY", "BUY", 10, 500.0, t1, strategy="strat_a")
        _fill(sf, "SPY", "SELL", 10, 510.0, t2, strategy="strat_a")
        _fill(sf, "QQQ", "BUY", 5, 400.0, t1, strategy="strat_b")
        _fill(sf, "QQQ", "SELL", 5, 410.0, t2, strategy="strat_b")

        with sf() as session:
            entries = get_journal(session, strategy="strat_a")

        assert len(entries) == 1
        assert entries[0].symbol == "SPY"

    def test_filter_by_symbol(self) -> None:
        sf = _make_sf()
        t1 = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
        t2 = datetime(2026, 3, 5, 14, 0, tzinfo=UTC)
        _fill(sf, "SPY", "BUY", 10, 500.0, t1)
        _fill(sf, "SPY", "SELL", 10, 510.0, t2)
        _fill(sf, "QQQ", "BUY", 5, 400.0, t1)
        _fill(sf, "QQQ", "SELL", 5, 410.0, t2)

        with sf() as session:
            entries = get_journal(session, symbol="QQQ")

        assert len(entries) == 1
        assert entries[0].symbol == "QQQ"

    def test_filter_by_date_range(self) -> None:
        sf = _make_sf()
        t1 = datetime(2026, 2, 1, 10, 0, tzinfo=UTC)
        t2 = datetime(2026, 2, 5, 14, 0, tzinfo=UTC)
        t3 = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
        t4 = datetime(2026, 3, 5, 14, 0, tzinfo=UTC)
        _fill(sf, "SPY", "BUY", 10, 500.0, t1)
        _fill(sf, "SPY", "SELL", 10, 510.0, t2)
        _fill(sf, "QQQ", "BUY", 5, 400.0, t3)
        _fill(sf, "QQQ", "SELL", 5, 410.0, t4)

        with sf() as session:
            entries = get_journal(session, start=date(2026, 3, 1))

        assert len(entries) == 1
        assert entries[0].symbol == "QQQ"

    def test_multiple_round_trips_same_symbol(self) -> None:
        sf = _make_sf()
        t1 = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
        t2 = datetime(2026, 3, 5, 14, 0, tzinfo=UTC)
        t3 = datetime(2026, 3, 10, 10, 0, tzinfo=UTC)
        t4 = datetime(2026, 3, 15, 14, 0, tzinfo=UTC)
        _fill(sf, "SPY", "BUY", 10, 500.0, t1)
        _fill(sf, "SPY", "SELL", 10, 520.0, t2)
        _fill(sf, "SPY", "BUY", 5, 510.0, t3)
        _fill(sf, "SPY", "SELL", 5, 505.0, t4)

        with sf() as session:
            entries = get_journal(session)

        assert len(entries) == 2
        # Sorted by exit_date desc
        assert entries[0].pnl == -25.0  # (505-510)*5
        assert entries[1].pnl == 200.0  # (520-500)*10

    def test_missing_fill_price_skipped(self) -> None:
        """Orders with None filled_price should not produce journal entries."""
        sf = _make_sf()
        t1 = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
        t2 = datetime(2026, 3, 5, 14, 0, tzinfo=UTC)
        # Insert buy with filled_price, sell with None filled_price
        with sf() as session:
            session.add(OrderLog(
                broker_order_id="buy-1",
                symbol="SPY",
                side="BUY",
                qty=10,
                status="FILLED",
                filled_price=500.0,
                strategy_name="etf_momentum",
                reason="test",
                created_at_utc=t1,
                filled_at_utc=t1,
            ))
            session.add(OrderLog(
                broker_order_id="sell-1",
                symbol="SPY",
                side="SELL",
                qty=10,
                status="FILLED",
                filled_price=None,
                strategy_name="etf_momentum",
                reason="test",
                created_at_utc=t2,
                filled_at_utc=t2,
            ))
            session.commit()

        with sf() as session:
            entries = get_journal(session)

        assert len(entries) == 0

    def test_same_day_round_trip(self) -> None:
        sf = _make_sf()
        t1 = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
        t2 = datetime(2026, 3, 1, 15, 0, tzinfo=UTC)
        _fill(sf, "SPY", "BUY", 10, 500.0, t1)
        _fill(sf, "SPY", "SELL", 10, 505.0, t2)

        with sf() as session:
            entries = get_journal(session)

        assert len(entries) == 1
        assert entries[0].hold_days == 0
        assert entries[0].pnl == 50.0


class TestGetJournalSummary:
    def test_empty_entries(self) -> None:
        result = get_journal_summary([])
        assert result["total_trades"] == 0
        assert result["win_rate_pct"] == 0.0

    def test_computes_stats(self) -> None:
        sf = _make_sf()
        t1 = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
        t2 = datetime(2026, 3, 5, 14, 0, tzinfo=UTC)
        t3 = datetime(2026, 3, 10, 10, 0, tzinfo=UTC)
        t4 = datetime(2026, 3, 15, 14, 0, tzinfo=UTC)
        _fill(sf, "SPY", "BUY", 10, 500.0, t1)
        _fill(sf, "SPY", "SELL", 10, 520.0, t2)  # win: +200
        _fill(sf, "QQQ", "BUY", 5, 400.0, t3)
        _fill(sf, "QQQ", "SELL", 5, 390.0, t4)   # loss: -50

        with sf() as session:
            entries = get_journal(session)

        summary = get_journal_summary(entries)
        assert summary["total_trades"] == 2
        assert summary["win_rate_pct"] == 50.0
        assert summary["avg_win"] == 200.0
        assert summary["avg_loss"] == -50.0
        assert summary["total_pnl"] == 150.0
        assert summary["best_trade"] == 200.0
        assert summary["worst_trade"] == -50.0
        assert summary["expectancy"] == 75.0  # 150/2


class TestJournalWithCostAdjustedPrices:
    """Verify journal P&L uses filled_price (cost-adjusted), not raw_filled_price."""

    def test_journal_pnl_uses_adjusted_prices(self) -> None:
        sf = _make_sf()
        t1 = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
        t2 = datetime(2026, 3, 5, 14, 0, tzinfo=UTC)

        # Simulate paper cost adjustment: BUY adjusted up, SELL adjusted down
        raw_buy, adj_buy = 500.0, 500.5    # paid more
        raw_sell, adj_sell = 510.0, 509.49  # received less

        with sf() as session:
            session.add(OrderLog(
                broker_order_id="buy-adj",
                symbol="SPY", side="BUY", qty=10, status="FILLED",
                raw_filled_price=raw_buy, filled_price=adj_buy,
                strategy_name="etf_momentum", reason="test",
                created_at_utc=t1, filled_at_utc=t1,
            ))
            session.add(OrderLog(
                broker_order_id="sell-adj",
                symbol="SPY", side="SELL", qty=10, status="FILLED",
                raw_filled_price=raw_sell, filled_price=adj_sell,
                strategy_name="etf_momentum", reason="test",
                created_at_utc=t2, filled_at_utc=t2,
            ))
            session.commit()

        with sf() as session:
            entries = get_journal(session)

        assert len(entries) == 1
        e = entries[0]
        # P&L should use adjusted prices, not raw
        assert e.entry_price == adj_buy
        assert e.exit_price == adj_sell
        expected_pnl = round((adj_sell - adj_buy) * 10, 2)
        assert e.pnl == expected_pnl
