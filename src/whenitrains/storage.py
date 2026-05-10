from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, time as day_time, timezone
from pathlib import Path
from typing import Callable

from .config import Settings
from .hko import HKT, HkoCurrentTemperature, HkoForecast, HkoObservation, OcfForecastSample
from .polymarket import OrderBook, TemperatureMarket


@dataclass(frozen=True)
class RawSnapshotRecord:
    id: int
    content_hash: str


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30.0)
    db.row_factory = sqlite3.Row
    db.execute("pragma busy_timeout = 30000")
    db.execute("pragma journal_mode = WAL")
    return db


def backup_sqlite_database(
    db_path: Path,
    backup_dir: Path | None = None,
    keep: int | None = 5,
) -> Path:
    if not db_path.exists():
        raise FileNotFoundError(f"database does not exist: {db_path}")
    if backup_dir is None:
        backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    backup_path = backup_dir / f"{db_path.stem}-{timestamp}.sqlite3"
    source = sqlite3.connect(db_path)
    try:
        destination = sqlite3.connect(backup_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    _assert_sqlite_backup_ok(backup_path)
    if keep is not None and keep > 0:
        _prune_old_backups(backup_dir, db_path.stem, keep)
    return backup_path


def _assert_sqlite_backup_ok(backup_path: Path) -> None:
    db = sqlite3.connect(backup_path)
    try:
        result = db.execute("pragma integrity_check").fetchone()
    finally:
        db.close()
    if result is None or result[0] != "ok":
        raise sqlite3.DatabaseError(f"backup integrity check failed: {backup_path}")


def _prune_old_backups(backup_dir: Path, db_stem: str, keep: int) -> None:
    backups = sorted(
        backup_dir.glob(f"{db_stem}-*.sqlite3"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for stale in backups[keep:]:
        stale.unlink()


def migrate(db: sqlite3.Connection) -> None:
    _rebuild_raw_snapshots_without_unique_hash(db)
    db.executescript(
        """
        create table if not exists raw_snapshots (
            id integer primary key autoincrement,
            source text not null,
            endpoint text not null,
            fetched_at_utc text not null,
            content_hash text not null,
            payload text not null,
            response_headers_json text,
            http_date text,
            http_last_modified text,
            http_etag text,
            fetch_started_at_utc text,
            headers_received_at_utc text,
            payload_received_at_utc text,
            response_elapsed_ms real
        );

        create table if not exists hko_forecasts (
            id integer primary key autoincrement,
            snapshot_id integer,
            source_type text not null,
            forecast_date_hkt text,
            forecast_min_c real,
            forecast_max_c real,
            weather_text text,
            wind_text text,
            psr text,
            update_time text,
            parse_warning integer not null default 0,
            raw_forecast text
        );

        create table if not exists ocf_forecast_samples (
            id integer primary key autoincrement,
            snapshot_id integer,
            fetched_at_utc text not null,
            forecast_date_hkt text not null,
            forecast_min_c real,
            forecast_max_c real,
            raw_min_c real,
            raw_max_c real,
            hourly_temperatures_json text,
            raw_daily_forecast text
        );

        create table if not exists hko_current_observations (
            id integer primary key autoincrement,
            snapshot_id integer,
            observed_at_hkt text,
            station text,
            temperature_c real,
            since_midnight_min_c real,
            since_midnight_max_c real,
            humidity_pct real,
            rainfall_mm real,
            raw_observation text
        );

        create table if not exists markets (
            id integer primary key autoincrement,
            polymarket_event_id text,
            polymarket_market_id text,
            slug text,
            question text,
            target_date_hkt text,
            status text,
            resolution_source_text text,
            raw_market text
        );

        create table if not exists outcomes (
            id integer primary key autoincrement,
            market_id integer,
            polymarket_market_id text,
            yes_token_id text,
            no_token_id text,
            label text,
            predicate_type text,
            predicate_value_c real,
            raw_outcome text
        );

        create table if not exists orderbook_snapshots (
            id integer primary key autoincrement,
            outcome_id text,
            fetched_at_utc text,
            best_bid real,
            best_ask real,
            mid real,
            depth_json text
        );

        create table if not exists signals (
            id integer primary key autoincrement,
            created_at_utc text,
            market_id text,
            trigger_type text,
            current_max_c real,
            forecast_max_c real,
            affected_outcomes_json text,
            directional_impacts_json text,
            pre_event_prices_json text,
            post_event_prices_json text,
            price_response_json text,
            notes text
        );

        create table if not exists paper_orders (
            id integer primary key autoincrement,
            created_at_utc text,
            signal_id text,
            outcome_id text,
            side text,
            limit_price real,
            size_usd real,
            simulated_fill_price real,
            simulated_fill_size_usd real,
            status text,
            reason text
        );

        create table if not exists paper_positions (
            outcome_id text primary key,
            net_shares real,
            avg_price real,
            realized_pnl real,
            updated_at_utc text
        );

        create table if not exists paper_order_exclusions (
            order_id integer primary key,
            tag text not null,
            reason text not null,
            created_at_utc text not null
        );

        create table if not exists live_orders (
            id integer primary key autoincrement,
            created_at_utc text not null,
            submitted_at_utc text,
            reconciled_at_utc text,
            event_type text,
            event_key text,
            outcome_id text not null,
            label text,
            side text not null,
            action text not null,
            clob_order_id text,
            order_type text,
            status text not null,
            requested_size_usd real,
            requested_shares real,
            limit_price real,
            fill_price real,
            fill_size_usd real,
            fill_shares real,
            reason text,
            error text,
            raw_request_json text,
            raw_response_json text,
            raw_reconcile_json text
        );

        create table if not exists live_positions (
            outcome_id text primary key,
            net_shares real not null,
            avg_price real not null,
            realized_pnl real not null,
            updated_at_utc text not null,
            last_reconciled_at_utc text
        );

        create table if not exists live_settings (
            name text primary key,
            value text not null,
            updated_at_utc text not null
        );

        create table if not exists live_user_events (
            id integer primary key autoincrement,
            event_id text not null unique,
            received_at_utc text not null,
            event_type text not null,
            clob_order_id text,
            outcome_id text,
            status text,
            side text,
            price real,
            size real,
            applied_position_delta integer not null default 0,
            raw_event_json text not null
        );

        create table if not exists risk_events (
            id integer primary key autoincrement,
            created_at_utc text,
            event_type text,
            severity text,
            details_json text
        );

        create table if not exists paper_decisions (
            id integer primary key autoincrement,
            created_at_utc text,
            event_type text,
            event_key text,
            outcome_id text,
            label text,
            side text,
            action text,
            status text,
            reason text,
            details_json text
        );

        create table if not exists hko_source_update_minutes (
            id integer primary key autoincrement,
            source text not null,
            update_minute_hkt text not null,
            first_seen_utc text not null,
            last_seen_utc text not null,
            seen_count integer not null,
            evidence_json text,
            unique(source, update_minute_hkt)
        );

        create table if not exists latency_trace_events (
            id integer primary key autoincrement,
            event_key text not null,
            stage text not null,
            event_type text,
            monotonic_ts real not null,
            recorded_at_utc text not null,
            details_json text
        );

        create table if not exists experiment_runs (
            id integer primary key autoincrement,
            name text not null,
            strategy text not null,
            config_json text not null,
            source_db text,
            target_start_hkt text,
            target_end_hkt text,
            created_at_utc text not null
        );

        create table if not exists experiment_decisions (
            id integer primary key autoincrement,
            run_id integer not null,
            created_at_utc text not null,
            event_key text not null,
            target_date_hkt text,
            outcome_id text,
            label text,
            side text,
            action text,
            status text,
            reason text,
            details_json text,
            unique(run_id, event_key)
        );

        create table if not exists experiment_orders (
            id integer primary key autoincrement,
            run_id integer not null,
            created_at_utc text not null,
            decision_id integer,
            outcome_id text,
            label text,
            side text,
            action text,
            status text,
            limit_price real,
            requested_size_usd real,
            fill_price real,
            fill_size_usd real,
            fill_shares real,
            reason text,
            details_json text
        );

        create table if not exists experiment_positions (
            run_id integer not null,
            outcome_id text not null,
            label text,
            side text,
            net_shares real not null,
            avg_price real not null,
            realized_pnl real not null,
            updated_at_utc text not null,
            primary key(run_id, outcome_id)
        );

        create table if not exists experiment_metrics (
            id integer primary key autoincrement,
            run_id integer not null,
            name text not null,
            value real,
            details_json text,
            created_at_utc text not null
        );

        create index if not exists idx_orderbook_snapshots_latest
        on orderbook_snapshots(outcome_id, fetched_at_utc desc, id desc);

        create index if not exists idx_ocf_forecast_samples_latest
        on ocf_forecast_samples(forecast_date_hkt, fetched_at_utc desc, id desc);

        create index if not exists idx_hko_forecasts_latest
        on hko_forecasts(source_type, forecast_date_hkt, id desc);

        create index if not exists idx_hko_current_observations_latest
        on hko_current_observations(observed_at_hkt, id desc);

        create index if not exists idx_latency_trace_events_key
        on latency_trace_events(event_key, id);
        """
    )
    _add_column_if_missing(db, "raw_snapshots", "response_headers_json", "text")
    _add_column_if_missing(db, "raw_snapshots", "http_date", "text")
    _add_column_if_missing(db, "raw_snapshots", "http_last_modified", "text")
    _add_column_if_missing(db, "raw_snapshots", "http_etag", "text")
    _add_column_if_missing(db, "raw_snapshots", "fetch_started_at_utc", "text")
    _add_column_if_missing(db, "raw_snapshots", "headers_received_at_utc", "text")
    _add_column_if_missing(db, "raw_snapshots", "payload_received_at_utc", "text")
    _add_column_if_missing(db, "raw_snapshots", "response_elapsed_ms", "real")
    _add_column_if_missing(db, "paper_decisions", "event_key", "text")
    _add_column_if_missing(db, "hko_forecasts", "update_time", "text")
    _add_column_if_missing(
        db, "hko_forecasts", "parse_warning", "integer not null default 0"
    )
    db.commit()


def _add_column_if_missing(
    db: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    existing = {row["name"] for row in db.execute(f"pragma table_info({table})")}
    if column not in existing:
        db.execute(f"alter table {table} add column {column} {definition}")


def _rebuild_raw_snapshots_without_unique_hash(db: sqlite3.Connection) -> None:
    row = db.execute(
        """
        select sql from sqlite_master
        where type = 'table' and name = 'raw_snapshots'
        """
    ).fetchone()
    if row is None or "content_hash text not null unique" not in (row["sql"] or ""):
        return
    db.executescript(
        """
        alter table raw_snapshots rename to raw_snapshots_old;
        create table raw_snapshots (
            id integer primary key autoincrement,
            source text not null,
            endpoint text not null,
            fetched_at_utc text not null,
            content_hash text not null,
            payload text not null,
            response_headers_json text,
            http_date text,
            http_last_modified text,
            http_etag text,
            fetch_started_at_utc text,
            headers_received_at_utc text,
            payload_received_at_utc text,
            response_elapsed_ms real
        );
        insert into raw_snapshots
        (id, source, endpoint, fetched_at_utc, content_hash, payload)
        select id, source, endpoint, fetched_at_utc, content_hash, payload
        from raw_snapshots_old;
        drop table raw_snapshots_old;
        """
    )


def store_raw_snapshot(
    db: sqlite3.Connection,
    source: str,
    endpoint: str,
    payload: str,
    response_headers: dict[str, str] | None = None,
    *,
    fetch_started_at_utc: str | None = None,
    headers_received_at_utc: str | None = None,
    payload_received_at_utc: str | None = None,
    response_elapsed_ms: float | None = None,
) -> RawSnapshotRecord:
    content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    headers = response_headers or {}
    cursor = db.execute(
        """
        insert into raw_snapshots
        (source, endpoint, fetched_at_utc, content_hash, payload,
         response_headers_json, http_date, http_last_modified, http_etag,
         fetch_started_at_utc, headers_received_at_utc, payload_received_at_utc,
         response_elapsed_ms)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source,
            endpoint,
            now,
            content_hash,
            payload,
            json.dumps(headers),
            headers.get("Date"),
            headers.get("Last-Modified"),
            headers.get("Etag") or headers.get("ETag"),
            fetch_started_at_utc,
            headers_received_at_utc,
            payload_received_at_utc,
            response_elapsed_ms,
        ),
    )
    db.commit()
    return RawSnapshotRecord(id=int(cursor.lastrowid), content_hash=content_hash)


def record_hko_update_minute(
    db: sqlite3.Connection,
    source: str,
    update_time_hkt: datetime,
    evidence: dict,
) -> None:
    minute = update_time_hkt.strftime("%H:%M")
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """
        insert into hko_source_update_minutes
        (source, update_minute_hkt, first_seen_utc, last_seen_utc, seen_count, evidence_json)
        values (?, ?, ?, ?, 1, ?)
        on conflict(source, update_minute_hkt) do update set
            last_seen_utc = excluded.last_seen_utc,
            seen_count = hko_source_update_minutes.seen_count + 1,
            evidence_json = excluded.evidence_json
        """,
        (source, minute, now, now, json.dumps(evidence)),
    )
    db.commit()


def list_hko_update_times(db: sqlite3.Connection, source: str) -> list[day_time]:
    rows = db.execute(
        """
        select update_minute_hkt
        from hko_source_update_minutes
        where source = ?
        order by update_minute_hkt
        """,
        (source,),
    )
    times = []
    for row in rows:
        try:
            times.append(datetime.strptime(row["update_minute_hkt"], "%H:%M").time())
        except ValueError:
            continue
    return times


def store_hko_observation(
    db: sqlite3.Connection, snapshot_id: int, observation: HkoObservation
) -> None:
    db.execute(
        """
        insert into hko_current_observations
        (snapshot_id, observed_at_hkt, station, temperature_c,
         since_midnight_min_c, since_midnight_max_c, raw_observation)
        values (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            observation.observed_at_hkt.isoformat(),
            observation.station,
            None,
            observation.since_midnight_min_c,
            observation.since_midnight_max_c,
            json.dumps(observation.raw),
        ),
    )
    db.commit()


def store_hko_current_temperature(
    db: sqlite3.Connection,
    snapshot_id: int,
    observation: HkoCurrentTemperature,
    *,
    event_queue=None,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> None:
    cursor = db.execute(
        """
        insert into hko_current_observations
        (snapshot_id, observed_at_hkt, station, temperature_c,
         since_midnight_min_c, since_midnight_max_c, raw_observation)
        values (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            observation.observed_at_hkt.isoformat(),
            observation.station,
            observation.temperature_c,
            observation.since_midnight_min_c,
            observation.since_midnight_max_c,
            json.dumps(observation.raw),
        ),
    )
    db.commit()
    if event_queue is not None:
        from .low_latency import enqueue_hko_actual_transition_events

        enqueue_hko_actual_transition_events(
            db,
            event_queue,
            observation_id=int(cursor.lastrowid),
            committed_monotonic=monotonic_fn(),
            monotonic_fn=monotonic_fn,
        )


def record_latency_stage(
    db: sqlite3.Connection,
    event_key: str,
    stage: str,
    monotonic_ts: float,
    event_type: str | None = None,
    details: dict | None = None,
) -> None:
    db.execute(
        """
        insert into latency_trace_events
        (event_key, stage, event_type, monotonic_ts, recorded_at_utc, details_json)
        values (?, ?, ?, ?, ?, ?)
        """,
        (
            event_key,
            stage,
            event_type,
            monotonic_ts,
            datetime.now(timezone.utc).isoformat(),
            json.dumps(details or {}),
        ),
    )
    db.commit()


def latency_stages_for_event(
    db: sqlite3.Connection, event_key: str
) -> list[sqlite3.Row]:
    return list(
        db.execute(
            """
            select event_key, stage, event_type, monotonic_ts, recorded_at_utc, details_json
            from latency_trace_events
            where event_key = ?
            order by id asc
            """,
            (event_key,),
        )
    )


def latency_duration_summary(
    db: sqlite3.Connection, start_stage: str, end_stage: str
) -> dict:
    rows = db.execute(
        """
        select start.event_key,
               min(start.monotonic_ts) as start_ts,
               min(finish.monotonic_ts) as end_ts
        from latency_trace_events start
        join latency_trace_events finish on finish.event_key = start.event_key
        where start.stage = ?
          and finish.stage = ?
          and finish.monotonic_ts >= start.monotonic_ts
        group by start.event_key
        order by end_ts - start_ts
        """,
        (start_stage, end_stage),
    ).fetchall()
    durations = [float(row["end_ts"]) - float(row["start_ts"]) for row in rows]
    return {
        "start_stage": start_stage,
        "end_stage": end_stage,
        "count": len(durations),
        "p50_seconds": _nearest_rank_percentile(durations, 0.50),
        "p95_seconds": _nearest_rank_percentile(durations, 0.95),
        "p99_seconds": _nearest_rank_percentile(durations, 0.99),
    }


def _nearest_rank_percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    index = max(0, min(len(values) - 1, int(len(values) * percentile + 0.999999) - 1))
    return values[index]


def store_hko_forecasts(
    db: sqlite3.Connection, snapshot_id: int, forecasts: list[HkoForecast]
) -> None:
    for forecast in forecasts:
        db.execute(
            """
            insert into hko_forecasts
            (snapshot_id, source_type, forecast_date_hkt, forecast_min_c,
             forecast_max_c, weather_text, wind_text, psr, update_time,
             parse_warning, raw_forecast)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                forecast.source_type,
                forecast.forecast_date_hkt.isoformat()
                if forecast.forecast_date_hkt
                else None,
                forecast.forecast_min_c,
                forecast.forecast_max_c,
                forecast.weather_text,
                forecast.wind_text,
                forecast.psr,
                forecast.update_time,
                1 if forecast.parse_warning else 0,
                json.dumps(forecast.raw or {}),
            ),
        )
    db.commit()


def store_ocf_forecast_samples(
    db: sqlite3.Connection,
    snapshot_id: int,
    samples: list[OcfForecastSample],
    *,
    event_queue=None,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    sample_ids: list[int] = []
    for sample in samples:
        cursor = db.execute(
            """
            insert into ocf_forecast_samples
            (snapshot_id, fetched_at_utc, forecast_date_hkt, forecast_min_c,
             forecast_max_c, raw_min_c, raw_max_c, hourly_temperatures_json,
             raw_daily_forecast)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                now,
                sample.forecast_date_hkt.isoformat(),
                sample.forecast_min_c,
                sample.forecast_max_c,
                sample.raw_min_c,
                sample.raw_max_c,
                json.dumps(sample.hourly_temperatures),
                json.dumps(sample.raw),
            ),
        )
        sample_ids.append(int(cursor.lastrowid))
    db.commit()
    if event_queue is not None and sample_ids:
        from .low_latency import enqueue_ocf_forecast_sample_events

        enqueue_ocf_forecast_sample_events(
            db,
            event_queue,
            sample_ids=sample_ids,
            committed_monotonic=monotonic_fn(),
            monotonic_fn=monotonic_fn,
        )


def store_polymarket_event(
    db: sqlite3.Connection,
    market: TemperatureMarket,
    *,
    event_queue=None,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> None:
    existing = db.execute(
        "select id, status, target_date_hkt from markets where slug = ? order by id desc limit 1",
        (market.event_slug,),
    ).fetchone()
    if existing is not None:
        local_market_id = int(existing["id"])
        previous_status = str(existing["status"] or "")
        db.execute(
            """
            update markets
            set polymarket_event_id = ?,
                question = ?,
                target_date_hkt = ?,
                status = ?,
                resolution_source_text = ?,
                raw_market = ?
            where id = ?
            """,
            (
                market.event_id,
                market.title,
                market.target_date.isoformat() if market.target_date else None,
                market.status,
                market.resolution_rules_text,
                json.dumps(market.raw_event or {}),
                local_market_id,
            ),
        )
        db.commit()
        if (
            event_queue is not None
            and market.target_date is not None
            and previous_status != market.status
        ):
            from .low_latency import enqueue_market_resolution_event

            enqueue_market_resolution_event(
                db,
                event_queue,
                market_id=local_market_id,
                target_date_hkt=market.target_date.isoformat(),
                previous_status=previous_status,
                new_status=market.status,
                committed_monotonic=monotonic_fn(),
                monotonic_fn=monotonic_fn,
            )
        return
    else:
        market_row = db.execute(
            """
            insert into markets
            (polymarket_event_id, slug, question, target_date_hkt, status,
             resolution_source_text, raw_market)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                market.event_id,
                market.event_slug,
                market.title,
                market.target_date.isoformat() if market.target_date else None,
                market.status,
                market.resolution_rules_text,
                json.dumps(market.raw_event or {}),
            ),
        )
        local_market_id = market_row.lastrowid
    for outcome in market.outcomes:
        db.execute(
            """
            insert into outcomes
            (market_id, polymarket_market_id, yes_token_id, no_token_id, label,
             predicate_type, predicate_value_c, raw_outcome)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                local_market_id,
                outcome.market_id,
                outcome.yes_token_id,
                outcome.no_token_id,
                outcome.label,
                outcome.predicate.type.value,
                outcome.predicate.value_c,
                "{}",
            ),
        )
    db.commit()


def store_risk_event(
    db: sqlite3.Connection,
    event_type: str,
    severity: str,
    details: dict,
) -> None:
    db.execute(
        """
        insert into risk_events
        (created_at_utc, event_type, severity, details_json)
        values (?, ?, ?, ?)
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            event_type,
            severity,
            json.dumps(details),
        ),
    )
    db.commit()


def store_orderbook(
    db: sqlite3.Connection,
    outcome_id: str,
    book: OrderBook,
    metadata: dict | None = None,
) -> None:
    best_bid = book.best_bid
    best_ask = book.best_ask
    mid = (best_bid + best_ask) / 2 if best_bid is not None and best_ask is not None else None
    db.execute(
        """
        insert into orderbook_snapshots
        (outcome_id, fetched_at_utc, best_bid, best_ask, mid, depth_json)
        values (?, ?, ?, ?, ?, ?)
        """,
        (
            outcome_id,
            datetime.now(timezone.utc).isoformat(),
            best_bid,
            best_ask,
            mid,
            json.dumps(
                {
                    "bids": book.bids,
                    "asks": book.asks,
                    "tick_size": book.tick_size,
                    "min_order_size": book.min_order_size,
                    **(metadata or {}),
                }
            ),
        ),
    )
    db.commit()


def latest_orderbook(db: sqlite3.Connection, token_id: str) -> OrderBook:
    row = db.execute(
        """
        select depth_json from orderbook_snapshots
        where outcome_id = ?
        order by fetched_at_utc desc, id desc
        limit 1
        """,
        (token_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no orderbook snapshot for token {token_id}")
    depth = json.loads(row["depth_json"])
    bids = sorted([tuple(item) for item in depth.get("bids", [])], reverse=True)
    asks = sorted([tuple(item) for item in depth.get("asks", [])])
    return OrderBook(
        token_id=token_id,
        bids=bids,
        asks=asks,
        tick_size=float(depth.get("tick_size", 0.01)),
        min_order_size=float(depth.get("min_order_size", 5)),
    )


def list_outcomes(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        db.execute(
            """
            select id, label, predicate_type, predicate_value_c, yes_token_id, no_token_id
            from outcomes
            order by predicate_value_c, label
            """
        )
    )


def list_outcomes_for_date(db: sqlite3.Connection, target_date_hkt: str) -> list[sqlite3.Row]:
    return list(
        db.execute(
            """
            select o.id, o.market_id, o.polymarket_market_id, o.label,
                   o.predicate_type, o.predicate_value_c, o.yes_token_id, o.no_token_id,
                   m.target_date_hkt, m.slug, m.status
            from outcomes o
            join markets m on m.id = o.market_id
            where m.target_date_hkt = ?
            order by o.predicate_value_c, o.label
            """,
            (target_date_hkt,),
        )
    )


def list_outcomes_from_date(db: sqlite3.Connection, min_date_hkt: str) -> list[sqlite3.Row]:
    return list(
        db.execute(
            """
            select o.id, o.market_id, o.polymarket_market_id, o.label,
                   o.predicate_type, o.predicate_value_c, o.yes_token_id, o.no_token_id,
                   m.target_date_hkt, m.slug, m.status
            from outcomes o
            join markets m on m.id = o.market_id
            where m.target_date_hkt >= ?
            order by m.target_date_hkt, o.predicate_value_c, o.label
            """,
            (min_date_hkt,),
        )
    )


def list_active_market_token_ids(db: sqlite3.Connection, min_date_hkt: str) -> list[str]:
    rows = db.execute(
        """
        select o.yes_token_id, o.no_token_id
        from outcomes o
        join markets m on m.id = o.market_id
        where m.target_date_hkt >= ?
          and coalesce(m.status, 'active') = 'active'
        order by m.target_date_hkt, o.predicate_value_c, o.label, o.id
        """,
        (min_date_hkt,),
    )
    token_ids: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for token_id in (row["yes_token_id"], row["no_token_id"]):
            if token_id and token_id not in seen:
                seen.add(token_id)
                token_ids.append(token_id)
    return token_ids


def list_active_market_condition_ids(
    db: sqlite3.Connection, min_date_hkt: str
) -> list[str]:
    rows = db.execute(
        """
        select o.polymarket_market_id
        from outcomes o
        join markets m on m.id = o.market_id
        where m.target_date_hkt >= ?
          and coalesce(m.status, 'active') = 'active'
          and o.polymarket_market_id is not null
        order by m.target_date_hkt, o.id
        """,
        (min_date_hkt,),
    )
    condition_ids: list[str] = []
    seen: set[str] = set()
    for row in rows:
        condition_id = row["polymarket_market_id"]
        if condition_id and condition_id not in seen:
            seen.add(condition_id)
            condition_ids.append(condition_id)
    return condition_ids


def list_hko_forecast_dates(
    db: sqlite3.Connection, min_date_hkt: str | None = None
) -> list[str]:
    params: tuple[str, ...] = ()
    date_filter = ""
    if min_date_hkt is not None:
        date_filter = "and forecast_date_hkt >= ?"
        params = (min_date_hkt,)
    return [
        row["forecast_date_hkt"]
        for row in db.execute(
            f"""
            select distinct forecast_date_hkt
            from hko_forecasts
            where source_type = 'ocf_station'
              and forecast_date_hkt is not null
              and forecast_max_c is not null
              and coalesce(parse_warning, 0) = 0
              {date_filter}
            order by forecast_date_hkt
            """,
            params,
        )
    ]


def list_tradeable_forecast_dates(
    db: sqlite3.Connection, min_date_hkt: str | None = None
) -> list[str]:
    params: tuple[str, ...] = ()
    date_filter = ""
    if min_date_hkt is not None:
        date_filter = "and m.target_date_hkt >= ?"
        params = (min_date_hkt,)
    return [
        row["target_date_hkt"]
        for row in db.execute(
            f"""
            select distinct m.target_date_hkt
            from markets m
            join hko_forecasts f on f.forecast_date_hkt = m.target_date_hkt
            where m.target_date_hkt is not null
              and (f.forecast_min_c is not null or f.forecast_max_c is not null)
              and coalesce(f.parse_warning, 0) = 0
              and f.source_type = 'ocf_station'
              {date_filter}
            order by m.target_date_hkt
            """,
            params,
        )
    ]


def find_outcome_by_label(db: sqlite3.Connection, label: str) -> sqlite3.Row:
    row = db.execute(
        """
        select id, label, predicate_type, predicate_value_c, yes_token_id, no_token_id
        from outcomes
        where label = ?
        order by id desc
        limit 1
        """,
        (label,),
    ).fetchone()
    if row is None:
        raise ValueError(f"outcome label not found: {label}")
    return row


def find_outcome_by_label_and_filters(
    db: sqlite3.Connection,
    label: str,
    *,
    target_date_hkt: str | None = None,
    slug_contains: str | None = None,
) -> sqlite3.Row:
    filters = ["o.label = ?"]
    params: list[str] = [label]
    if target_date_hkt is not None:
        filters.append("m.target_date_hkt = ?")
        params.append(target_date_hkt)
    if slug_contains is not None:
        filters.append("m.slug like ?")
        params.append(f"%{slug_contains}%")
    rows = list(
        db.execute(
            f"""
            select o.id, o.market_id, o.polymarket_market_id, o.label,
                   o.predicate_type, o.predicate_value_c, o.yes_token_id, o.no_token_id,
                   m.target_date_hkt, m.slug, m.status
            from outcomes o
            join markets m on m.id = o.market_id
            where {" and ".join(filters)}
            order by m.target_date_hkt desc, m.slug, o.id desc
            """,
            tuple(params),
        )
    )
    if not rows:
        raise ValueError(f"outcome label not found: {label}")
    if len(rows) > 1:
        choices = ", ".join(
            f"{row['target_date_hkt']} {row['slug']}" for row in rows[:5]
        )
        raise ValueError(
            f"ambiguous outcome label {label}; use --date or --market-kind. matches: {choices}"
        )
    return rows[0]


def find_outcome_by_token(db: sqlite3.Connection, token_id: str) -> sqlite3.Row | None:
    return db.execute(
        """
        select o.id, o.market_id, o.polymarket_market_id, o.label,
               o.predicate_type, o.predicate_value_c, o.yes_token_id, o.no_token_id,
               m.target_date_hkt, m.slug, m.status
        from outcomes o
        join markets m on m.id = o.market_id
        where o.yes_token_id = ? or o.no_token_id = ?
        order by o.id desc
        limit 1
        """,
        (token_id, token_id),
    ).fetchone()


def store_paper_order_result(
    db: sqlite3.Connection,
    token_id: str,
    side: str,
    limit_price: float | None,
    size_usd: float,
    fill_price: float | None,
    fill_size_usd: float,
    status: str,
    reason: str,
) -> None:
    db.execute(
        """
        insert into paper_orders
        (created_at_utc, outcome_id, side, limit_price, size_usd,
         simulated_fill_price, simulated_fill_size_usd, status, reason)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            token_id,
            side,
            limit_price,
            size_usd,
            fill_price,
            fill_size_usd,
            status,
            reason,
        ),
    )
    db.commit()


def get_paper_position(db: sqlite3.Connection, token_id: str) -> sqlite3.Row | None:
    return db.execute(
        "select * from paper_positions where outcome_id = ?", (token_id,)
    ).fetchone()


def list_open_paper_positions(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        db.execute(
            """
            select * from paper_positions
            where net_shares > 0
            order by updated_at_utc
            """
        )
    )


def upsert_paper_position(
    db: sqlite3.Connection,
    token_id: str,
    shares: float,
    avg_price: float,
    realized_pnl: float,
) -> None:
    db.execute(
        """
        insert into paper_positions
        (outcome_id, net_shares, avg_price, realized_pnl, updated_at_utc)
        values (?, ?, ?, ?, ?)
        on conflict(outcome_id) do update set
            net_shares = excluded.net_shares,
            avg_price = excluded.avg_price,
            realized_pnl = excluded.realized_pnl,
            updated_at_utc = excluded.updated_at_utc
        """,
        (
            token_id,
            shares,
            avg_price,
            realized_pnl,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    db.commit()


def store_live_order(
    db: sqlite3.Connection,
    *,
    outcome_id: str,
    side: str,
    action: str,
    status: str,
    label: str | None = None,
    event_type: str | None = None,
    event_key: str | None = None,
    clob_order_id: str | None = None,
    order_type: str | None = None,
    requested_size_usd: float | None = None,
    requested_shares: float | None = None,
    limit_price: float | None = None,
    fill_price: float | None = None,
    fill_size_usd: float | None = None,
    fill_shares: float | None = None,
    reason: str | None = None,
    error: str | None = None,
    raw_request: dict | None = None,
    raw_response: dict | None = None,
    raw_reconcile: dict | None = None,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cursor = db.execute(
        """
        insert into live_orders
        (created_at_utc, submitted_at_utc, reconciled_at_utc, event_type, event_key,
         outcome_id, label, side, action, clob_order_id, order_type, status,
         requested_size_usd, requested_shares, limit_price, fill_price,
         fill_size_usd, fill_shares, reason, error, raw_request_json,
         raw_response_json, raw_reconcile_json)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now,
            now if status not in ("rejected", "blocked") else None,
            now if raw_reconcile is not None else None,
            event_type,
            event_key,
            outcome_id,
            label,
            side,
            action,
            clob_order_id,
            order_type,
            status,
            requested_size_usd,
            requested_shares,
            limit_price,
            fill_price,
            fill_size_usd,
            fill_shares,
            reason,
            error,
            json.dumps(raw_request or {}),
            json.dumps(raw_response or {}),
            json.dumps(raw_reconcile or {}),
        ),
    )
    db.commit()
    return int(cursor.lastrowid)


def update_live_order_reconcile(
    db: sqlite3.Connection,
    order_id: int,
    *,
    status: str,
    fill_price: float | None,
    fill_size_usd: float,
    fill_shares: float,
    raw_reconcile: dict | None = None,
    error: str | None = None,
) -> None:
    db.execute(
        """
        update live_orders
        set reconciled_at_utc = ?,
            status = ?,
            fill_price = ?,
            fill_size_usd = ?,
            fill_shares = ?,
            raw_reconcile_json = ?,
            error = coalesce(?, error)
        where id = ?
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            status,
            fill_price,
            fill_size_usd,
            fill_shares,
            json.dumps(raw_reconcile or {}),
            error,
            order_id,
        ),
    )
    db.commit()


def list_live_orders_by_status(
    db: sqlite3.Connection, statuses: tuple[str, ...]
) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in statuses)
    return list(
        db.execute(
            f"""
            select *
            from live_orders
            where status in ({placeholders})
            order by id asc
            """,
            statuses,
        )
    )


def list_live_orders_for_reconcile(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        db.execute(
            """
            select *
            from live_orders
            where status in ('submitted', 'unknown_fill')
               or (
                   status = 'filled'
                   and (
                       coalesce(fill_shares, 0) <= 0
                       or coalesce(fill_size_usd, 0) <= 0
                   )
               )
            order by id asc
            """
        )
    )


def get_live_position(db: sqlite3.Connection, token_id: str) -> sqlite3.Row | None:
    return db.execute(
        "select * from live_positions where outcome_id = ?", (token_id,)
    ).fetchone()


def list_open_live_positions(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        db.execute(
            """
            select * from live_positions
            where net_shares > 0
            order by updated_at_utc
            """
        )
    )


def upsert_live_position(
    db: sqlite3.Connection,
    token_id: str,
    shares: float,
    avg_price: float,
    realized_pnl: float,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """
        insert into live_positions
        (outcome_id, net_shares, avg_price, realized_pnl, updated_at_utc, last_reconciled_at_utc)
        values (?, ?, ?, ?, ?, ?)
        on conflict(outcome_id) do update set
            net_shares = excluded.net_shares,
            avg_price = excluded.avg_price,
            realized_pnl = excluded.realized_pnl,
            updated_at_utc = excluded.updated_at_utc,
            last_reconciled_at_utc = excluded.last_reconciled_at_utc
        """,
        (token_id, shares, avg_price, realized_pnl, now, now),
    )
    db.commit()


def live_total_open_exposure(db: sqlite3.Connection) -> float:
    value = db.execute(
        """
        select coalesce(sum(net_shares * avg_price), 0)
        from live_positions
        where net_shares > 0
        """
    ).fetchone()[0]
    return float(value or 0.0)


def live_realized_pnl_since(db: sqlite3.Connection, start_utc: str) -> float:
    value = db.execute(
        """
        select coalesce(sum(realized_pnl), 0)
        from live_positions
        where updated_at_utc >= ?
        """,
        (start_utc,),
    ).fetchone()[0]
    return float(value or 0.0)


def set_live_setting(db: sqlite3.Connection, name: str, value: str | bool) -> None:
    db.execute(
        """
        insert into live_settings (name, value, updated_at_utc)
        values (?, ?, ?)
        on conflict(name) do update set
            value = excluded.value,
            updated_at_utc = excluded.updated_at_utc
        """,
        (
            name,
            "1" if value is True else "0" if value is False else str(value),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    db.commit()


def get_live_setting(db: sqlite3.Connection, name: str, default: str = "0") -> str:
    row = db.execute(
        "select value from live_settings where name = ?", (name,)
    ).fetchone()
    return str(row["value"]) if row else default


def live_setting_enabled(db: sqlite3.Connection, name: str) -> bool:
    return get_live_setting(db, name).lower() in ("1", "true", "yes", "on")


def store_signal(
    db: sqlite3.Connection,
    market_id: str,
    trigger_type: str,
    current_max_c: float | None,
    forecast_max_c: float | None,
    affected_outcomes: dict,
    price_response: dict,
    notes: str,
) -> None:
    db.execute(
        """
        insert into signals
        (created_at_utc, market_id, trigger_type, current_max_c, forecast_max_c,
         affected_outcomes_json, directional_impacts_json, price_response_json, notes)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            market_id,
            trigger_type,
            current_max_c,
            forecast_max_c,
            json.dumps(affected_outcomes),
            json.dumps(affected_outcomes),
            json.dumps(price_response),
            notes,
        ),
    )
    db.commit()


def store_trading_decision(
    db: sqlite3.Connection,
    event_type: str,
    outcome_id: str | None,
    label: str | None,
    side: str | None,
    action: str,
    status: str,
    reason: str,
    details: dict | None = None,
    event_key: str | None = None,
) -> None:
    decision_details = dict(details or {})
    _add_orderbook_age_to_details(db, outcome_id, decision_details)
    db.execute(
        """
        insert into paper_decisions
        (created_at_utc, event_type, event_key, outcome_id, label, side, action, status, reason, details_json)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            event_type,
            event_key,
            outcome_id,
            label,
            side,
            action,
            status,
            reason,
            json.dumps(decision_details),
        ),
    )
    db.commit()


def _add_orderbook_age_to_details(
    db: sqlite3.Connection, outcome_id: str | None, details: dict
) -> None:
    if outcome_id is None or "orderbook_state_age_seconds" in details:
        return
    row = db.execute(
        """
        select fetched_at_utc
        from orderbook_snapshots
        where outcome_id = ?
        order by fetched_at_utc desc, id desc
        limit 1
        """,
        (outcome_id,),
    ).fetchone()
    if row is None or row["fetched_at_utc"] is None:
        return
    now_text = details.get("decision_now_utc")
    try:
        fetched_at = datetime.fromisoformat(row["fetched_at_utc"])
        now = (
            datetime.fromisoformat(now_text)
            if isinstance(now_text, str)
            else datetime.now(timezone.utc)
        )
    except ValueError:
        return
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age = max(0.0, (now - fetched_at).total_seconds())
    details["orderbook_state_age_seconds"] = age


def store_paper_decision(
    db: sqlite3.Connection,
    event_type: str,
    outcome_id: str | None,
    label: str | None,
    side: str | None,
    action: str,
    status: str,
    reason: str,
    details: dict | None = None,
    event_key: str | None = None,
) -> None:
    store_trading_decision(
        db,
        event_type,
        outcome_id,
        label,
        side,
        action,
        status,
        reason,
        details,
        event_key,
    )


def has_processed_event(db: sqlite3.Connection, event_key: str) -> bool:
    return (
        db.execute(
            """
            select 1 from paper_decisions
            where event_key = ?
              and action = 'EVENT'
              and status = 'processed'
            limit 1
            """,
            (event_key,),
        ).fetchone()
        is not None
    )


def latest_two_forecast_highs(
    db: sqlite3.Connection, forecast_date_hkt: str | None = None
) -> list[sqlite3.Row]:
    params: tuple[str, ...] = ()
    date_filter = ""
    if forecast_date_hkt is not None:
        date_filter = "and forecast_date_hkt = ?"
        params = (forecast_date_hkt,)
    return list(
        db.execute(
            f"""
            select forecast_date_hkt, forecast_max_c, update_time, parse_warning, max(id) as id
            from hko_forecasts
            where source_type = 'ocf_station'
              and forecast_max_c is not null
              and coalesce(parse_warning, 0) = 0
              {date_filter}
            group by forecast_date_hkt, forecast_max_c, update_time
            order by id desc
            limit 2
            """,
            params,
        )
    )


def latest_forecast_high(
    db: sqlite3.Connection, forecast_date_hkt: str
) -> sqlite3.Row | None:
    return db.execute(
        """
        select forecast_date_hkt, forecast_max_c, update_time, parse_warning, id
        from hko_forecasts
        where source_type = 'ocf_station'
          and forecast_date_hkt = ?
          and forecast_max_c is not null
          and coalesce(parse_warning, 0) = 0
        order by id desc
        limit 1
        """,
        (forecast_date_hkt,),
    ).fetchone()


def latest_two_observed_maxes(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        db.execute(
            """
            select observed_at_hkt, since_midnight_max_c, station, max(id) as id
            from hko_current_observations
            where since_midnight_max_c is not null
            group by observed_at_hkt, since_midnight_max_c, station
            order by id desc
            limit 2
            """
        )
    )


def observed_max_increases(
    db: sqlite3.Connection, target_date_hkt: str | None = None
) -> list[tuple[sqlite3.Row, sqlite3.Row]]:
    params: tuple[str, ...] = ()
    date_filter = ""
    if target_date_hkt is not None:
        date_filter = "and substr(observed_at_hkt, 1, 10) = ?"
        params = (target_date_hkt,)
    rows = list(
        db.execute(
            f"""
            select observed_at_hkt, since_midnight_max_c, station, max(id) as id
            from hko_current_observations
            where since_midnight_max_c is not null
              {date_filter}
            group by observed_at_hkt, since_midnight_max_c, station
            order by id asc
            """,
            params,
        )
    )
    transitions = []
    for old, new in zip(rows, rows[1:]):
        if float(new["since_midnight_max_c"]) > float(old["since_midnight_max_c"]):
            transitions.append((old, new))
    return transitions


def observed_min_decreases(
    db: sqlite3.Connection, target_date_hkt: str | None = None
) -> list[tuple[sqlite3.Row, sqlite3.Row]]:
    params: tuple[str, ...] = ()
    date_filter = ""
    if target_date_hkt is not None:
        date_filter = "and substr(observed_at_hkt, 1, 10) = ?"
        params = (target_date_hkt,)
    rows = list(
        db.execute(
            f"""
            select observed_at_hkt, since_midnight_min_c, station, max(id) as id
            from hko_current_observations
            where since_midnight_min_c is not null
              {date_filter}
            group by observed_at_hkt, since_midnight_min_c, station
            order by id asc
            """,
            params,
        )
    )
    transitions = []
    for old, new in zip(rows, rows[1:]):
        if float(new["since_midnight_min_c"]) < float(old["since_midnight_min_c"]):
            transitions.append((old, new))
    return transitions


def latest_observed_max_for_date(
    db: sqlite3.Connection, target_date_hkt: str
) -> sqlite3.Row | None:
    return db.execute(
        """
        select observed_at_hkt, since_midnight_max_c, station, max(id) as id
        from hko_current_observations
        where since_midnight_max_c is not null
          and substr(observed_at_hkt, 1, 10) = ?
        group by observed_at_hkt, since_midnight_max_c, station
        order by id desc
        limit 1
        """,
        (target_date_hkt,),
    ).fetchone()


def latest_observed_min_for_date(
    db: sqlite3.Connection, target_date_hkt: str
) -> sqlite3.Row | None:
    return db.execute(
        """
        select observed_at_hkt, since_midnight_min_c, station, max(id) as id
        from hko_current_observations
        where since_midnight_min_c is not null
          and substr(observed_at_hkt, 1, 10) = ?
        group by observed_at_hkt, since_midnight_min_c, station
        order by id desc
        limit 1
        """,
        (target_date_hkt,),
    ).fetchone()


def latest_two_orderbook_prices(
    db: sqlite3.Connection, token_id: str
) -> list[sqlite3.Row]:
    return list(
        db.execute(
            """
            select id, fetched_at_utc, best_bid, best_ask, depth_json
            from orderbook_snapshots
            where outcome_id = ?
            order by fetched_at_utc desc, id desc
            limit 2
            """,
            (token_id,),
        )
    )


def dashboard_stats(db: sqlite3.Connection) -> dict:
    today_hkt = datetime.now(HKT).date().isoformat()
    row = db.execute(
        """
        select forecast_date_hkt, forecast_max_c, update_time, parse_warning
        from hko_forecasts
        where source_type = 'ocf_station'
          and forecast_date_hkt = ?
        order by id desc
        limit 1
        """,
        (today_hkt,),
    ).fetchone()
    obs = db.execute(
        """
        select observed_at_hkt, since_midnight_max_c
        from hko_current_observations
        order by id desc
        limit 1
        """
    ).fetchone()
    counts = {
        "hko_forecasts": db.execute(
            """
            select count(*) from (
                select distinct forecast_date_hkt, forecast_max_c, update_time
                from hko_forecasts
                where source_type in ('ocf_station', 'flw_page')
            )
            """
        ).fetchone()[0],
        "markets": db.execute("select count(*) from markets").fetchone()[0],
        "outcomes": db.execute("select count(*) from outcomes").fetchone()[0],
        "orderbooks": db.execute("select count(*) from orderbook_snapshots").fetchone()[0],
        "buy_filled": db.execute(
            "select count(*) from paper_orders where side like 'BUY_%' and status = 'filled'"
        ).fetchone()[0],
        "buy_missed": db.execute(
            "select count(*) from paper_decisions where action = 'BUY' and status = 'missed'"
        ).fetchone()[0],
        "sell_filled": db.execute(
            "select count(*) from paper_orders where side = 'SELL' and status = 'filled'"
        ).fetchone()[0],
        "sell_missed": db.execute(
            "select count(*) from paper_decisions where action = 'SELL' and status = 'missed'"
        ).fetchone()[0],
    }
    realized_pnl = db.execute(
        "select coalesce(sum(realized_pnl), 0) from paper_positions"
    ).fetchone()[0]
    executable_unrealized = 0.0
    worst_case_open_loss = 0.0
    for pos in list_open_paper_positions(db):
        shares = float(pos["net_shares"])
        avg_price = float(pos["avg_price"])
        worst_case_open_loss += shares * avg_price
        bid_row = db.execute(
            """
            select best_bid
            from orderbook_snapshots
            where outcome_id = ?
            order by fetched_at_utc desc, id desc
            limit 1
            """,
            (pos["outcome_id"],),
        ).fetchone()
        bid = float(bid_row["best_bid"]) if bid_row and bid_row["best_bid"] is not None else 0.0
        executable_unrealized += shares * (bid - avg_price)
    open_positions = db.execute(
        "select count(*) from paper_positions where net_shares > 0"
    ).fetchone()[0]
    realized = float(realized_pnl or 0)
    return {
        "latest_forecast": dict(row) if row else None,
        "latest_observation": dict(obs) if obs else None,
        "counts": counts,
        "open_positions": open_positions,
        "realized_pnl": realized,
        "executable_unrealized_pnl": executable_unrealized,
        "total_profit": realized + executable_unrealized,
        "worst_case_open_loss": worst_case_open_loss,
    }


def live_dashboard_stats(db: sqlite3.Connection) -> dict:
    open_position_rows = list_open_live_positions(db)
    open_exposure = live_total_open_exposure(db)
    realized_pnl = db.execute(
        "select coalesce(sum(realized_pnl), 0) from live_positions"
    ).fetchone()[0]
    counts = {
        "orders": db.execute("select count(*) from live_orders").fetchone()[0],
        "filled": db.execute(
            "select count(*) from live_orders where status = 'filled'"
        ).fetchone()[0],
        "submitted": db.execute(
            "select count(*) from live_orders where status = 'submitted'"
        ).fetchone()[0],
        "rejected": db.execute(
            "select count(*) from live_orders where status = 'rejected'"
        ).fetchone()[0],
        "blocked": db.execute(
            "select count(*) from live_orders where status = 'blocked'"
        ).fetchone()[0],
        "error": db.execute(
            "select count(*) from live_orders where status = 'error'"
        ).fetchone()[0],
    }
    positions = []
    executable_unrealized_pnl = 0.0
    missing_bid_count = 0
    for pos in open_position_rows:
        outcome = find_outcome_by_token(db, pos["outcome_id"])
        bid_row = db.execute(
            """
            select best_bid
            from orderbook_snapshots
            where outcome_id = ?
            order by fetched_at_utc desc, id desc
            limit 1
            """,
            (pos["outcome_id"],),
        ).fetchone()
        latest_bid = (
            float(bid_row["best_bid"])
            if bid_row and bid_row["best_bid"] is not None
            else None
        )
        shares = float(pos["net_shares"])
        avg_price = float(pos["avg_price"])
        cost_basis = shares * avg_price
        current_value = shares * latest_bid if latest_bid is not None else None
        unrealized = (
            shares * (latest_bid - avg_price) if latest_bid is not None else None
        )
        if unrealized is None:
            missing_bid_count += 1
        else:
            executable_unrealized_pnl += unrealized
        token_side = None
        if outcome is not None:
            token_side = "YES" if pos["outcome_id"] == outcome["yes_token_id"] else "NO"
        positions.append(
            {
                **dict(pos),
                "label": outcome["label"] if outcome is not None else None,
                "side": token_side,
                "target_date_hkt": outcome["target_date_hkt"] if outcome is not None else None,
                "slug": outcome["slug"] if outcome is not None else None,
                "latest_bid": latest_bid,
                "cost_basis_usd": cost_basis,
                "current_value_usd": current_value,
                "executable_unrealized_pnl": unrealized,
            }
        )
    recent_orders = [
        dict(row)
        for row in db.execute(
            """
            select created_at_utc, outcome_id, label, side, action, clob_order_id,
                   order_type, status, requested_size_usd, requested_shares,
                   limit_price, fill_price, fill_size_usd, fill_shares, reason, error
            from live_orders
            order by id desc
            limit 25
            """
        )
    ]
    return {
        "mode": "live",
        "counts": counts,
        "open_positions": len(open_position_rows),
        "open_exposure_usd": open_exposure,
        "realized_pnl": float(realized_pnl or 0.0),
        "executable_unrealized_pnl": executable_unrealized_pnl,
        "total_pnl": float(realized_pnl or 0.0) + executable_unrealized_pnl,
        "missing_bid_positions": missing_bid_count,
        "caps": {
            "manual_order_usd": Settings.live_manual_order_cap_usd,
            "scheduler_order_usd": Settings.live_scheduler_order_cap_usd,
            "open_exposure_usd": Settings.live_total_open_exposure_cap_usd,
            "daily_realized_loss_usd": Settings.live_daily_realized_loss_cap_usd,
        },
        "block_new_entries": live_setting_enabled(db, "block_new_entries"),
        "cancel_open_orders_and_exit_positions": live_setting_enabled(
            db, "cancel_open_orders_and_exit_positions"
        ),
        "positions": positions,
        "recent_orders": recent_orders,
    }


def reset_paper_state(db: sqlite3.Connection) -> None:
    db.execute("delete from paper_orders")
    db.execute("delete from paper_positions")
    db.execute("delete from paper_decisions")
    db.execute("delete from signals")
    db.commit()
