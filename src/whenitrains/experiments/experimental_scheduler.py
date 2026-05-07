from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone

from ..paper_db import calculate_entry
from ..storage import latest_orderbook
from .config import ExperimentConfig
from .store import (
    get_experiment_position,
    record_experiment_decision,
    record_experiment_order,
    upsert_experiment_position,
)
from .strategy import ExperimentSignal, build_signals


@dataclass(frozen=True)
class ExperimentTickResult:
    decisions: int = 0
    orders_filled: int = 0
    orders_rejected: int = 0
    notes: tuple[str, ...] = ()


def run_experiment_tick(
    db: sqlite3.Connection,
    *,
    run_id: int,
    config: ExperimentConfig,
    target_date: date,
    tick_at: datetime | None = None,
) -> ExperimentTickResult:
    timestamp = _utc_iso(tick_at)
    notes: list[str] = []
    decisions = 0
    filled = 0
    rejected = 0
    for signal in build_signals(db, config, target_date):
        decision_id = record_experiment_decision(
            db,
            run_id=run_id,
            event_key=signal.event_key,
            target_date_hkt=signal.target_date_hkt,
            outcome_id=signal.outcome_id,
            label=signal.label,
            side=signal.side,
            action=signal.action,
            status="candidate" if signal.action == "BUY" else "skipped",
            reason=signal.reason,
            details=signal.details,
            created_at_utc=timestamp,
        )
        if decision_id is None:
            notes.append(f"duplicate {signal.event_key}")
            continue
        decisions += 1
        if signal.action != "BUY":
            notes.append(f"skipped {signal.label} {signal.side}: {signal.reason}")
            continue
        result = _execute_experiment_buy(
            db,
            run_id=run_id,
            decision_id=decision_id,
            signal=signal,
            config=config,
            created_at_utc=timestamp,
        )
        if result:
            filled += 1
        else:
            rejected += 1
    return ExperimentTickResult(
        decisions=decisions,
        orders_filled=filled,
        orders_rejected=rejected,
        notes=tuple(notes),
    )


def _execute_experiment_buy(
    db: sqlite3.Connection,
    *,
    run_id: int,
    decision_id: int,
    signal: ExperimentSignal,
    config: ExperimentConfig,
    created_at_utc: str,
) -> bool:
    book = latest_orderbook(db, signal.outcome_id)
    quote = calculate_entry(
        signal.outcome_id,
        config.execution.order_size_usd,
        book.asks,
        max_order_usd=config.execution.max_order_usd,
        max_price=config.execution.max_entry_price,
        min_fill_usd=config.execution.min_fill_usd,
    )
    if quote.status != "fillable":
        record_experiment_order(
            db,
            run_id=run_id,
            decision_id=decision_id,
            outcome_id=signal.outcome_id,
            label=signal.label,
            side=signal.side,
            action="BUY",
            status="rejected",
            limit_price=quote.limit_price,
            requested_size_usd=config.execution.order_size_usd,
            fill_price=None,
            fill_size_usd=0.0,
            fill_shares=0.0,
            reason=quote.reason,
            details=signal.details,
            created_at_utc=created_at_utc,
        )
        return False

    pos = get_experiment_position(db, run_id, signal.outcome_id)
    old_shares = float(pos["net_shares"]) if pos else 0.0
    old_avg = float(pos["avg_price"]) if pos else 0.0
    old_realized = float(pos["realized_pnl"]) if pos else 0.0
    new_shares = old_shares + quote.estimated_shares
    new_avg = (
        old_avg * old_shares + quote.estimated_cost_usd
    ) / new_shares
    upsert_experiment_position(
        db,
        run_id=run_id,
        outcome_id=signal.outcome_id,
        label=signal.label,
        side=signal.side,
        net_shares=new_shares,
        avg_price=new_avg,
        realized_pnl=old_realized,
        updated_at_utc=created_at_utc,
    )
    record_experiment_order(
        db,
        run_id=run_id,
        decision_id=decision_id,
        outcome_id=signal.outcome_id,
        label=signal.label,
        side=signal.side,
        action="BUY",
        status="filled",
        limit_price=quote.limit_price,
        requested_size_usd=config.execution.order_size_usd,
        fill_price=quote.estimated_avg_price,
        fill_size_usd=quote.estimated_cost_usd,
        fill_shares=quote.estimated_shares,
        reason=signal.reason,
        details=signal.details,
        created_at_utc=created_at_utc,
    )
    return True


def _utc_iso(value: datetime | None) -> str:
    if value is None:
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()

