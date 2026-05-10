from __future__ import annotations

import queue
import sqlite3
import time
from dataclasses import dataclass
from datetime import date
from typing import Callable

from .runner import RunnerResult, process_actual_entries
from .storage import record_latency_stage


@dataclass(frozen=True)
class AlphaEvent:
    kind: str
    event_key: str
    target_date_hkt: str
    source_row_id: int
    previous_row_id: int
    committed_monotonic: float
    detected_monotonic: float
    details: dict


@dataclass(frozen=True)
class FastEventResult:
    event_key: str
    result: object


class LowLatencyEventQueue:
    def __init__(self) -> None:
        self._queue: queue.Queue[AlphaEvent] = queue.Queue()
        self._seen_keys: set[str] = set()

    def put(self, event: AlphaEvent) -> bool:
        if event.event_key in self._seen_keys:
            return False
        self._seen_keys.add(event.event_key)
        self._queue.put(event)
        return True

    def get_nowait(self) -> AlphaEvent:
        return self._queue.get_nowait()

    def get(self, timeout: float | None = None) -> AlphaEvent:
        return self._queue.get(timeout=timeout)

    def empty(self) -> bool:
        return self._queue.empty()


def enqueue_hko_actual_transition_events(
    db: sqlite3.Connection,
    event_queue: LowLatencyEventQueue,
    *,
    observation_id: int,
    committed_monotonic: float | None = None,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> list[AlphaEvent]:
    committed = monotonic_fn() if committed_monotonic is None else committed_monotonic
    new = db.execute(
        """
        select id, observed_at_hkt, station, since_midnight_max_c, since_midnight_min_c
        from hko_current_observations
        where id = ?
        """,
        (observation_id,),
    ).fetchone()
    if new is None or new["observed_at_hkt"] is None:
        return []
    previous = db.execute(
        """
        select id, observed_at_hkt, station, since_midnight_max_c, since_midnight_min_c
        from hko_current_observations
        where id < ?
          and station = ?
          and substr(observed_at_hkt, 1, 10) = substr(?, 1, 10)
        order by id desc
        limit 1
        """,
        (observation_id, new["station"], new["observed_at_hkt"]),
    ).fetchone()
    if previous is None:
        return []

    detected = monotonic_fn()
    events: list[AlphaEvent] = []
    if (
        _float_or_none(new["since_midnight_max_c"]) is not None
        and _float_or_none(previous["since_midnight_max_c"]) is not None
    ):
        old_max = float(previous["since_midnight_max_c"])
        new_max = float(new["since_midnight_max_c"])
        if new_max > old_max:
            events.append(
                _actual_event(
                    kind="aws_actual_transition",
                    event_name="max",
                    new=new,
                    previous=previous,
                    committed=committed,
                    detected=detected,
                    old_value=old_max,
                    new_value=new_max,
                )
            )
    if (
        _float_or_none(new["since_midnight_min_c"]) is not None
        and _float_or_none(previous["since_midnight_min_c"]) is not None
    ):
        old_min = float(previous["since_midnight_min_c"])
        new_min = float(new["since_midnight_min_c"])
        if new_min < old_min:
            events.append(
                _actual_event(
                    kind="aws_actual_transition",
                    event_name="min",
                    new=new,
                    previous=previous,
                    committed=committed,
                    detected=detected,
                    old_value=old_min,
                    new_value=new_min,
                )
            )

    for event in events:
        record_latency_stage(
            db,
            event.event_key,
            "db_committed",
            event.committed_monotonic,
            event.kind,
            {"source_row_id": event.source_row_id},
        )
        record_latency_stage(
            db,
            event.event_key,
            "event_detected",
            event.detected_monotonic,
            event.kind,
            event.details,
        )
        event_queue.put(event)
    return events


def process_next_fast_event(
    db: sqlite3.Connection,
    event_queue: LowLatencyEventQueue,
    *,
    decision_handler: Callable[[sqlite3.Connection, date], object] = process_actual_entries,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> FastEventResult:
    event = event_queue.get_nowait()
    target = date.fromisoformat(event.target_date_hkt)
    record_latency_stage(
        db,
        event.event_key,
        "decision_started",
        monotonic_fn(),
        event.kind,
        event.details,
    )
    result = decision_handler(db, target)
    record_latency_stage(
        db,
        event.event_key,
        "decision_completed",
        monotonic_fn(),
        event.kind,
        _result_details(result),
    )
    return FastEventResult(event_key=event.event_key, result=result)


def _actual_event(
    *,
    kind: str,
    event_name: str,
    new: sqlite3.Row,
    previous: sqlite3.Row,
    committed: float,
    detected: float,
    old_value: float,
    new_value: float,
) -> AlphaEvent:
    target_date = str(new["observed_at_hkt"])[:10]
    event_key = (
        f"{kind}:{event_name}:{target_date}:"
        f"{previous['id']}:{old_value}->{new['id']}:{new_value}"
    )
    return AlphaEvent(
        kind=kind,
        event_key=event_key,
        target_date_hkt=target_date,
        source_row_id=int(new["id"]),
        previous_row_id=int(previous["id"]),
        committed_monotonic=committed,
        detected_monotonic=detected,
        details={
            "transition": event_name,
            "old_value": old_value,
            "new_value": new_value,
            "previous_row_id": int(previous["id"]),
            "source_row_id": int(new["id"]),
            "observed_at_hkt": new["observed_at_hkt"],
        },
    )


def _float_or_none(value) -> float | None:
    return None if value is None else float(value)


def _result_details(result: object) -> dict:
    if isinstance(result, RunnerResult):
        return {
            "buys_filled": result.buys_filled,
            "buys_missed": result.buys_missed,
            "sells_filled": result.sells_filled,
            "sells_missed": result.sells_missed,
            "signals": result.signals,
            "notes": list(result.notes),
        }
    return {}
