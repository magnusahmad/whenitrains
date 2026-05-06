from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from .hko import HKT
from .runner import RunnerResult, run_paper_tick
from .storage import connect, migrate


REPLAY_TABLES = (
    "hko_forecasts",
    "ocf_forecast_samples",
    "hko_current_observations",
    "orderbook_snapshots",
)

PAPER_TABLES = (
    "paper_orders",
    "paper_positions",
    "paper_decisions",
    "signals",
    "paper_order_exclusions",
)


@dataclass(frozen=True)
class BacktestOrder:
    id: int
    created_at_hkt: str | None
    target_date_hkt: str | None
    label: str | None
    side: str | None
    limit_price: float | None
    fill_price: float | None
    fill_size_usd: float | None
    status: str | None
    reason: str | None


@dataclass(frozen=True)
class BacktestPosition:
    target_date_hkt: str | None
    label: str | None
    side: str | None
    net_shares: float
    avg_price: float
    realized_pnl: float


@dataclass(frozen=True)
class BacktestTick:
    tick_at_hkt: str
    result: RunnerResult


@dataclass(frozen=True)
class BacktestResult:
    source_db: Path
    replay_db: Path
    target_date: date
    tick_count: int
    active_ticks: list[BacktestTick]
    orders: list[BacktestOrder]
    positions: list[BacktestPosition]


def run_backtest_day(
    source_db: Path,
    target_date: date,
    replay_db: Path | None = None,
    tick_source: str = "scheduler",
    include_orderbook_ticks: bool = False,
    max_ticks: int | None = None,
) -> BacktestResult:
    if replay_db is None:
        replay_db = Path("/private/tmp") / f"whenitrains-backtest-{target_date.isoformat()}.sqlite3"
    replay_db.parent.mkdir(parents=True, exist_ok=True)
    if replay_db.exists():
        replay_db.unlink()
    _copy_sqlite_database(source_db, replay_db)

    db = connect(replay_db)
    migrate(db)
    db.execute("attach database ? as src", (str(source_db),))
    try:
        _reset_replay_tables(db)
        tick_times = _tick_times(
            db,
            target_date,
            tick_source=tick_source,
            include_orderbook_ticks=include_orderbook_ticks,
        )
        if max_ticks is not None:
            tick_times = tick_times[:max_ticks]
        source_rows = _load_source_rows(db)
        _create_replay_indexes(db)
        indexes = {table: 0 for table in REPLAY_TABLES}
        active_ticks: list[BacktestTick] = []

        for tick_at in tick_times:
            for table in REPLAY_TABLES:
                indexes[table] = _ingest_until(
                    db, table, source_rows[table], indexes[table], tick_at
                )
            db.commit()
            before = _max_ids(db)
            result = run_paper_tick(db, today_hkt=target_date)
            _stamp_new_paper_rows(db, before, tick_at)
            if _active_result(result):
                active_ticks.append(BacktestTick(_format_hkt(tick_at), result))

        return BacktestResult(
            source_db=source_db,
            replay_db=replay_db,
            target_date=target_date,
            tick_count=len(tick_times),
            active_ticks=active_ticks,
            orders=_load_orders(db),
            positions=_load_positions(db),
        )
    finally:
        db.execute("detach database src")
        db.close()


def render_backtest_result(result: BacktestResult) -> str:
    lines = [
        f"Backtest date: {result.target_date.isoformat()}",
        f"Source DB: {result.source_db}",
        f"Replay DB: {result.replay_db}",
        f"Ticks replayed: {result.tick_count}",
        f"Active ticks: {len(result.active_ticks)}",
        f"Orders: {len(result.orders)}",
        "",
        "Orders:",
    ]
    if not result.orders:
        lines.append("  none")
    for order in result.orders:
        lines.append(
            "  "
            f"{order.created_at_hkt or 'n/a'} | {order.target_date_hkt or 'n/a'} "
            f"{order.label or 'n/a'} {order.side or 'n/a'} | "
            f"{order.status or 'n/a'} | fill={_fmt(order.fill_price)} "
            f"usd={_fmt(order.fill_size_usd)} | {order.reason or ''}"
        )
    lines.append("")
    lines.append("Positions:")
    if not result.positions:
        lines.append("  none")
    for pos in result.positions:
        lines.append(
            "  "
            f"{pos.target_date_hkt or 'n/a'} {pos.label or 'n/a'} {pos.side or 'n/a'} | "
            f"shares={pos.net_shares:.4f} avg={pos.avg_price:.4f} "
            f"realized={pos.realized_pnl:.2f}"
        )
    lines.append("")
    lines.append("Active Tick Summary:")
    if not result.active_ticks:
        lines.append("  none")
    for tick in result.active_ticks:
        notes = "; ".join(tick.result.notes)
        lines.append(
            "  "
            f"{tick.tick_at_hkt} | buys={tick.result.buys_filled}/{tick.result.buys_missed} "
            f"sells={tick.result.sells_filled}/{tick.result.sells_missed} "
            f"signals={tick.result.signals} | {notes}"
        )
    return "\n".join(lines)


def _copy_sqlite_database(source_db: Path, replay_db: Path) -> None:
    source = sqlite3.connect(source_db)
    try:
        destination = sqlite3.connect(replay_db)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()


def backtest_result_json(result: BacktestResult) -> dict:
    return {
        "source_db": str(result.source_db),
        "replay_db": str(result.replay_db),
        "target_date": result.target_date.isoformat(),
        "tick_count": result.tick_count,
        "active_ticks": [
            {
                "tick_at_hkt": tick.tick_at_hkt,
                "buys_filled": tick.result.buys_filled,
                "buys_missed": tick.result.buys_missed,
                "sells_filled": tick.result.sells_filled,
                "sells_missed": tick.result.sells_missed,
                "signals": tick.result.signals,
                "notes": list(tick.result.notes),
            }
            for tick in result.active_ticks
        ],
        "orders": [order.__dict__ for order in result.orders],
        "positions": [position.__dict__ for position in result.positions],
    }


def _reset_replay_tables(db: sqlite3.Connection) -> None:
    for table in PAPER_TABLES + REPLAY_TABLES:
        db.execute(f"delete from {table}")
    db.commit()


def _create_replay_indexes(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        create index if not exists idx_backtest_orderbook_token_time
            on orderbook_snapshots(outcome_id, fetched_at_utc desc, id desc);
        create index if not exists idx_backtest_forecast_date_id
            on hko_forecasts(source_type, forecast_date_hkt, id desc);
        create index if not exists idx_backtest_ocf_date_time
            on ocf_forecast_samples(forecast_date_hkt, fetched_at_utc desc, id desc);
        create index if not exists idx_backtest_observation_time
            on hko_current_observations(observed_at_hkt desc, id desc);
        create index if not exists idx_backtest_decision_event_key
            on paper_decisions(event_key);
        create index if not exists idx_backtest_position_token
            on paper_positions(outcome_id);
        """
    )
    db.commit()


def _tick_times(
    db: sqlite3.Connection,
    target_date: date,
    tick_source: str,
    include_orderbook_ticks: bool,
) -> list[datetime]:
    day = target_date.isoformat()
    times: set[datetime] = set()
    if tick_source in {"scheduler", "both"}:
        rows = db.execute(
            """
            select distinct created_at_utc
            from src.paper_decisions
            where date(created_at_utc, '+8 hours') = ?
              and created_at_utc is not null
            """,
            (day,),
        ).fetchall()
        times.update(_parse_utc(row["created_at_utc"]) for row in rows)
    if tick_source in {"data", "both"}:
        for sql in _data_time_queries(include_orderbook_ticks):
            rows = db.execute(sql, (day,)).fetchall()
            times.update(_parse_utc(row[0]) for row in rows if row[0])
    return sorted(times)


def _data_time_queries(include_orderbook_ticks: bool) -> list[str]:
    queries = [
        """
        select distinct coalesce(r.fetched_at_utc, f.update_time)
        from src.hko_forecasts f
        left join src.raw_snapshots r on r.id = f.snapshot_id
        where date(coalesce(r.fetched_at_utc, f.update_time), '+8 hours') = ?
        """,
        """
        select distinct fetched_at_utc
        from src.ocf_forecast_samples
        where date(fetched_at_utc, '+8 hours') = ?
        """,
        """
        select distinct coalesce(r.fetched_at_utc, o.observed_at_hkt)
        from src.hko_current_observations o
        left join src.raw_snapshots r on r.id = o.snapshot_id
        where date(coalesce(r.fetched_at_utc, o.observed_at_hkt), '+8 hours') = ?
        """,
    ]
    if include_orderbook_ticks:
        queries.append(
            """
            select distinct fetched_at_utc
            from src.orderbook_snapshots
            where date(fetched_at_utc, '+8 hours') = ?
            """
        )
    return queries


def _load_source_rows(db: sqlite3.Connection) -> dict[str, list[tuple[datetime, tuple]]]:
    return {
        "hko_forecasts": _load_rows(
            db,
            "hko_forecasts",
            """
            select coalesce(r.fetched_at_utc, t.update_time) as availability, t.*
            from src.hko_forecasts t
            left join src.raw_snapshots r on r.id = t.snapshot_id
            order by availability asc, t.id asc
            """,
        ),
        "ocf_forecast_samples": _load_rows(
            db,
            "ocf_forecast_samples",
            """
            select t.fetched_at_utc as availability, t.*
            from src.ocf_forecast_samples t
            order by availability asc, t.id asc
            """,
        ),
        "hko_current_observations": _load_rows(
            db,
            "hko_current_observations",
            """
            select coalesce(r.fetched_at_utc, t.observed_at_hkt) as availability, t.*
            from src.hko_current_observations t
            left join src.raw_snapshots r on r.id = t.snapshot_id
            order by availability asc, t.id asc
            """,
        ),
        "orderbook_snapshots": _load_rows(
            db,
            "orderbook_snapshots",
            """
            select t.fetched_at_utc as availability, t.*
            from src.orderbook_snapshots t
            order by availability asc, t.id asc
            """,
        ),
    }


def _load_rows(
    db: sqlite3.Connection, table: str, sql: str
) -> list[tuple[datetime, tuple]]:
    columns = _table_columns(db, table)
    rows = db.execute(sql).fetchall()
    loaded = []
    for row in rows:
        availability = row["availability"]
        if availability is None:
            continue
        loaded.append((_parse_utc(availability), tuple(row[col] for col in columns)))
    return loaded


def _ingest_until(
    db: sqlite3.Connection,
    table: str,
    rows: list[tuple[datetime, tuple]],
    start_index: int,
    tick_at: datetime,
) -> int:
    columns = _table_columns(db, table)
    placeholders = ",".join("?" for _ in columns)
    column_sql = ",".join(columns)
    insert_sql = f"insert or ignore into {table} ({column_sql}) values ({placeholders})"
    index = start_index
    batch = []
    while index < len(rows) and rows[index][0] <= tick_at:
        batch.append(rows[index][1])
        index += 1
    if batch:
        db.executemany(insert_sql, batch)
    return index


def _table_columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in db.execute(f"pragma table_info({table})")]


def _max_ids(db: sqlite3.Connection) -> dict[str, int]:
    ids = {}
    for table in ("paper_orders", "paper_decisions", "signals"):
        ids[table] = db.execute(f"select coalesce(max(id), 0) from {table}").fetchone()[0]
    return ids


def _stamp_new_paper_rows(
    db: sqlite3.Connection, before: dict[str, int], tick_at: datetime
) -> None:
    timestamp = tick_at.astimezone(timezone.utc).isoformat()
    db.execute(
        "update paper_orders set created_at_utc = ? where id > ?",
        (timestamp, before["paper_orders"]),
    )
    db.execute(
        "update paper_decisions set created_at_utc = ? where id > ?",
        (timestamp, before["paper_decisions"]),
    )
    db.execute(
        "update signals set created_at_utc = ? where id > ?",
        (timestamp, before["signals"]),
    )
    db.execute(
        "update paper_positions set updated_at_utc = ?",
        (timestamp,),
    )
    db.commit()


def _load_orders(db: sqlite3.Connection) -> list[BacktestOrder]:
    rows = db.execute(
        """
        select po.id, po.created_at_utc, m.target_date_hkt, o.label, po.side,
               po.limit_price, po.simulated_fill_price, po.simulated_fill_size_usd,
               po.status, po.reason
        from paper_orders po
        left join outcomes o on o.yes_token_id = po.outcome_id or o.no_token_id = po.outcome_id
        left join markets m on m.id = o.market_id
        order by po.id asc
        """
    ).fetchall()
    return [
        BacktestOrder(
            id=row["id"],
            created_at_hkt=_format_hkt(_parse_utc(row["created_at_utc"])) if row["created_at_utc"] else None,
            target_date_hkt=row["target_date_hkt"],
            label=row["label"],
            side=row["side"],
            limit_price=row["limit_price"],
            fill_price=row["simulated_fill_price"],
            fill_size_usd=row["simulated_fill_size_usd"],
            status=row["status"],
            reason=row["reason"],
        )
        for row in rows
    ]


def _load_positions(db: sqlite3.Connection) -> list[BacktestPosition]:
    rows = db.execute(
        """
        select m.target_date_hkt, o.label,
               case
                   when p.outcome_id = o.yes_token_id then 'YES'
                   when p.outcome_id = o.no_token_id then 'NO'
                   else null
               end as side,
               p.net_shares, p.avg_price, p.realized_pnl
        from paper_positions p
        left join outcomes o on o.yes_token_id = p.outcome_id or o.no_token_id = p.outcome_id
        left join markets m on m.id = o.market_id
        where p.net_shares > 0 or abs(p.realized_pnl) > 0
        order by m.target_date_hkt, o.label
        """
    ).fetchall()
    return [
        BacktestPosition(
            target_date_hkt=row["target_date_hkt"],
            label=row["label"],
            side=row["side"],
            net_shares=float(row["net_shares"]),
            avg_price=float(row["avg_price"]),
            realized_pnl=float(row["realized_pnl"]),
        )
        for row in rows
    ]


def _active_result(result: RunnerResult) -> bool:
    return any(
        (
            result.buys_filled,
            result.buys_missed,
            result.sells_filled,
            result.sells_missed,
            result.signals,
        )
    )


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_hkt(value: datetime) -> str:
    return value.astimezone(HKT).strftime("%Y-%m-%d %H:%M:%S")


def _fmt(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.4f}"


def dumps_result_json(result: BacktestResult) -> str:
    return json.dumps(backtest_result_json(result), indent=2)
