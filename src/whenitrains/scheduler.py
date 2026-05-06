from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as day_time, timedelta

from .hko import HKT
from .runner import run_paper_tick


SINCE_MIDNIGHT_MINUTES = (0, 9, 19, 29, 38, 48, 58)
FORECAST_POLL_MINUTES = tuple(range(0, 60, 10))
FORECAST_CATCHUP_MINUTES = 50


@dataclass(frozen=True)
class SourcePollPlan:
    source: str
    scheduled_at: datetime
    window_start: datetime
    window_end: datetime
    cadence_seconds: int


@dataclass
class SchedulerState:
    completed_windows: set[str] = field(default_factory=set)
    last_hashes: dict[str, str] = field(default_factory=dict)
    last_source_poll_at: dict[str, datetime] = field(default_factory=dict)
    last_orderbook_fetch_at: datetime | None = None
    last_market_discovery_at: datetime | None = None
    last_current_temperature_fetch_at: datetime | None = None


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
) -> list[SourcePollPlan]:
    due: list[SourcePollPlan] = []
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
    orderbook_interval_seconds: int = 15,
    market_discovery_interval_seconds: int = 300,
    current_temperature_interval_seconds: int = 600,
) -> SchedulerActions:
    sources = {
        plan.source for plan in due_hko_sources(now_hkt, state, learned_forecast_times)
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
    current_temperature_due = not bool(sources) and (
        state.last_current_temperature_fetch_at is None
        or now_hkt - state.last_current_temperature_fetch_at
        >= timedelta(seconds=current_temperature_interval_seconds)
    )
    return SchedulerActions(
        fetch_since_midnight="since_midnight" in sources,
        fetch_bulletin="bulletin" in sources,
        fetch_current_temperature=current_temperature_due,
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
    if did_change:
        state.completed_windows.add(key)
    return did_change


def mark_orderbooks_fetched(state: SchedulerState, now_hkt: datetime) -> None:
    state.last_orderbook_fetch_at = now_hkt


def mark_market_discovered(state: SchedulerState, now_hkt: datetime) -> None:
    state.last_market_discovery_at = now_hkt


def mark_current_temperature_fetched(state: SchedulerState, now_hkt: datetime) -> None:
    state.last_current_temperature_fetch_at = now_hkt


def run_scheduled_paper_loop(
    db: sqlite3.Connection,
    fetch_since_midnight,
    fetch_bulletin,
    discover_market,
    fetch_orderbooks,
    fetch_current_temperature=None,
    learned_forecast_times=None,
    base_sleep_seconds: float = 1.0,
    max_ticks: int | None = None,
    now_fn=None,
    quiet: bool = True,
    run_tick_fn=None,
) -> None:
    state = SchedulerState()
    tick = 0
    clock = now_fn or (lambda: datetime.now(HKT))
    print("paper-scheduler started")
    while max_ticks is None or tick < max_ticks:
        now = clock()
        learned_times = learned_forecast_times() if learned_forecast_times else []
        actions = scheduler_actions(now, state, learned_times)
        plans = {
            plan.source: plan for plan in due_hko_sources(now, state, learned_times)
        }
        notes: list[str] = []
        if actions.fetch_since_midnight:
            payload = _try_fetch_source("since_midnight", fetch_since_midnight, notes)
            if payload is not None and mark_source_fetch(
                state, plans["since_midnight"], payload, now
            ):
                notes.append("since_midnight changed")
        if actions.fetch_bulletin:
            payload = _try_fetch_source("forecast", fetch_bulletin, notes)
            if payload is not None and mark_source_fetch(
                state, plans["bulletin"], payload, now
            ):
                notes.append("forecast changed")
        if actions.discover_market:
            if _try_run_action("market discovery", lambda: discover_market(now.date()), notes):
                mark_market_discovered(state, now)
                notes.append("discovered market")
        if actions.fetch_orderbooks:
            if _try_run_action("orderbooks", lambda: fetch_orderbooks(now.date()), notes):
                mark_orderbooks_fetched(state, now)
                notes.append("fetched orderbooks")
        tick_fn = run_tick_fn or run_paper_tick
        result = tick_fn(db, today_hkt=now.date())
        if should_print_scheduled_tick(notes, result, quiet):
            print(
                "scheduled-paper "
                f"actions={','.join(notes) if notes else 'decisions-only'} "
                f"buys={result.buys_filled}/{result.buys_missed} "
                f"sells={result.sells_filled}/{result.sells_missed} "
                f"signals={result.signals} notes={'; '.join(result.notes)}"
            )
        if actions.fetch_current_temperature and fetch_current_temperature is not None:
            temp_notes: list[str] = []
            if _try_fetch_source("current temperature", fetch_current_temperature, temp_notes) is not None:
                mark_current_temperature_fetched(state, now)
            elif not quiet:
                print("; ".join(temp_notes))
        tick += 1
        if max_ticks is None or tick < max_ticks:
            time.sleep(base_sleep_seconds)


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


def _since_midnight_plans(target: date) -> list[SourcePollPlan]:
    plans: list[SourcePollPlan] = []
    day_start = datetime.combine(target, day_time(10, 0), tzinfo=HKT)
    day_end = datetime.combine(target, day_time(20, 0), tzinfo=HKT)
    for hour in range(10, 21):
        for minute in SINCE_MIDNIGHT_MINUTES:
            scheduled = datetime.combine(target, day_time(hour, minute), tzinfo=HKT)
            if scheduled.hour >= 20 and scheduled.minute > 0:
                continue
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
    return f"{plan.source}:{plan.scheduled_at.isoformat()}"


def _is_market_day_active(now_hkt: datetime) -> bool:
    return day_time(0, 0) <= now_hkt.time() <= day_time(23, 59, 59)


def should_print_scheduled_tick(notes: list[str], result, quiet: bool) -> bool:
    if not quiet:
        return True
    if result.buys_filled or result.buys_missed or result.sells_filled or result.sells_missed:
        return True
    if result.signals:
        return True
    interesting_actions = {"since_midnight changed", "forecast changed"}
    return any(note in interesting_actions or " failed:" in note for note in notes)
