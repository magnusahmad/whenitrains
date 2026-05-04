from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time as day_time, timezone
from pathlib import Path

from .hko import HKT, HkoForecast, HkoObservation, OcfForecastSample
from .polymarket import OrderBook, TemperatureMarket


@dataclass(frozen=True)
class RawSnapshotRecord:
    id: int
    content_hash: str


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


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
            http_etag text
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
        """
    )
    _add_column_if_missing(db, "raw_snapshots", "response_headers_json", "text")
    _add_column_if_missing(db, "raw_snapshots", "http_date", "text")
    _add_column_if_missing(db, "raw_snapshots", "http_last_modified", "text")
    _add_column_if_missing(db, "raw_snapshots", "http_etag", "text")
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
            http_etag text
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
) -> RawSnapshotRecord:
    content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    headers = response_headers or {}
    cursor = db.execute(
        """
        insert into raw_snapshots
        (source, endpoint, fetched_at_utc, content_hash, payload,
         response_headers_json, http_date, http_last_modified, http_etag)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    db: sqlite3.Connection, snapshot_id: int, samples: list[OcfForecastSample]
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for sample in samples:
        db.execute(
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
    db.commit()


def store_polymarket_event(db: sqlite3.Connection, market: TemperatureMarket) -> None:
    existing = db.execute(
        "select id from markets where slug = ? order by id desc limit 1",
        (market.event_slug,),
    ).fetchone()
    if existing is not None:
        local_market_id = int(existing["id"])
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
                "active",
                market.resolution_rules_text,
                json.dumps(market.raw_event or {}),
                local_market_id,
            ),
        )
        db.commit()
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
                "active",
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


def store_orderbook(db: sqlite3.Connection, outcome_id: str, book: OrderBook) -> None:
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
            json.dumps({"bids": book.bids, "asks": book.asks}),
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
    return OrderBook(token_id=token_id, bids=bids, asks=asks, tick_size=0.01, min_order_size=5)


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
                   m.target_date_hkt, m.slug
            from outcomes o
            join markets m on m.id = o.market_id
            where m.target_date_hkt = ?
            order by o.predicate_value_c, o.label
            """,
            (target_date_hkt,),
        )
    )


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


def find_outcome_by_token(db: sqlite3.Connection, token_id: str) -> sqlite3.Row | None:
    return db.execute(
        """
        select o.id, o.market_id, o.polymarket_market_id, o.label,
               o.predicate_type, o.predicate_value_c, o.yes_token_id, o.no_token_id,
               m.target_date_hkt, m.slug
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
            json.dumps(details or {}),
        ),
    )
    db.commit()


def has_processed_event(db: sqlite3.Connection, event_key: str) -> bool:
    return (
        db.execute(
            """
            select 1 from paper_decisions
            where event_key = ?
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
            where source_type in ('ocf_station', 'flw_page')
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


def latest_two_observed_maxes(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        db.execute(
            """
            select observed_at_hkt, since_midnight_max_c, max(id) as id
            from hko_current_observations
            where since_midnight_max_c is not null
            group by observed_at_hkt, since_midnight_max_c
            order by id desc
            limit 2
            """
        )
    )


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
        where source_type in ('ocf_station', 'flw_page')
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
            where outcome_id = ? and best_bid is not null
            order by fetched_at_utc desc, id desc
            limit 1
            """,
            (pos["outcome_id"],),
        ).fetchone()
        bid = float(bid_row["best_bid"]) if bid_row else 0.0
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


def reset_paper_state(db: sqlite3.Connection) -> None:
    db.execute("delete from paper_orders")
    db.execute("delete from paper_positions")
    db.execute("delete from paper_decisions")
    db.execute("delete from signals")
    db.commit()
