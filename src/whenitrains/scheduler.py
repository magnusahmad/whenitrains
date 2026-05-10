from __future__ import annotations

import hashlib
import signal
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time as day_time, timedelta

from .alerting import AlertMessage, AlertSink
from .hko import HKT, parse_aws_gis_current_temperature
from .low_latency import LowLatencyEventQueue, process_next_fast_event
from .runner import RunnerResult, run_paper_tick


SINCE_MIDNIGHT_MINUTES = (0, 9, 19, 29, 38, 48, 58)
AWS_ACTUAL_POLL_MINUTES = tuple(range(0, 60, 5))
AWS_ACTUAL_FETCHABLE_BUFFER_SECONDS = 120
AWS_ACTUAL_BURST_WINDOW_SECONDS = 10
AWS_ACTUAL_BURST_CADENCE_SECONDS = 0.5
FORECAST_POLL_MINUTES = tuple(range(0, 60, 10))
FORECAST_CATCHUP_MINUTES = 50


@dataclass(frozen=True)
class SourcePollPlan:
    source: str
    scheduled_at: datetime
    window_start: datetime
    window_end: datetime
    cadence_seconds: float


@dataclass
class SchedulerState:
    completed_windows: set[str] = field(default_factory=set)
    last_hashes: dict[str, str] = field(default_factory=dict)
    last_source_poll_at: dict[str, datetime] = field(default_factory=dict)
    last_orderbook_fetch_at: datetime | None = None
    last_market_discovery_at: datetime | None = None
    last_current_temperature_fetch_at: datetime | None = None
    trading_warmed_up: bool = False
    source_backoff_until: dict[str, datetime] = field(default_factory=dict)


@dataclass(frozen=True)
class SchedulerActions:
    fetch_since_midnight: bool = False
    fetch_bulletin: bool = False
    fetch_current_temperature: bool = False
    discover_market: bool = False
    fetch_orderbooks: bool = False
    run_decisions: bool = True


def due_hko_sources(
    now_hkt: datetime,
    state: SchedulerState,
    learned_forecast_times: list[day_time] | tuple[day_time, ...] = (),
    learned_actual_times: list[day_time] | tuple[day_time, ...] = (),
) -> list[SourcePollPlan]:
    due: list[SourcePollPlan] = []
    for plan in _aws_actual_plans(now_hkt.date(), learned_actual_times):
        if _is_due(plan, now_hkt, state):
            due.append(plan)
    for plan in _since_midnight_plans(now_hkt.date()):
        if _is_due(plan, now_hkt, state):
            due.append(plan)
    bulletin_plan = _active_bulletin_plan(
        now_hkt, state, _bulletin_plans(now_hkt.date(), learned_forecast_times)
    )
    if bulletin_plan is not None and _is_due(bulletin_plan, now_hkt, state):
        due.append(bulletin_plan)
    return due


def scheduler_actions(
    now_hkt: datetime,
    state: SchedulerState,
    learned_forecast_times: list[day_time] | tuple[day_time, ...] = (),
    learned_actual_times: list[day_time] | tuple[day_time, ...] = (),
    orderbook_interval_seconds: int = 15,
    market_discovery_interval_seconds: int = 300,
) -> SchedulerActions:
    sources = {
        plan.source
        for plan in due_hko_sources(
            now_hkt, state, learned_forecast_times, learned_actual_times
        )
    }
    market_due = (
        state.last_market_discovery_at is None
        or now_hkt - state.last_market_discovery_at
        >= timedelta(seconds=market_discovery_interval_seconds)
    )
    orderbooks_due = (
        _is_market_day_active(now_hkt)
        and (
            state.last_orderbook_fetch_at is None
            or now_hkt - state.last_orderbook_fetch_at
            >= timedelta(seconds=orderbook_interval_seconds)
        )
    )
    return SchedulerActions(
        fetch_since_midnight="since_midnight" in sources,
        fetch_bulletin="bulletin" in sources,
        fetch_current_temperature="aws_actual" in sources,
        discover_market=market_due,
        fetch_orderbooks=orderbooks_due,
        run_decisions=True,
    )


def mark_source_fetch(
    state: SchedulerState,
    plan: SourcePollPlan,
    payload: str,
    now_hkt: datetime | None = None,
    changed: bool | None = None,
) -> bool:
    key = _window_key(plan)
    content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    previous = state.last_hashes.get(plan.source)
    did_change = (
        changed
        if changed is not None
        else previous is None or previous != content_hash
    )
    state.last_hashes[plan.source] = content_hash
    state.last_source_poll_at[key] = now_hkt or datetime.now(HKT)
    if did_change and plan.source != "aws_actual":
        state.completed_windows.add(key)
    return did_change


def mark_orderbooks_fetched(state: SchedulerState, now_hkt: datetime) -> None:
    state.last_orderbook_fetch_at = now_hkt


def mark_market_discovered(state: SchedulerState, now_hkt: datetime) -> None:
    state.last_market_discovery_at = now_hkt


def mark_current_temperature_fetched(state: SchedulerState, now_hkt: datetime) -> None:
    state.last_current_temperature_fetch_at = now_hkt


def mark_source_polled(
    state: SchedulerState, plan: SourcePollPlan, now_hkt: datetime
) -> None:
    state.last_source_poll_at[_window_key(plan)] = now_hkt


def mark_source_backoff(
    state: SchedulerState, source: str, until_hkt: datetime
) -> None:
    if source != "aws_actual":
        state.source_backoff_until[source] = until_hkt


def _aws_actual_payload_observed_at(payload: str) -> datetime | None:
    try:
        return parse_aws_gis_current_temperature(payload).observed_at_hkt
    except Exception:
        return None


def _is_stale_aws_actual_payload(
    payload: str, latest_observed_at: datetime | None
) -> bool:
    observed_at = _aws_actual_payload_observed_at(payload)
    return (
        latest_observed_at is not None
        and observed_at is not None
        and observed_at < latest_observed_at
    )


def _install_stop_signal_handlers(
    stop_event: threading.Event, output_label: str
) -> dict[signal.Signals, object]:
    previous_handlers: dict[signal.Signals, object] = {}
    printed = False

    def handle_stop(signum, _frame):
        nonlocal printed
        if stop_event.is_set():
            raise KeyboardInterrupt
        stop_event.set()
        if not printed:
            signame = signal.Signals(signum).name
            print(f"{output_label} stopping ({signame})", flush=True)
            printed = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, handle_stop)
        except (ValueError, OSError):
            previous_handlers.pop(sig, None)
    return previous_handlers


def _restore_signal_handlers(previous_handlers: dict[signal.Signals, object]) -> None:
    for sig, handler in previous_handlers.items():
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


def run_scheduled_paper_loop(
    db: sqlite3.Connection,
    fetch_since_midnight,
    fetch_bulletin,
    discover_market,
    fetch_orderbooks,
    fetch_current_temperature=None,
    learned_forecast_times=None,
    learned_actual_times=None,
    base_sleep_seconds: float = 1.0,
    max_ticks: int | None = None,
    now_fn=None,
    quiet: bool = True,
    run_tick_fn=None,
    low_latency_event_queue: LowLatencyEventQueue | None = None,
    fast_event_handler=None,
    reconcile_watchdog_fn=None,
    aws_actual_poll_fetch=None,
    aws_actual_poll_learned_times=None,
    output_label: str = "paper-scheduler",
    stop_event: threading.Event | None = None,
    alert_sink: AlertSink | None = None,
) -> None:
    state = SchedulerState()
    scheduler_stop = stop_event or threading.Event()
    stop_aws_actual_polling = threading.Event()
    aws_actual_thread: threading.Thread | None = None
    tick = 0
    clock = now_fn or (lambda: datetime.now(HKT))
    previous_signal_handlers = _install_stop_signal_handlers(
        scheduler_stop, output_label
    )
    print(f"{output_label} started", flush=True)
    if aws_actual_poll_fetch is not None:
        aws_actual_thread = threading.Thread(
            target=_run_aws_actual_poll_loop,
            kwargs={
                "fetch_current_temperature": aws_actual_poll_fetch,
                "learned_actual_times": aws_actual_poll_learned_times,
                "stop_event": stop_aws_actual_polling,
                "quiet": quiet,
            },
            daemon=True,
        )
        aws_actual_thread.start()
    try:
        while not scheduler_stop.is_set() and (
            max_ticks is None or tick < max_ticks
        ):
            now = clock()
            learned_times = learned_forecast_times() if learned_forecast_times else []
            learned_actuals = learned_actual_times() if learned_actual_times else []
            actions = scheduler_actions(now, state, learned_times, learned_actuals)
            plans = {
                plan.source: plan
                for plan in due_hko_sources(now, state, learned_times, learned_actuals)
            }
            notes: list[str] = []
            data_failed = False
            if actions.fetch_since_midnight:
                payload = _try_fetch_source("since_midnight", fetch_since_midnight, notes)
                if payload is None:
                    data_failed = True
                    mark_source_backoff(
                        state, "since_midnight", now + timedelta(minutes=1)
                    )
                elif mark_source_fetch(state, plans["since_midnight"], payload, now):
                    notes.append("since_midnight changed")
            if actions.fetch_bulletin:
                payload = _try_fetch_source("forecast", fetch_bulletin, notes)
                if payload is None:
                    data_failed = True
                    mark_source_backoff(state, "bulletin", now + timedelta(minutes=1))
                elif mark_source_fetch(state, plans["bulletin"], payload, now):
                    notes.append("forecast changed")
            if (
                aws_actual_poll_fetch is not None
                and not state.trading_warmed_up
                and state.last_current_temperature_fetch_at is None
            ):
                temp_notes: list[str] = []
                payload = _try_fetch_source("aws_actual", aws_actual_poll_fetch, temp_notes)
                if payload is None:
                    data_failed = True
                    notes.extend(temp_notes)
                else:
                    mark_current_temperature_fetched(state, now)
                    notes.append(_source_changed_note("aws_actual", payload))
            if (
                actions.fetch_current_temperature
                and fetch_current_temperature is not None
                and aws_actual_poll_fetch is None
            ):
                temp_notes: list[str] = []
                payload = _try_fetch_source("aws_actual", fetch_current_temperature, temp_notes)
                if payload is not None:
                    mark_current_temperature_fetched(state, now)
                    plan = plans.get("aws_actual")
                    if plan is not None and mark_source_fetch(state, plan, payload, now):
                        notes.append(_source_changed_note("aws_actual", payload))
                else:
                    data_failed = True
                    notes.extend(temp_notes)
            if actions.discover_market:
                if _try_run_action("market discovery", lambda: discover_market(now.date()), notes):
                    mark_market_discovered(state, now)
                    notes.append("discovered market")
                else:
                    data_failed = True
            if actions.fetch_orderbooks:
                if _try_run_action("orderbooks", lambda: fetch_orderbooks(now.date()), notes):
                    mark_orderbooks_fetched(state, now)
                    notes.append("fetched orderbooks")
                else:
                    data_failed = True
            if data_failed:
                warmed_up = state.trading_warmed_up
                result = RunnerResult(
                    notes=(
                        "decisions skipped: data fetch failed"
                        if warmed_up
                        else "startup warmup blocked: data fetch failed",
                    )
                )
                if warmed_up and alert_sink is not None:
                    alert_sink.send(
                        AlertMessage(
                            title=f"{output_label} source freshness breach",
                            severity="critical",
                            details={
                                "action": "decisions skipped",
                                "notes": list(notes),
                            },
                        )
                    )
            elif state.trading_warmed_up:
                tick_fn = run_tick_fn or run_paper_tick
                result = RunnerResult()
                if reconcile_watchdog_fn is not None:
                    result = _merge_runner_results(
                        result, reconcile_watchdog_fn(db)
                    )
                if low_latency_event_queue is not None and not low_latency_event_queue.empty():
                    handler = fast_event_handler or tick_fn
                    result = _merge_runner_results(
                        result, RunnerResult(notes=("fast event queue drained",))
                    )
                    while not low_latency_event_queue.empty():
                        fast_result = process_next_fast_event(
                            db,
                            low_latency_event_queue,
                            decision_handler=handler,
                        )
                        if isinstance(fast_result.result, RunnerResult):
                            result = _merge_runner_results(result, fast_result.result)
                    notes.append("fast hko events")
                else:
                    result = _merge_runner_results(
                        result, tick_fn(db, today_hkt=now.date())
                    )
            else:
                state.trading_warmed_up = True
                result = RunnerResult(notes=("startup warmup: trading skipped",))
            if should_print_scheduled_tick(notes, result, quiet):
                print(
                    f"{output_label} "
                    f"actions={','.join(notes) if notes else 'decisions-only'} "
                    f"buys={result.buys_filled}/{result.buys_missed} "
                    f"sells={result.sells_filled}/{result.sells_missed} "
                    f"signals={result.signals} notes={'; '.join(result.notes)}",
                    flush=True,
                )
                if result.buys_filled or result.sells_filled:
                    print(
                        f"💰 TRADE EXECUTED 💰 {output_label} "
                        f"filled_buys={result.buys_filled} "
                        f"filled_sells={result.sells_filled} "
                        f"notes={'; '.join(result.notes) or 'n/a'}",
                        flush=True,
                    )
                    if alert_sink is not None:
                        alert_sink.send(
                            AlertMessage(
                                title=f"{output_label} trade executed",
                                severity="info",
                                details={
                                    "filled_buys": result.buys_filled,
                                    "filled_sells": result.sells_filled,
                                    "notes": list(result.notes),
                                },
                            )
                        )
            tick += 1
            if (
                not scheduler_stop.is_set()
                and (max_ticks is None or tick < max_ticks)
            ):
                scheduler_stop.wait(base_sleep_seconds)
    except KeyboardInterrupt:
        scheduler_stop.set()
        print(f"{output_label} stopping", flush=True)
    finally:
        stop_aws_actual_polling.set()
        if aws_actual_thread is not None:
            aws_actual_thread.join(timeout=5)
        _restore_signal_handlers(previous_signal_handlers)
        if scheduler_stop.is_set():
            print(f"{output_label} stopped", flush=True)


def _run_aws_actual_poll_loop(
    fetch_current_temperature,
    learned_actual_times,
    stop_event: threading.Event,
    quiet: bool,
) -> None:
    state = SchedulerState()
    latest_observed_at: datetime | None = None
    while not stop_event.is_set():
        now = datetime.now(HKT)
        learned_actuals = learned_actual_times() if learned_actual_times else []
        plans = [
            plan
            for plan in due_hko_sources(
                now,
                state,
                learned_actual_times=learned_actuals,
            )
            if plan.source == "aws_actual"
        ]
        if plans:
            notes: list[str] = []
            payload = _try_fetch_source("aws_actual", fetch_current_temperature, notes)
            if payload is not None:
                mark_current_temperature_fetched(state, now)
                observed_at = _aws_actual_payload_observed_at(payload)
                if _is_stale_aws_actual_payload(payload, latest_observed_at):
                    for plan in plans:
                        mark_source_polled(state, plan, now)
                else:
                    if observed_at is not None:
                        latest_observed_at = (
                            observed_at
                            if latest_observed_at is None
                            else max(latest_observed_at, observed_at)
                        )
                    changed = False
                    for plan in plans:
                        changed = mark_source_fetch(state, plan, payload, now) or changed
                    if changed:
                        notes.append(_source_changed_note("aws_actual", payload))
            if not quiet or any(
                note.startswith("aws_actual changed") or " failed:" in note
                for note in notes
            ):
                print(
                    "aws-actual-poller "
                    f"actions={','.join(notes) if notes else 'fetched'}"
                )
        stop_event.wait(0.5)


def _try_fetch_source(source: str, fetch_fn, notes: list[str]) -> str | None:
    try:
        return fetch_fn()
    except Exception as exc:
        notes.append(f"{source} fetch failed: {type(exc).__name__}: {exc}")
        return None


def _try_run_action(action: str, action_fn, notes: list[str]) -> bool:
    try:
        action_fn()
        return True
    except Exception as exc:
        notes.append(f"{action} failed: {type(exc).__name__}: {exc}")
        return False


def _merge_runner_results(first: RunnerResult, second: RunnerResult) -> RunnerResult:
    return RunnerResult(
        buys_filled=first.buys_filled + second.buys_filled,
        buys_missed=first.buys_missed + second.buys_missed,
        sells_filled=first.sells_filled + second.sells_filled,
        sells_missed=first.sells_missed + second.sells_missed,
        signals=first.signals + second.signals,
        notes=first.notes + second.notes,
    )


def _source_changed_note(source: str, payload: str) -> str:
    if source == "aws_actual":
        first_line = payload.splitlines()[0].strip() if payload.splitlines() else ""
        if first_line:
            return f"aws_actual changed: {first_line}"
    return f"{source} changed"


def _since_midnight_plans(target: date) -> list[SourcePollPlan]:
    plans: list[SourcePollPlan] = []
    day_start = datetime.combine(target, day_time(0, 0), tzinfo=HKT)
    day_end = datetime.combine(target, day_time(23, 59, 59), tzinfo=HKT)
    for hour in range(24):
        for minute in SINCE_MIDNIGHT_MINUTES:
            scheduled = datetime.combine(target, day_time(hour, minute), tzinfo=HKT)
            window_start = max(scheduled - timedelta(minutes=1), day_start)
            window_end = min(scheduled + timedelta(minutes=2), day_end)
            plans.append(
                SourcePollPlan(
                    source="since_midnight",
                    scheduled_at=scheduled,
                    window_start=window_start,
                    window_end=window_end,
                    cadence_seconds=10,
                )
            )
    return plans


def _aws_actual_plans(
    target: date, learned_actual_times: list[day_time] | tuple[day_time, ...] = ()
) -> list[SourcePollPlan]:
    plans: list[SourcePollPlan] = []
    day_start = datetime.combine(target, day_time(0, 0), tzinfo=HKT)
    day_end = datetime.combine(target, day_time(23, 59, 59), tzinfo=HKT)
    scheduled_times = {
        day_time(hour, minute)
        for hour in range(24)
        for minute in AWS_ACTUAL_POLL_MINUTES
    }
    for scheduled_time in sorted(scheduled_times):
        scheduled = datetime.combine(target, scheduled_time, tzinfo=HKT)
        plans.append(
            SourcePollPlan(
                source="aws_actual",
                scheduled_at=scheduled,
                window_start=max(
                    scheduled - timedelta(seconds=30), day_start
                ),
                window_end=min(
                    scheduled + timedelta(seconds=30), day_end
                ),
                cadence_seconds=10,
            )
        )
    learned_publish_times = _expanded_aws_publish_times(
        set(learned_actual_times) - scheduled_times
    )
    for scheduled_time in sorted(learned_publish_times):
        scheduled = datetime.combine(target, scheduled_time, tzinfo=HKT)
        plans.append(
            SourcePollPlan(
                source="aws_actual",
                scheduled_at=scheduled,
                window_start=max(
                    scheduled - timedelta(seconds=AWS_ACTUAL_BURST_WINDOW_SECONDS),
                    day_start,
                ),
                window_end=min(
                    scheduled + timedelta(seconds=AWS_ACTUAL_BURST_WINDOW_SECONDS),
                    day_end,
                ),
                cadence_seconds=AWS_ACTUAL_BURST_CADENCE_SECONDS,
            )
        )
        plans.append(
            SourcePollPlan(
                source="aws_actual",
                scheduled_at=scheduled,
                window_start=max(
                    scheduled - timedelta(seconds=AWS_ACTUAL_FETCHABLE_BUFFER_SECONDS),
                    day_start,
                ),
                window_end=min(
                    scheduled + timedelta(seconds=AWS_ACTUAL_FETCHABLE_BUFFER_SECONDS),
                    day_end,
                ),
                cadence_seconds=10,
            )
        )
    return plans


def _expanded_aws_publish_times(learned_publish_times: set[day_time]) -> set[day_time]:
    expanded: set[day_time] = set(learned_publish_times)
    learned_publish_minute_remainders = {
        learned.minute % 10 for learned in learned_publish_times
    }
    for hour in range(24):
        for minute in range(60):
            if minute % 10 in learned_publish_minute_remainders:
                expanded.add(day_time(hour, minute))
    return expanded


def _bulletin_plans(
    target: date, learned_forecast_times: list[day_time] | tuple[day_time, ...] = ()
) -> list[SourcePollPlan]:
    plans = []
    regular_times: set[day_time] = set()
    for hour in range(24):
        for minute in FORECAST_POLL_MINUTES:
            regular_times.add(day_time(hour, minute))
    for scheduled_time in sorted(regular_times):
        scheduled = datetime.combine(target, scheduled_time, tzinfo=HKT)
        plans.append(
            SourcePollPlan(
                source="bulletin",
                scheduled_at=scheduled,
                window_start=scheduled,
                window_end=scheduled + timedelta(seconds=10),
                cadence_seconds=10,
            )
        )
    learned_times: set[day_time] = set()
    learned_minutes = {learned_time.minute for learned_time in learned_forecast_times}
    for hour in range(24):
        for minute in learned_minutes:
            learned_times.add(day_time(hour, minute))
    learned_times.update(learned_forecast_times)
    for scheduled_time in sorted(learned_times):
        scheduled = datetime.combine(target, scheduled_time, tzinfo=HKT)
        plans.append(
            SourcePollPlan(
                source="bulletin",
                scheduled_at=scheduled,
                window_start=scheduled,
                window_end=scheduled + timedelta(minutes=FORECAST_CATCHUP_MINUTES),
                cadence_seconds=10,
            )
        )
    return plans


def _is_due(plan: SourcePollPlan, now_hkt: datetime, state: SchedulerState) -> bool:
    key = _window_key(plan)
    if key in state.completed_windows:
        return False
    backoff_until = state.source_backoff_until.get(plan.source)
    if (
        plan.source != "aws_actual"
        and backoff_until is not None
        and now_hkt < backoff_until
    ):
        return False
    if not (plan.window_start <= now_hkt <= plan.window_end):
        return False
    last_poll = state.last_source_poll_at.get(key)
    if last_poll is None:
        return True
    return now_hkt - last_poll >= timedelta(seconds=plan.cadence_seconds)


def _active_bulletin_plan(
    now_hkt: datetime, state: SchedulerState, plans: list[SourcePollPlan]
) -> SourcePollPlan | None:
    active = [
        plan
        for plan in plans
        if plan.window_start <= now_hkt <= plan.window_end
    ]
    if not active:
        return None
    latest = max(active, key=lambda plan: plan.scheduled_at)
    if _window_key(latest) in state.completed_windows:
        return None
    return latest


def _window_key(plan: SourcePollPlan) -> str:
    return (
        f"{plan.source}:{plan.scheduled_at.isoformat()}:"
        f"{plan.window_start.isoformat()}:{plan.window_end.isoformat()}"
    )


def _is_market_day_active(now_hkt: datetime) -> bool:
    return day_time(0, 0) <= now_hkt.time() <= day_time(23, 59, 59)


def should_print_scheduled_tick(notes: list[str], result, quiet: bool) -> bool:
    if not quiet:
        return True
    if result.buys_filled or result.buys_missed or result.sells_filled or result.sells_missed:
        return True
    if result.signals:
        return True
    interesting_actions = ("aws_actual changed", "since_midnight changed", "forecast changed")
    return any(note.startswith(interesting_actions) or " failed:" in note for note in notes)
