from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date

from .config import ExperimentConfig


@dataclass(frozen=True)
class ExperimentSignal:
    event_key: str
    target_date_hkt: str
    outcome_id: str
    label: str
    side: str
    action: str
    reason: str
    details: dict


def build_signals(
    db: sqlite3.Connection, config: ExperimentConfig, target_date: date
) -> list[ExperimentSignal]:
    if config.strategy != "forecast_bucket_cheap_yes":
        raise ValueError(f"unknown experiment strategy: {config.strategy}")
    signal = _forecast_bucket_cheap_yes(db, config, target_date)
    return [] if signal is None else [signal]


def _forecast_bucket_cheap_yes(
    db: sqlite3.Connection, config: ExperimentConfig, target_date: date
) -> ExperimentSignal | None:
    target = target_date.isoformat()
    forecast = db.execute(
        """
        select id, forecast_max_c, update_time
        from hko_forecasts
        where source_type = 'ocf_station'
          and forecast_date_hkt = ?
          and forecast_max_c is not null
          and coalesce(parse_warning, 0) = 0
        order by id desc
        limit 1
        """,
        (target,),
    ).fetchone()
    if forecast is None:
        return None
    forecast_bucket = math.floor(float(forecast["forecast_max_c"]))
    outcome = _outcome_for_bucket(db, target, forecast_bucket)
    if outcome is None:
        return None
    book = db.execute(
        """
        select best_ask
        from orderbook_snapshots
        where outcome_id = ?
          and best_ask is not null
        order by fetched_at_utc desc, id desc
        limit 1
        """,
        (outcome["yes_token_id"],),
    ).fetchone()
    if book is None:
        return None
    ask = float(book["best_ask"])
    label = outcome["label"]
    details = {
        "forecast_id": forecast["id"],
        "forecast_max_c": float(forecast["forecast_max_c"]),
        "forecast_bucket": forecast_bucket,
        "forecast_update_time": forecast["update_time"],
        "ask": ask,
        "threshold": config.execution.max_entry_price,
    }
    event_key = f"{config.strategy}:{target}:{forecast['id']}:{label}:YES"
    if ask > config.execution.max_entry_price:
        return ExperimentSignal(
            event_key=event_key,
            target_date_hkt=target,
            outcome_id=outcome["yes_token_id"],
            label=label,
            side="YES",
            action="SKIP",
            reason="forecast bucket not cheap",
            details=details,
        )
    return ExperimentSignal(
        event_key=event_key,
        target_date_hkt=target,
        outcome_id=outcome["yes_token_id"],
        label=label,
        side="YES",
        action="BUY",
        reason="forecast bucket ask below threshold",
        details=details,
    )


def _outcome_for_bucket(
    db: sqlite3.Connection, target_date_hkt: str, forecast_bucket: int
) -> sqlite3.Row | None:
    rows = list(
        db.execute(
            """
            select o.*
            from outcomes o
            join markets m on m.id = o.market_id
            where m.target_date_hkt = ?
            order by o.predicate_value_c asc, o.id asc
            """,
            (target_date_hkt,),
        )
    )
    exact = [
        row
        for row in rows
        if row["predicate_type"] == "EXACT_C"
        and int(float(row["predicate_value_c"])) == forecast_bucket
    ]
    if exact:
        return exact[-1]
    gte = [
        row
        for row in rows
        if row["predicate_type"] == "GTE_C"
        and forecast_bucket >= int(float(row["predicate_value_c"]))
    ]
    return gte[-1] if gte else None

