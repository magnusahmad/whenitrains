from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .hko import HkoForecast, HkoObservation
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
    db.executescript(
        """
        create table if not exists raw_snapshots (
            id integer primary key autoincrement,
            source text not null,
            endpoint text not null,
            fetched_at_utc text not null,
            content_hash text not null unique,
            payload text not null
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
            raw_forecast text
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
        """
    )
    db.commit()


def store_raw_snapshot(
    db: sqlite3.Connection, source: str, endpoint: str, payload: str
) -> RawSnapshotRecord:
    content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """
        insert or ignore into raw_snapshots
        (source, endpoint, fetched_at_utc, content_hash, payload)
        values (?, ?, ?, ?, ?)
        """,
        (source, endpoint, now, content_hash, payload),
    )
    db.commit()
    row = db.execute(
        "select id, content_hash from raw_snapshots where content_hash = ?",
        (content_hash,),
    ).fetchone()
    return RawSnapshotRecord(id=int(row["id"]), content_hash=str(row["content_hash"]))


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
             forecast_max_c, weather_text, wind_text, psr, raw_forecast)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                json.dumps(forecast.raw or {}),
            ),
        )
    db.commit()


def store_polymarket_event(db: sqlite3.Connection, market: TemperatureMarket) -> None:
    market_row = db.execute(
        """
        insert into markets
        (polymarket_event_id, slug, question, target_date_hkt, status, raw_market)
        values (?, ?, ?, ?, ?, ?)
        """,
        (
            market.event_id,
            market.event_slug,
            market.title,
            market.target_date.isoformat() if market.target_date else None,
            "active",
            "{}",
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
