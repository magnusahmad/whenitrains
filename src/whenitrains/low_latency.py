from __future__ import annotations

import queue
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

from .runner import (
    RunnerResult,
    process_actual_entries,
    process_forecast_entries,
    process_open_position_exits,
)
from .storage import connect, record_latency_stage


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
    event: AlphaEvent
    result: object


class LowLatencyEventQueue:
    def __init__(self) -> None:
        self._queue: queue.Queue[AlphaEvent] = queue.Queue()
        self._seen_keys: set[str] = set()
        self._condition = threading.Condition()

    def put(self, event: AlphaEvent) -> bool:
        with self._condition:
            if event.event_key in self._seen_keys:
                return False
            self._seen_keys.add(event.event_key)
            self._queue.put(event)
            self._condition.notify_all()
            return True

    def get_nowait(self) -> AlphaEvent:
        return self._queue.get_nowait()

    def get(self, timeout: float | None = None) -> AlphaEvent:
        return self._queue.get(timeout=timeout)

    def empty(self) -> bool:
        return self._queue.empty()

    def wait_for_event_or_stop(
        self,
        timeout: float,
        stop_event: threading.Event,
        *,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> bool:
        if not self.empty():
            return True
        deadline = monotonic_fn() + max(0.0, timeout)
        with self._condition:
            while self.empty() and not stop_event.is_set():
                remaining = deadline - monotonic_fn()
                if remaining <= 0:
                    break
                self._condition.wait(timeout=min(remaining, 0.1))
            return not self.empty()


class FastDecisionWorker:
    def __init__(
        self,
        *,
        db_path: Path,
        event_queue: LowLatencyEventQueue,
        decision_handler: Callable[[sqlite3.Connection, date], object] | None = None,
        result_callback: Callable[[FastEventResult], None] | None = None,
        poll_timeout: float = 0.5,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db_path = db_path
        self._event_queue = event_queue
        self._decision_handler = decision_handler
        self._result_callback = result_callback
        self._poll_timeout = poll_timeout
        self._monotonic_fn = monotonic_fn
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        db = connect(self._db_path)
        try:
            while not self._stop_event.is_set():
                try:
                    event = self._event_queue.get(timeout=self._poll_timeout)
                except queue.Empty:
                    continue
                result = process_fast_event(
                    db,
                    event,
                    decision_handler=self._decision_handler,
                    monotonic_fn=self._monotonic_fn,
                )
                if self._result_callback is not None:
                    self._result_callback(result)
        finally:
            db.close()


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


def enqueue_ocf_forecast_sample_events(
    db: sqlite3.Connection,
    event_queue: LowLatencyEventQueue,
    *,
    sample_ids: list[int],
    committed_monotonic: float | None = None,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> list[AlphaEvent]:
    committed = monotonic_fn() if committed_monotonic is None else committed_monotonic
    detected: float | None = None
    events: list[AlphaEvent] = []
    for sample_id in sample_ids:
        new = db.execute(
            """
            select id, forecast_date_hkt, forecast_min_c, forecast_max_c,
                   raw_min_c, raw_max_c, hourly_temperatures_json
            from ocf_forecast_samples
            where id = ?
            """,
            (sample_id,),
        ).fetchone()
        if new is None or new["forecast_date_hkt"] is None:
            continue
        previous = db.execute(
            """
            select id, forecast_date_hkt, forecast_min_c, forecast_max_c,
                   raw_min_c, raw_max_c, hourly_temperatures_json
            from ocf_forecast_samples
            where id < ?
              and forecast_date_hkt = ?
            order by id desc
            limit 1
            """,
            (sample_id, new["forecast_date_hkt"]),
        ).fetchone()
        if previous is None or not _forecast_sample_changed(previous, new):
            continue
        if detected is None:
            detected = monotonic_fn()
        events.append(
            _forecast_sample_event(
                new=new,
                previous=previous,
                committed=committed,
                detected=detected,
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


def enqueue_market_resolution_event(
    db: sqlite3.Connection,
    event_queue: LowLatencyEventQueue,
    *,
    market_id: int,
    target_date_hkt: str,
    previous_status: str,
    new_status: str,
    committed_monotonic: float | None = None,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> AlphaEvent | None:
    if previous_status == new_status:
        return None
    committed = monotonic_fn() if committed_monotonic is None else committed_monotonic
    detected = monotonic_fn()
    event_key = (
        f"market_resolution_changed:{target_date_hkt}:"
        f"{market_id}:{previous_status}->{new_status}"
    )
    event = AlphaEvent(
        kind="market_resolution_changed",
        event_key=event_key,
        target_date_hkt=target_date_hkt,
        source_row_id=market_id,
        previous_row_id=market_id,
        committed_monotonic=committed,
        detected_monotonic=detected,
        details={
            "market_id": market_id,
            "previous_status": previous_status,
            "new_status": new_status,
        },
    )
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
    return event


def process_next_fast_event(
    db: sqlite3.Connection,
    event_queue: LowLatencyEventQueue,
    *,
    decision_handler: Callable[[sqlite3.Connection, date], object] | None = None,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> FastEventResult:
    event = event_queue.get_nowait()
    return process_fast_event(
        db,
        event,
        decision_handler=decision_handler,
        monotonic_fn=monotonic_fn,
    )


def process_fast_event(
    db: sqlite3.Connection,
    event: AlphaEvent,
    *,
    decision_handler: Callable[[sqlite3.Connection, date], object] | None = None,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> FastEventResult:
    target = date.fromisoformat(event.target_date_hkt)
    record_latency_stage(
        db,
        event.event_key,
        "decision_started",
        monotonic_fn(),
        event.kind,
        event.details,
    )
    handler = decision_handler or _default_decision_handler(event.kind)
    result = handler(db, target)
    record_latency_stage(
        db,
        event.event_key,
        "decision_completed",
        monotonic_fn(),
        event.kind,
        _result_details(result),
    )
    return FastEventResult(event_key=event.event_key, event=event, result=result)


def compact_latency_event_line(event: AlphaEvent) -> str:
    commit_to_detect_ms = max(
        0.0, (event.detected_monotonic - event.committed_monotonic) * 1000.0
    )
    bits = [
        f"latency_event={event.kind}",
        f"key={event.event_key}",
        f"target={event.target_date_hkt}",
        f"commit_to_detect_ms={commit_to_detect_ms:.1f}",
    ]
    transition = event.details.get("transition")
    if transition is not None:
        bits.append(f"transition={transition}")
    for field in (
        "old_raw_max_c",
        "new_raw_max_c",
        "old_raw_min_c",
        "new_raw_min_c",
        "previous_status",
        "new_status",
    ):
        value = event.details.get(field)
        if value is not None:
            bits.append(f"{field}={value}")
    return " ".join(bits)


def _default_decision_handler(kind: str) -> Callable[[sqlite3.Connection, date], object]:
    if kind == "forecast_sample_changed":
        return process_forecast_entries
    if kind == "market_resolution_changed":
        return process_open_position_exits
    return process_actual_entries


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


def _forecast_sample_event(
    *,
    new: sqlite3.Row,
    previous: sqlite3.Row,
    committed: float,
    detected: float,
) -> AlphaEvent:
    target_date = str(new["forecast_date_hkt"])
    event_key = (
        f"forecast_sample_changed:{target_date}:"
        f"{previous['id']}->{new['id']}"
    )
    return AlphaEvent(
        kind="forecast_sample_changed",
        event_key=event_key,
        target_date_hkt=target_date,
        source_row_id=int(new["id"]),
        previous_row_id=int(previous["id"]),
        committed_monotonic=committed,
        detected_monotonic=detected,
        details={
            "previous_row_id": int(previous["id"]),
            "source_row_id": int(new["id"]),
            "old_forecast_min_c": _float_or_none(previous["forecast_min_c"]),
            "new_forecast_min_c": _float_or_none(new["forecast_min_c"]),
            "old_forecast_max_c": _float_or_none(previous["forecast_max_c"]),
            "new_forecast_max_c": _float_or_none(new["forecast_max_c"]),
            "old_raw_min_c": _float_or_none(previous["raw_min_c"]),
            "new_raw_min_c": _float_or_none(new["raw_min_c"]),
            "old_raw_max_c": _float_or_none(previous["raw_max_c"]),
            "new_raw_max_c": _float_or_none(new["raw_max_c"]),
        },
    )


def _forecast_sample_changed(previous: sqlite3.Row, new: sqlite3.Row) -> bool:
    keys = (
        "forecast_min_c",
        "forecast_max_c",
        "raw_min_c",
        "raw_max_c",
        "hourly_temperatures_json",
    )
    return any(previous[key] != new[key] for key in keys)


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
