from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from .config import ExperimentConfig


def create_experiment_run(
    db: sqlite3.Connection,
    config: ExperimentConfig,
    *,
    source_db: Path | None = None,
    target_start: date | None = None,
    target_end: date | None = None,
) -> int:
    cursor = db.execute(
        """
        insert into experiment_runs
        (name, strategy, config_json, source_db, target_start_hkt, target_end_hkt, created_at_utc)
        values (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            config.name,
            config.strategy,
            config.to_json(),
            str(source_db) if source_db is not None else None,
            target_start.isoformat() if target_start else None,
            target_end.isoformat() if target_end else None,
            utc_now(),
        ),
    )
    db.commit()
    return int(cursor.lastrowid)


def record_experiment_decision(
    db: sqlite3.Connection,
    *,
    run_id: int,
    event_key: str,
    target_date_hkt: str,
    outcome_id: str | None,
    label: str | None,
    side: str | None,
    action: str,
    status: str,
    reason: str,
    details: dict,
    created_at_utc: str | None = None,
) -> int | None:
    try:
        cursor = db.execute(
            """
            insert into experiment_decisions
            (run_id, created_at_utc, event_key, target_date_hkt, outcome_id, label,
             side, action, status, reason, details_json)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                created_at_utc or utc_now(),
                event_key,
                target_date_hkt,
                outcome_id,
                label,
                side,
                action,
                status,
                reason,
                json.dumps(details, sort_keys=True),
            ),
        )
        db.commit()
        return int(cursor.lastrowid)
    except sqlite3.IntegrityError:
        return None


def record_experiment_order(
    db: sqlite3.Connection,
    *,
    run_id: int,
    decision_id: int | None,
    outcome_id: str,
    label: str,
    side: str,
    action: str,
    status: str,
    limit_price: float | None,
    requested_size_usd: float,
    fill_price: float | None,
    fill_size_usd: float,
    fill_shares: float,
    reason: str,
    details: dict | None = None,
    created_at_utc: str | None = None,
) -> int:
    cursor = db.execute(
        """
        insert into experiment_orders
        (run_id, created_at_utc, decision_id, outcome_id, label, side, action, status,
         limit_price, requested_size_usd, fill_price, fill_size_usd, fill_shares,
         reason, details_json)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            created_at_utc or utc_now(),
            decision_id,
            outcome_id,
            label,
            side,
            action,
            status,
            limit_price,
            requested_size_usd,
            fill_price,
            fill_size_usd,
            fill_shares,
            reason,
            json.dumps(details or {}, sort_keys=True),
        ),
    )
    db.commit()
    return int(cursor.lastrowid)


def get_experiment_position(
    db: sqlite3.Connection, run_id: int, outcome_id: str
) -> sqlite3.Row | None:
    return db.execute(
        """
        select *
        from experiment_positions
        where run_id = ? and outcome_id = ?
        """,
        (run_id, outcome_id),
    ).fetchone()


def upsert_experiment_position(
    db: sqlite3.Connection,
    *,
    run_id: int,
    outcome_id: str,
    label: str,
    side: str,
    net_shares: float,
    avg_price: float,
    realized_pnl: float,
    updated_at_utc: str | None = None,
) -> None:
    db.execute(
        """
        insert into experiment_positions
        (run_id, outcome_id, label, side, net_shares, avg_price, realized_pnl, updated_at_utc)
        values (?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(run_id, outcome_id) do update set
            label = excluded.label,
            side = excluded.side,
            net_shares = excluded.net_shares,
            avg_price = excluded.avg_price,
            realized_pnl = excluded.realized_pnl,
            updated_at_utc = excluded.updated_at_utc
        """,
        (
            run_id,
            outcome_id,
            label,
            side,
            net_shares,
            avg_price,
            realized_pnl,
            updated_at_utc or utc_now(),
        ),
    )
    db.commit()


def reset_experiment_tables(db: sqlite3.Connection) -> None:
    for table in (
        "experiment_metrics",
        "experiment_orders",
        "experiment_positions",
        "experiment_decisions",
        "experiment_runs",
    ):
        db.execute(f"delete from {table}")
    db.commit()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

