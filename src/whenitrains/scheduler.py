from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as day_time, timedelta

from .hko import HKT
from .runner import run_paper_tick


SINCE_MIDNIGHT_MINUTES = (0, 9, 19, 29, 38, 48, 58)
BULLETIN_TIMES = (
    day_time(0, 0),
    day_time(16, 15),
    day_time(23, 15),
)


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
    last_orderbook_fetch_at: datetime | None = None
    last_market_discovery_at: datetime | None = None


@dataclass(frozen=True)
class SchedulerActions:
    fetch_since_midnight: bool = False
    fetch_bulletin: bool = False
    discover_market: bool = False
    fetch_orderbooks: bool = False
    run_decisions: bool = True


def due_hko_sources(now_hkt: datetime, state: SchedulerState) -> list[SourcePollPlan]:
    due: list[SourcePollPlan] = []
    for plan in _since_midnight_plans(now_hkt.date()):
        if _is_due(plan, now_hkt, state):
            due.append(plan)
    for plan in _bulletin_plans(now_hkt.date()):
        if _is_due(plan, now_hkt, state):
            due.append(plan)
    return due


def scheduler_actions(
    now_hkt: datetime,
    state: SchedulerState,
    orderbook_interval_seconds: int = 15,
    market_discovery_interval_seconds: int = 300,
) -> SchedulerActions:
    sources = {plan.source for plan in due_hko_sources(now_hkt, state)}
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
        discover_market=market_due,
        fetch_orderbooks=orderbooks_due,
        run_decisions=True,
    )


def mark_source_fetch(
    state: SchedulerState, plan: SourcePollPlan, payload: str, changed: bool | None = None
) -> bool:
    content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    previous = state.last_hashes.get(plan.source)
    did_change = changed if changed is not None else previous is not None and previous != content_hash
    state.last_hashes[plan.source] = content_hash
    if did_change:
        state.completed_windows.add(_window_key(plan))
    return did_change


def mark_orderbooks_fetched(state: SchedulerState, now_hkt: datetime) -> None:
    state.last_orderbook_fetch_at = now_hkt


def mark_market_discovered(state: SchedulerState, now_hkt: datetime) -> None:
    state.last_market_discovery_at = now_hkt


def run_scheduled_paper_loop(
    db: sqlite3.Connection,
    fetch_since_midnight,
    fetch_bulletin,
    discover_market,
    fetch_orderbooks,
    base_sleep_seconds: float = 1.0,
    max_ticks: int | None = None,
    now_fn=None,
    quiet: bool = True,
) -> None:
    state = SchedulerState()
    tick = 0
    clock = now_fn or (lambda: datetime.now(HKT))
    print("paper-scheduler started")
    while max_ticks is None or tick < max_ticks:
        now = clock()
        actions = scheduler_actions(now, state)
        plans = {plan.source: plan for plan in due_hko_sources(now, state)}
        notes: list[str] = []
        if actions.fetch_since_midnight:
            payload = fetch_since_midnight()
            mark_source_fetch(state, plans["since_midnight"], payload)
            notes.append("fetched since_midnight")
        if actions.fetch_bulletin:
            payload = fetch_bulletin()
            mark_source_fetch(state, plans["bulletin"], payload)
            notes.append("fetched bulletin")
        if actions.discover_market:
            discover_market(now.date())
            mark_market_discovered(state, now)
            notes.append("discovered market")
        if actions.fetch_orderbooks:
            fetch_orderbooks(now.date())
            mark_orderbooks_fetched(state, now)
            notes.append("fetched orderbooks")
        result = run_paper_tick(db, today_hkt=now.date())
        if should_print_scheduled_tick(notes, result, quiet):
            print(
                "scheduled-paper "
                f"actions={','.join(notes) if notes else 'decisions-only'} "
                f"buys={result.buys_filled}/{result.buys_missed} "
                f"sells={result.sells_filled}/{result.sells_missed} "
                f"signals={result.signals} notes={'; '.join(result.notes)}"
            )
        tick += 1
        if max_ticks is None or tick < max_ticks:
            time.sleep(base_sleep_seconds)


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


def _bulletin_plans(target: date) -> list[SourcePollPlan]:
    times = [day_time(hour, 45) for hour in range(24)]
    times.extend(BULLETIN_TIMES)
    plans = []
    for scheduled_time in sorted(set(times)):
        scheduled = datetime.combine(target, scheduled_time, tzinfo=HKT)
        plans.append(
            SourcePollPlan(
                source="bulletin",
                scheduled_at=scheduled,
                window_start=scheduled - timedelta(seconds=30),
                window_end=scheduled + timedelta(minutes=2),
                cadence_seconds=10,
            )
        )
    return plans


def _is_due(plan: SourcePollPlan, now_hkt: datetime, state: SchedulerState) -> bool:
    if _window_key(plan) in state.completed_windows:
        return False
    return plan.window_start <= now_hkt <= plan.window_end


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
    interesting_actions = {"fetched since_midnight", "fetched bulletin"}
    return any(note in interesting_actions for note in notes)
