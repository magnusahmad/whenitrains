from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .. import backtest as replay
from ..storage import connect, migrate
from .config import ExperimentConfig
from .experimental_scheduler import ExperimentTickResult, run_experiment_tick
from .store import create_experiment_run, reset_experiment_tables


@dataclass(frozen=True)
class ExperimentBacktestResult:
    run_id: int
    source_db: Path
    replay_db: Path
    target_date: date
    config: ExperimentConfig
    tick_count: int
    active_ticks: list[tuple[str, ExperimentTickResult]]
    decision_count: int
    order_count: int
    filled_order_count: int
    rejected_order_count: int
    open_position_count: int
    realized_pnl: float
    cost_basis: float


def run_experiment_backtest_day(
    source_db: Path,
    target_date: date,
    config: ExperimentConfig,
    replay_db: Path | None = None,
    tick_source: str = "data",
    include_orderbook_ticks: bool = False,
    max_ticks: int | None = None,
) -> ExperimentBacktestResult:
    if replay_db is None:
        replay_db = (
            Path("/private/tmp")
            / f"whenitrains-experiment-{config.name}-{target_date.isoformat()}.sqlite3"
        )
    replay_db.parent.mkdir(parents=True, exist_ok=True)
    if replay_db.exists():
        replay_db.unlink()
    replay._copy_sqlite_database(source_db, replay_db)

    db = connect(replay_db)
    migrate(db)
    db.execute("attach database ? as src", (str(source_db),))
    try:
        replay._reset_replay_tables(db)
        reset_experiment_tables(db)
        run_id = create_experiment_run(
            db,
            config,
            source_db=source_db,
            target_start=target_date,
            target_end=target_date,
        )
        tick_times = replay._tick_times(
            db,
            target_date,
            tick_source=tick_source,
            include_orderbook_ticks=include_orderbook_ticks,
        )
        if max_ticks is not None:
            tick_times = tick_times[:max_ticks]
        source_rows = replay._load_source_rows(db)
        replay._create_replay_indexes(db)
        indexes = {table: 0 for table in replay.REPLAY_TABLES}
        active_ticks: list[tuple[str, ExperimentTickResult]] = []
        for tick_at in tick_times:
            for table in replay.REPLAY_TABLES:
                indexes[table] = replay._ingest_until(
                    db, table, source_rows[table], indexes[table], tick_at
                )
            db.commit()
            result = run_experiment_tick(
                db,
                run_id=run_id,
                config=config,
                target_date=target_date,
                tick_at=tick_at,
            )
            if result.decisions or result.orders_filled or result.orders_rejected:
                active_ticks.append((replay._format_hkt(tick_at), result))

        metrics = _load_metrics(db, run_id)
        return ExperimentBacktestResult(
            run_id=run_id,
            source_db=source_db,
            replay_db=replay_db,
            target_date=target_date,
            config=config,
            tick_count=len(tick_times),
            active_ticks=active_ticks,
            **metrics,
        )
    finally:
        db.execute("detach database src")
        db.close()


def _load_metrics(db: sqlite3.Connection, run_id: int) -> dict:
    row = db.execute(
        """
        select
            (select count(*) from experiment_decisions where run_id = ?) as decision_count,
            (select count(*) from experiment_orders where run_id = ?) as order_count,
            (select count(*) from experiment_orders where run_id = ? and status = 'filled') as filled_order_count,
            (select count(*) from experiment_orders where run_id = ? and status = 'rejected') as rejected_order_count,
            (select count(*) from experiment_positions where run_id = ? and net_shares > 0) as open_position_count,
            (select coalesce(sum(realized_pnl), 0) from experiment_positions where run_id = ?) as realized_pnl,
            (select coalesce(sum(net_shares * avg_price), 0) from experiment_positions where run_id = ? and net_shares > 0) as cost_basis
        """,
        (run_id, run_id, run_id, run_id, run_id, run_id, run_id),
    ).fetchone()
    return {
        "decision_count": int(row["decision_count"]),
        "order_count": int(row["order_count"]),
        "filled_order_count": int(row["filled_order_count"]),
        "rejected_order_count": int(row["rejected_order_count"]),
        "open_position_count": int(row["open_position_count"]),
        "realized_pnl": float(row["realized_pnl"] or 0.0),
        "cost_basis": float(row["cost_basis"] or 0.0),
    }


def experiment_result_json(result: ExperimentBacktestResult) -> dict:
    return {
        "run_id": result.run_id,
        "source_db": str(result.source_db),
        "replay_db": str(result.replay_db),
        "target_date": result.target_date.isoformat(),
        "config": json.loads(result.config.to_json()),
        "tick_count": result.tick_count,
        "active_ticks": [
            {
                "tick_at_hkt": tick_at,
                "decisions": tick.decisions,
                "orders_filled": tick.orders_filled,
                "orders_rejected": tick.orders_rejected,
                "notes": list(tick.notes),
            }
            for tick_at, tick in result.active_ticks
        ],
        "decision_count": result.decision_count,
        "order_count": result.order_count,
        "filled_order_count": result.filled_order_count,
        "rejected_order_count": result.rejected_order_count,
        "open_position_count": result.open_position_count,
        "realized_pnl": result.realized_pnl,
        "cost_basis": result.cost_basis,
    }


def dumps_experiment_result_json(result: ExperimentBacktestResult) -> str:
    return json.dumps(experiment_result_json(result), indent=2)


def render_experiment_result(result: ExperimentBacktestResult) -> str:
    lines = [
        f"Experiment: {result.config.name}",
        f"Strategy: {result.config.strategy}",
        f"Run ID: {result.run_id}",
        f"Backtest date: {result.target_date.isoformat()}",
        f"Source DB: {result.source_db}",
        f"Replay DB: {result.replay_db}",
        f"Ticks replayed: {result.tick_count}",
        f"Active ticks: {len(result.active_ticks)}",
        f"Decisions: {result.decision_count}",
        f"Orders filled/rejected: {result.filled_order_count}/{result.rejected_order_count}",
        f"Open positions: {result.open_position_count}",
        f"Realized PnL: ${result.realized_pnl:.2f}",
        f"Cost basis: ${result.cost_basis:.2f}",
        "",
        "Active Tick Summary:",
    ]
    if not result.active_ticks:
        lines.append("  none")
    for tick_at, tick in result.active_ticks:
        lines.append(
            "  "
            f"{tick_at} | decisions={tick.decisions} "
            f"filled={tick.orders_filled} rejected={tick.orders_rejected} "
            f"| {'; '.join(tick.notes)}"
        )
    return "\n".join(lines)

