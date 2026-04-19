"""Order recovery commands: backfill from Alpaca, repair missing fill prices."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import typer
from sqlalchemy import select

from bread.cli._app import app
from bread.cli._helpers import infer_strategy_from_symbol, load_strategy_universes
from bread.core.config import load_config
from bread.core.exceptions import BreadError
from bread.core.logging import setup_logging
from bread.db.database import get_engine, get_session_factory, init_db
from bread.db.models import OrderLog
from bread.execution.engine import adjust_fill_price

logger = logging.getLogger(__name__)


def _parse_ymd_utc(value: str, flag: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        typer.echo(f"Error: {flag} must be YYYY-MM-DD, got {value!r}", err=True)
        raise SystemExit(1) from exc


@app.command("backfill-orders")
def backfill_orders_cmd(
    from_: str = typer.Option(
        None,
        "--from",
        help="YYYY-MM-DD cutoff. Default: account creation date.",
    ),
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--no-dry-run",
        help="Preview changes without writing (default: dry-run).",
    ),
    batch_size: int = typer.Option(
        100,
        "--batch-size",
        help="Commit chunk size during the real run.",
    ),
) -> None:
    """Pull FILLED orders from Alpaca and insert missing rows into OrderLog.

    Idempotent: skips orders whose broker_order_id is already in the local
    database. Intended to recover round-trip history when SELLs were
    recorded locally but their matching BUYs predate local order_log
    coverage — those BUYs live in Alpaca and reappear here.
    """
    from bread.execution.alpaca_broker import AlpacaBroker

    try:
        config = load_config()
        setup_logging(config.app.log_level)
        engine_db = get_engine(config.db.path)
        init_db(engine_db)
        sf = get_session_factory(engine_db)

        broker = AlpacaBroker(config)

        if from_:
            after = _parse_ymd_utc(from_, "--from")
        else:
            after = broker.get_account_created_at()
            typer.echo(f"--from omitted; using account creation date {after.isoformat()}")

        universes = load_strategy_universes(config)

        typer.echo(f"Fetching Alpaca orders since {after.isoformat()} (status=closed)…")
        alpaca_orders = broker.list_historical_orders(after=after, status="closed")
        typer.echo(f"Alpaca returned {len(alpaca_orders)} orders.")

        with sf() as session:
            rows = session.execute(
                select(OrderLog.broker_order_id).where(
                    OrderLog.broker_order_id.is_not(None),
                )
            ).all()
            existing_ids: set[str] = {s for (s,) in rows if s is not None}

            inserted = 0
            skipped_duplicate = 0
            skipped_non_filled = 0
            skipped_no_price = 0
            skipped_no_qty = 0
            skipped_fractional = 0
            skipped_no_symbol = 0
            skipped_bad_side = 0
            skipped_no_timestamp = 0
            legacy_count = 0
            sample: list[str] = []
            committed_since_last = 0

            for idx, o in enumerate(alpaca_orders, start=1):
                if o.status is None or o.status.value != "FILLED":
                    skipped_non_filled += 1
                    continue

                order_id = o.id
                if order_id in existing_ids:
                    skipped_duplicate += 1
                    continue

                if not o.symbol:
                    skipped_no_symbol += 1
                    continue
                symbol = o.symbol

                if o.side is None:
                    # An unrecognized side would mis-apply paper slippage
                    # (adjust_fill_price treats anything != BUY as SELL).
                    logger.warning(
                        "Skipping order %s %s with unrecognized side",
                        order_id,
                        symbol,
                    )
                    skipped_bad_side += 1
                    continue
                side = o.side.value

                if o.filled_avg_price is None:
                    skipped_no_price += 1
                    continue
                raw = o.filled_avg_price
                if raw <= 0:
                    # A zero/negative fill price means Alpaca never populated
                    # the field; inserting it would break downstream P&L.
                    skipped_no_price += 1
                    continue
                adjusted = adjust_fill_price(config, raw, side)

                # qty on the DTO already falls back to filled_qty when raw
                # qty is None; a 0 here means neither was populated.
                qty_float = o.qty if o.qty else o.filled_qty
                if qty_float <= 0:
                    skipped_no_qty += 1
                    continue
                if qty_float != int(qty_float):
                    # Bread only trades whole shares; a fractional fill implies
                    # either an external order or Alpaca data drift we don't
                    # want to truncate.
                    logger.warning(
                        "Skipping fractional qty for %s id=%s qty=%s",
                        symbol,
                        order_id,
                        qty_float,
                    )
                    skipped_fractional += 1
                    continue
                qty = int(qty_float)

                created_at = o.submitted_at or o.created_at
                if created_at is None:
                    # OrderLog.created_at_utc is NOT NULL; a row without any
                    # timestamp is unusable and would trip an IntegrityError
                    # on commit.
                    skipped_no_timestamp += 1
                    continue

                strategy_name = infer_strategy_from_symbol(universes, symbol)
                if strategy_name == "legacy":
                    legacy_count += 1

                row = OrderLog(
                    broker_order_id=order_id,
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    status="FILLED",
                    stop_loss_price=None,
                    take_profit_price=None,
                    raw_filled_price=raw,
                    filled_price=adjusted,
                    strategy_name=strategy_name,
                    reason="backfill",
                    created_at_utc=created_at,
                    filled_at_utc=o.filled_at,
                )
                session.add(row)
                existing_ids.add(order_id)
                inserted += 1

                if len(sample) < 10:
                    sample.append(
                        f"  {symbol:<5} {side:<4} qty={qty:<4}"
                        f"  raw={raw:.2f}  adj={adjusted:.2f}"
                        f"  strategy={strategy_name}  at={o.filled_at}"
                    )

                if not dry_run:
                    committed_since_last += 1
                    if committed_since_last >= batch_size:
                        session.commit()
                        committed_since_last = 0
                        typer.echo(f"  … committed {idx}/{len(alpaca_orders)}")

            if not dry_run and committed_since_last:
                session.commit()
            elif dry_run:
                session.rollback()

        typer.echo("\nSummary:")
        typer.echo(f"  inserted:            {inserted}")
        typer.echo(f"  skipped_duplicate:   {skipped_duplicate}")
        typer.echo(f"  skipped_non_filled:  {skipped_non_filled}")
        typer.echo(f"  skipped_no_price:    {skipped_no_price}")
        typer.echo(f"  skipped_no_qty:      {skipped_no_qty}")
        typer.echo(f"  skipped_fractional:  {skipped_fractional}")
        typer.echo(f"  skipped_no_symbol:   {skipped_no_symbol}")
        typer.echo(f"  skipped_bad_side:    {skipped_bad_side}")
        typer.echo(f"  skipped_no_timestamp:{skipped_no_timestamp}")
        typer.echo(f"  tagged_legacy:       {legacy_count}")
        if sample:
            typer.echo("\nSample inserts:")
            for line in sample:
                typer.echo(line)
        if legacy_count:
            typer.echo(
                f"\nNote: {legacy_count} rows attributed to 'legacy' "
                "(no unique strategy owns the symbol). They will show up "
                "on the Strategies leaderboard until you decide how to "
                "display them."
            )
        if dry_run:
            typer.echo("\nDry run — no changes committed. Re-run with --no-dry-run to apply.")
        else:
            typer.echo("\nDone.")
    except BreadError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc


@app.command("repair-orders")
def repair_orders_cmd(
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--no-dry-run",
        help="Preview changes without writing (default: dry-run).",
    ),
    since: str = typer.Option(
        None,
        "--since",
        help="Only repair orders created on or after this date (YYYY-MM-DD).",
    ),
    batch_size: int = typer.Option(
        100,
        "--batch-size",
        help="Commit chunk size during the real run.",
    ),
) -> None:
    """Backfill filled_price / filled_at_utc for FILLED orders from Alpaca.

    Historical rows written before the status-normalization fix have
    status='FILLED' but no fill price (the fill-capture branch in
    _reconcile_orders was skipped due to the ORDERSTATUS.FILLED mismatch).
    This command re-fetches each such order from Alpaca by broker_order_id
    and populates raw_filled_price, filled_price (with paper-cost model),
    and filled_at_utc. Idempotent — already-repaired rows are skipped.
    """
    from bread.execution.alpaca_broker import AlpacaBroker

    try:
        config = load_config()
        setup_logging(config.app.log_level)
        engine_db = get_engine(config.db.path)
        init_db(engine_db)
        sf = get_session_factory(engine_db)

        since_dt: datetime | None = None
        if since:
            since_dt = _parse_ymd_utc(since, "--since")

        with sf() as session:
            stmt = select(OrderLog).where(
                OrderLog.status == "FILLED",
                OrderLog.filled_price.is_(None),
                OrderLog.broker_order_id.is_not(None),
            )
            if since_dt is not None:
                stmt = stmt.where(OrderLog.created_at_utc >= since_dt)
            stmt = stmt.order_by(OrderLog.created_at_utc.asc())
            targets = list(session.execute(stmt).scalars().all())

            typer.echo(f"Found {len(targets)} FILLED rows needing backfill.")
            if not targets:
                typer.echo("Nothing to do.")
                return

            broker = AlpacaBroker(config)

            repaired = 0
            missing_on_broker = 0
            no_fill_price = 0
            sample: list[str] = []
            committed_since_last = 0

            for idx, row in enumerate(targets, start=1):
                broker_order = broker.get_order_by_id(row.broker_order_id or "")
                if broker_order is None:
                    missing_on_broker += 1
                    logger.debug(
                        "Broker has no order for %s (id=%s)", row.symbol, row.broker_order_id
                    )
                    continue
                raw_value = getattr(broker_order, "filled_avg_price", None)
                if raw_value is None:
                    no_fill_price += 1
                    continue

                raw = float(raw_value)
                adjusted = adjust_fill_price(config, raw, row.side)
                row.raw_filled_price = raw
                row.filled_price = adjusted
                row.filled_at_utc = broker_order.filled_at
                repaired += 1

                if len(sample) < 10:
                    sample.append(
                        f"  {row.symbol:<5} {row.side}  qty={row.qty}"
                        f"  raw={raw:.2f}  adj={adjusted:.2f}  at={broker_order.filled_at}"
                    )

                if not dry_run:
                    committed_since_last += 1
                    if committed_since_last >= batch_size:
                        session.commit()
                        committed_since_last = 0
                        typer.echo(f"  … committed {idx}/{len(targets)}")

            if not dry_run and committed_since_last:
                session.commit()
            elif dry_run:
                session.rollback()

        typer.echo("\nSummary:")
        typer.echo(f"  repaired:          {repaired}")
        typer.echo(f"  missing_on_broker: {missing_on_broker}")
        typer.echo(f"  no_fill_price:     {no_fill_price}")
        if sample:
            typer.echo("\nSample updates:")
            for line in sample:
                typer.echo(line)
        if dry_run:
            typer.echo("\nDry run — no changes committed. Re-run with --no-dry-run to apply.")
        else:
            typer.echo("\nDone.")
    except BreadError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc
