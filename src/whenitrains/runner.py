from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from math import floor

from .config import Settings
from .engine import TradeCandidate
from .hko import HKT
from .markets import PredicateType, parse_outcome_label, predicate_matches
from .paper_db import execute_paper_buy, execute_paper_sell
from .polymarket import Outcome, fetch_orderbook
from .signals import DirectionalImpact, PriceResponse
from .live import LiveClobClient, execute_live_buy, execute_live_sell
from .storage import (
    dashboard_stats,
    find_outcome_by_token,
    get_live_position,
    has_processed_event,
    latest_orderbook,
    latest_forecast_high,
    latest_observed_min_for_date,
    latest_observed_max_for_date,
    latest_two_forecast_highs,
    latest_two_orderbook_prices,
    list_open_live_positions,
    list_open_paper_positions,
    list_outcomes_for_date,
    list_tradeable_forecast_dates,
    observed_min_decreases,
    observed_max_increases,
    store_signal,
    store_orderbook,
    store_trading_decision,
)


_LIVE_CLIENT: LiveClobClient | None = None
_LIVE_ORDER_CAP_USD: float | None = None


@dataclass(frozen=True)
class RunnerResult:
    buys_filled: int = 0
    buys_missed: int = 0
    sells_filled: int = 0
    sells_missed: int = 0
    signals: int = 0
    notes: tuple[str, ...] = ()


def run_paper_tick(db: sqlite3.Connection, today_hkt: date | None = None) -> RunnerResult:
    today = today_hkt or datetime.now(HKT).date()
    forecast_result = process_all_forecast_entries(db, today)
    actual_result = process_actual_entries(db, today)
    exit_result = process_open_position_exits(db, today_hkt=today)
    return RunnerResult(
        buys_filled=forecast_result.buys_filled + actual_result.buys_filled,
        buys_missed=forecast_result.buys_missed + actual_result.buys_missed,
        sells_filled=forecast_result.sells_filled + exit_result.sells_filled,
        sells_missed=forecast_result.sells_missed + exit_result.sells_missed,
        signals=forecast_result.signals + actual_result.signals,
        notes=forecast_result.notes + actual_result.notes + exit_result.notes,
    )


def run_live_tick(
    db: sqlite3.Connection,
    client: LiveClobClient,
    today_hkt: date | None = None,
    order_cap_usd: float = Settings.live_scheduler_order_cap_usd,
) -> RunnerResult:
    global _LIVE_CLIENT, _LIVE_ORDER_CAP_USD
    previous_client = _LIVE_CLIENT
    previous_cap = _LIVE_ORDER_CAP_USD
    _LIVE_CLIENT = client
    _LIVE_ORDER_CAP_USD = order_cap_usd
    try:
        return run_paper_tick(db, today_hkt=today_hkt)
    finally:
        _LIVE_CLIENT = previous_client
        _LIVE_ORDER_CAP_USD = previous_cap


def process_all_forecast_entries(db: sqlite3.Connection, today_hkt: date) -> RunnerResult:
    aggregate = RunnerResult()
    notes: list[str] = []
    for target_date_text in list_tradeable_forecast_dates(db, today_hkt.isoformat()):
        target_date = date.fromisoformat(target_date_text)
        result = process_forecast_entries(db, target_date, today_hkt=today_hkt)
        aggregate = RunnerResult(
            buys_filled=aggregate.buys_filled + result.buys_filled,
            buys_missed=aggregate.buys_missed + result.buys_missed,
            sells_filled=aggregate.sells_filled + result.sells_filled,
            sells_missed=aggregate.sells_missed + result.sells_missed,
            signals=aggregate.signals + result.signals,
            notes=(),
        )
        if result.notes:
            notes.extend(f"{target_date_text}: {note}" for note in result.notes)
    if not notes:
        notes.append("no tradeable forecast dates")
    return RunnerResult(
        buys_filled=aggregate.buys_filled,
        buys_missed=aggregate.buys_missed,
        sells_filled=aggregate.sells_filled,
        sells_missed=aggregate.sells_missed,
        signals=aggregate.signals,
        notes=tuple(notes),
    )


def process_forecast_entries(
    db: sqlite3.Connection, target_date: date, today_hkt: date | None = None
) -> RunnerResult:
    today = today_hkt or datetime.now(HKT).date()
    value_result = process_forecast_value_entry(db, target_date, today)
    high_result = _process_forecast_change_entries_kind(db, target_date, today, "highest")
    low_rows = _lowest_temperature_rows(list_outcomes_for_date(db, target_date.isoformat()))
    if not low_rows:
        return _merge_runner_results(value_result, high_result)
    low_result = _process_forecast_change_entries_kind(
        db, target_date, today, "lowest", rows=low_rows
    )
    return _merge_runner_results(_merge_runner_results(value_result, high_result), low_result)


def _process_forecast_change_entries_kind(
    db: sqlite3.Connection,
    target_date: date,
    today: date,
    market_kind: str,
    rows: list[sqlite3.Row] | None = None,
) -> RunnerResult:
    noun = "forecast high" if market_kind == "highest" else "forecast low"
    event_type = "forecast_change" if market_kind == "highest" else "lowest_forecast_change"
    stale_reason = _ocf_forecast_stale_reason(db, target_date.isoformat())
    if stale_reason is not None:
        return RunnerResult(notes=(f"{noun} skipped: {stale_reason}",))
    latest = _effective_forecast_rows(
        db,
        target_date.isoformat(),
        limit=2,
        market_kind=market_kind,
    )
    if len(latest) < 2:
        return RunnerResult(notes=(f"need two decimal {noun}s",))
    new, old = latest[0], latest[1]
    if new["forecast_date_hkt"] != target_date.isoformat():
        return RunnerResult(notes=("latest forecast is not for target day",))
    old_value = float(old["forecast_value_c"])
    new_value = float(new["forecast_value_c"])
    if old_value == new_value:
        return RunnerResult(notes=(f"{noun} unchanged",))
    event_key = (
        f"{event_type}:{target_date.isoformat()}:"
        f"{old['update_time']}:{old_value}->{new['update_time']}:{new_value}"
    )
    if has_processed_event(db, event_key):
        return RunnerResult(notes=(f"{noun} change already processed",))
    exit_result = process_forecast_position_exits(
        db, target_date, new_value, event_key=event_key, market_kind=market_kind
    )

    rows = rows or _highest_temperature_rows(list_outcomes_for_date(db, target_date.isoformat()))
    outcomes = [_outcome_from_row(row) for row in rows]
    prior_yes_asks: dict[str, float] = {}
    current_yes_asks: dict[str, float] = {}
    missing_orderbooks = False
    for row in rows:
        prices = latest_two_orderbook_prices(db, row["yes_token_id"])
        if len(prices) < 2 or prices[0]["best_ask"] is None or prices[1]["best_ask"] is None:
            missing_orderbooks = True
            store_trading_decision(
                db,
                event_type,
                row["yes_token_id"],
                row["label"],
                "YES",
                "BUY",
                "missed",
                "missing prior/current yes ask",
                {f"old_{market_kind}": old_value, f"new_{market_kind}": new_value},
                event_key=event_key,
            )
            continue
        current_yes_asks[row["polymarket_market_id"]] = float(prices[0]["best_ask"])
        prior_yes_asks[row["polymarket_market_id"]] = float(prices[1]["best_ask"])

    candidates = build_forecast_move_candidates(
        outcomes=outcomes,
        old_forecast_max_c=old_value,
        new_forecast_max_c=new_value,
        prior_yes_asks=prior_yes_asks,
        current_yes_asks=current_yes_asks,
        max_move=Settings.forecast_change_max_price_move,
    )
    if not candidates:
        if missing_orderbooks:
            return RunnerResult(
                sells_filled=exit_result.sells_filled,
                sells_missed=exit_result.sells_missed,
                notes=(f"{noun} waiting for orderbooks",) + exit_result.notes,
            )
        _mark_event_processed(
            db,
            event_type=event_type,
            event_key=event_key,
            reason=f"{noun} changed {old_value} -> {new_value}",
            details={f"old_{market_kind}": old_value, f"new_{market_kind}": new_value},
        )
        no_candidates_note = (
            "no stale forecast candidates"
            if market_kind == "highest"
            else "no stale forecast low candidates"
        )
        return RunnerResult(
            buys_missed=1,
            sells_filled=exit_result.sells_filled,
            sells_missed=exit_result.sells_missed,
            notes=(no_candidates_note,) + exit_result.notes,
        )

    _mark_event_processed(
        db,
        event_type=event_type,
        event_key=event_key,
        reason=f"{noun} changed {old_value} -> {new_value}",
        details={f"old_{market_kind}": old_value, f"new_{market_kind}": new_value},
    )
    store_signal(
        db,
        market_id=target_date.isoformat(),
        trigger_type=event_type,
        current_max_c=None,
        forecast_max_c=new_value,
        affected_outcomes={candidate.outcome.label: candidate.impact.value for candidate in candidates},
        price_response={candidate.outcome.label: candidate.price_response.value for candidate in candidates},
        notes=f"{noun} changed {old_value} -> {new_value}",
    )

    buys_filled = 0
    buys_missed = 0
    max_entry_price = _forecast_change_max_entry_price(target_date, today)
    for candidate in candidates:
        filled = _execute_candidate_buy(
            db,
            candidate,
            event_type=event_type,
            event_key=event_key,
            max_buy_price=max_entry_price,
        )
        if filled is True:
            buys_filled += 1
        elif filled is False:
            buys_missed += 1
    return RunnerResult(
        buys_filled=buys_filled,
        buys_missed=buys_missed,
        sells_filled=exit_result.sells_filled,
        sells_missed=exit_result.sells_missed,
        signals=1,
        notes=(f"{noun} changed {old_value} -> {new_value}",) + exit_result.notes,
    )


def process_forecast_value_entry(
    db: sqlite3.Connection, target_date: date, today_hkt: date
) -> RunnerResult:
    high_result = _process_forecast_value_entry_kind(db, target_date, today_hkt, "highest")
    low_rows = _lowest_temperature_rows(list_outcomes_for_date(db, target_date.isoformat()))
    if not low_rows:
        return high_result
    low_result = _process_forecast_value_entry_kind(
        db, target_date, today_hkt, "lowest", rows=low_rows
    )
    return _merge_runner_results(high_result, low_result)


def _process_forecast_value_entry_kind(
    db: sqlite3.Connection,
    target_date: date,
    today_hkt: date,
    market_kind: str,
    rows: list[sqlite3.Row] | None = None,
) -> RunnerResult:
    lead_days = (target_date - today_hkt).days
    label = "forecast value" if market_kind == "highest" else "low forecast value"
    if lead_days < 0:
        return RunnerResult(
            notes=(f"{label} skipped: {target_date.isoformat()} is before today",)
        )
    if lead_days > Settings.forecast_value_max_lead_days:
        return RunnerResult(
            notes=(
                f"{label} skipped: "
                f"{target_date.isoformat()} lead_days={lead_days} "
                f"> max={Settings.forecast_value_max_lead_days}",
            )
        )
    stale_reason = _ocf_forecast_stale_reason(db, target_date.isoformat())
    if stale_reason is not None:
        return RunnerResult(
            notes=(
                f"{label} skipped: "
                f"{target_date.isoformat()} {stale_reason}",
            )
        )
    latest = _latest_effective_forecast_value(db, target_date.isoformat(), market_kind)
    if latest is None:
        return RunnerResult(
            notes=(
                f"{label} skipped: "
                f"{target_date.isoformat()} missing decimal forecast",
            )
        )
    forecast_value = float(latest["forecast_value_c"])
    event_type = "forecast_value" if market_kind == "highest" else "lowest_forecast_value"
    rows = rows or _highest_temperature_rows(list_outcomes_for_date(db, target_date.isoformat()))
    if not rows:
        return RunnerResult(
            notes=(f"{label} skipped: {target_date.isoformat()} missing outcomes",)
        )
    books = _latest_yes_books_by_market(db, rows)
    forecast_row = _forecast_matching_row(rows, forecast_value)
    if forecast_row is None:
        return RunnerResult(
            notes=(
                f"{label} skipped: "
                f"{target_date.isoformat()} forecast_{market_kind}={forecast_value:g} missing matching bucket",
            )
        )
    guard_reason = _forecast_value_guard_reason(db, target_date.isoformat(), forecast_row, "YES")
    if guard_reason is not None:
        store_trading_decision(
            db,
            event_type,
            forecast_row["yes_token_id"],
            forecast_row["label"],
            "YES",
            "BUY",
            "ignored",
            guard_reason,
            {
                f"forecast_{market_kind}": forecast_value,
                "forecast_label": forecast_row["label"],
            },
        )
        return RunnerResult(
            notes=(
                f"{label} skipped: "
                f"{target_date.isoformat()} {forecast_row['label']} "
                f"{guard_reason}",
            )
        )
    forecast_book = books.get(forecast_row["polymarket_market_id"])
    if forecast_book is None or forecast_book.best_ask is None:
        return RunnerResult(
            notes=(
                f"{label} skipped: "
                f"{target_date.isoformat()} {forecast_row['label']} missing YES ask",
            )
        )
    if forecast_book.best_ask > Settings.forecast_value_max_yes_ask:
        return RunnerResult(
            notes=(
                f"{label} skipped: "
                f"{target_date.isoformat()} {forecast_row['label']} "
                f"ask={forecast_book.best_ask:.3f} "
                f"> cheap_threshold={Settings.forecast_value_max_yes_ask:.3f}",
            )
        )
    favorite = _favorite_yes_row(rows, books)
    if favorite is None:
        return RunnerResult(
            notes=(f"{label} skipped: {target_date.isoformat()} missing market favorite",)
        )
    favorite_predicate = parse_outcome_label(favorite["label"])
    forecast_predicate = parse_outcome_label(forecast_row["label"])
    if favorite_predicate.value_c is None or forecast_predicate.value_c is None:
        return RunnerResult(
            notes=(
                f"{label} skipped: "
                f"{target_date.isoformat()} unparseable favorite={favorite['label']} "
                f"or forecast_bucket={forecast_row['label']}",
            )
        )
    if favorite["polymarket_market_id"] == forecast_row["polymarket_market_id"]:
        return RunnerResult(
            notes=(
                f"{label} skipped: "
                f"{target_date.isoformat()} {forecast_row['label']} already market favorite",
            )
        )
    if market_kind == "highest" and (
        favorite_predicate.value_c > forecast_predicate.value_c
        and forecast_predicate.type != PredicateType.GTE_C
    ):
        return RunnerResult(
            notes=(
                f"{label} skipped: "
                f"{target_date.isoformat()} favorite {favorite['label']} is above "
                f"forecast bucket {forecast_row['label']}; threshold risk",
            )
        )
    if market_kind == "highest" and favorite_predicate.value_c >= forecast_predicate.value_c:
        return RunnerResult(
            notes=(
                f"{label} skipped: "
                f"{target_date.isoformat()} favorite {favorite['label']} is not below "
                f"forecast bucket {forecast_row['label']}",
            )
        )
    if market_kind == "lowest" and favorite_predicate.value_c <= forecast_predicate.value_c:
        return RunnerResult(
            notes=(
                f"{label} skipped: "
                f"{target_date.isoformat()} favorite {favorite['label']} is not above "
                f"forecast bucket {forecast_row['label']}",
            )
        )
    event_key = (
        f"{event_type}:{target_date.isoformat()}:"
        f"{forecast_value:g}:{forecast_row['polymarket_market_id']}:"
        f"{favorite['polymarket_market_id']}:{forecast_book.best_ask}"
    )
    if has_processed_event(db, event_key):
        return RunnerResult(
            notes=(
                f"{label} skipped: "
                f"{target_date.isoformat()} {forecast_row['label']} already processed "
                f"at ask={forecast_book.best_ask:.3f}",
            )
        )
    _mark_event_processed(
        db,
        event_type=event_type,
        event_key=event_key,
        reason=(
            f"forecast bucket {forecast_row['label']} cheap at "
            f"{forecast_book.best_ask:.3f}; favorite is {favorite['label']}"
        ),
        details={
            f"forecast_{market_kind}": forecast_value,
            "forecast_label": forecast_row["label"],
            "forecast_yes_ask": forecast_book.best_ask,
            "favorite_label": favorite["label"],
        },
    )
    candidate = TradeCandidate(
        outcome=_outcome_from_row(forecast_row),
        side="BUY_YES",
        impact=DirectionalImpact.INCREASES_YES_PROBABILITY,
        price_response=PriceResponse.PRICE_NOT_MOVED_WITH_EVENT,
        prior_yes_ask=forecast_book.best_ask,
        current_yes_ask=forecast_book.best_ask,
        reason=(
            "forecast bucket priced unrealistically low vs HKO forecast"
            if market_kind == "highest"
            else "lowest forecast bucket priced unrealistically low vs HKO forecast"
        ),
    )
    filled = _execute_candidate_buy(
        db,
        candidate,
        event_type=event_type,
        event_key=event_key,
        max_buy_price=Settings.forecast_value_max_yes_ask,
        allow_existing_position=True,
        size_usd=_remaining_position_budget(db, forecast_row["yes_token_id"]),
    )
    store_signal(
        db,
        market_id=target_date.isoformat(),
        trigger_type=event_type,
        current_max_c=None,
        forecast_max_c=forecast_value,
        affected_outcomes={forecast_row["label"]: "INCREASES_YES_PROBABILITY"},
        price_response={forecast_row["label"]: "PRICE_NOT_MOVED_WITH_EVENT"},
        notes=f"forecast bucket {forecast_row['label']} cheap vs lower favorite {favorite['label']}",
    )
    return RunnerResult(
        buys_filled=1 if filled is True else 0,
        buys_missed=1 if filled is False else 0,
        signals=1,
        notes=(f"{label} entry {forecast_row['label']} YES",),
    )


def process_forecast_position_exits(
    db: sqlite3.Connection,
    target_date: date,
    new_forecast_max_c: float,
    event_key: str | None = None,
    market_kind: str = "highest",
) -> RunnerResult:
    sells_filled = 0
    sells_missed = 0
    notes: list[str] = []
    positions = list_open_live_positions(db) if _LIVE_CLIENT is not None else list_open_paper_positions(db)
    for pos in positions:
        token_id = pos["outcome_id"]
        outcome = find_outcome_by_token(db, token_id)
        if outcome is None or outcome["target_date_hkt"] != target_date.isoformat():
            continue
        if _temperature_market_kind_for_row(outcome) != market_kind:
            continue
        side = "YES" if token_id == outcome["yes_token_id"] else "NO"
        reason = _hourly_forecast_invalidation_reason(db, outcome, side)
        if reason is None and _position_invalidated_by_forecast(
            outcome, side, new_forecast_max_c
        ):
            reason = "position invalidated by forecast change"
        if reason is None:
            continue
        try:
            book = latest_orderbook(db, token_id)
        except ValueError:
            store_trading_decision(
                db,
                "forecast_exit",
                token_id,
                outcome["label"],
                side,
                "SELL",
                "missed",
                "missing orderbook",
                {"forecast_max": new_forecast_max_c},
                event_key=event_key,
            )
            sells_missed += 1
            continue
        if _LIVE_CLIENT is not None:
            result = execute_live_sell(
                db,
                _LIVE_CLIENT,
                token_id=token_id,
                bids=book.bids,
                reason=reason,
                label=outcome["label"],
                event_type="forecast_exit",
                event_key=event_key,
            )
        else:
            result = execute_paper_sell(db, token_id, book.bids, reason)
        if result.status == "filled":
            store_trading_decision(
                db,
                "forecast_exit",
                token_id,
                outcome["label"],
                side,
                "SELL",
                "filled",
                reason,
                {"forecast_max": new_forecast_max_c, "bid": book.best_bid},
                event_key=event_key,
            )
            sells_filled += 1
            notes.append(f"sold forecast-invalidated {outcome['label']} {side}")
        else:
            store_trading_decision(
                db,
                "forecast_exit",
                token_id,
                outcome["label"],
                side,
                "SELL",
                "missed",
                result.reason,
                {"forecast_max": new_forecast_max_c, "bid": book.best_bid},
                event_key=event_key,
            )
            sells_missed += 1
    return RunnerResult(
        sells_filled=sells_filled,
        sells_missed=sells_missed,
        notes=tuple(notes),
    )


def process_actual_entries(db: sqlite3.Connection, today_hkt: date) -> RunnerResult:
    transitions = observed_max_increases(db, today_hkt.isoformat())
    min_transitions = observed_min_decreases(db, today_hkt.isoformat())
    aws_transitions = [
        (old, new) for old, new in transitions if _is_aws_actual_row(old, new)
    ]
    aws_min_transitions = [
        (old, new) for old, new in min_transitions if _is_aws_actual_row(old, new)
    ]
    if aws_transitions:
        transitions = aws_transitions
    if aws_min_transitions:
        min_transitions = aws_min_transitions
    if not transitions and not min_transitions:
        return RunnerResult(notes=("need two observed maxes/mins",))
    aggregate = RunnerResult()
    notes: list[str] = []
    for old, new in transitions:
        result = _process_actual_transition(db, today_hkt, old, new)
        aggregate = RunnerResult(
            buys_filled=aggregate.buys_filled + result.buys_filled,
            buys_missed=aggregate.buys_missed + result.buys_missed,
            sells_filled=aggregate.sells_filled,
            sells_missed=aggregate.sells_missed,
            signals=aggregate.signals + result.signals,
            notes=(),
        )
        notes.extend(result.notes)
    for old, new in min_transitions:
        result = _process_actual_min_transition(db, today_hkt, old, new)
        aggregate = RunnerResult(
            buys_filled=aggregate.buys_filled + result.buys_filled,
            buys_missed=aggregate.buys_missed + result.buys_missed,
            sells_filled=aggregate.sells_filled,
            sells_missed=aggregate.sells_missed,
            signals=aggregate.signals + result.signals,
            notes=(),
        )
        notes.extend(result.notes)
    if not notes:
        notes.append("no actual cross candidates")
    return RunnerResult(
        buys_filled=aggregate.buys_filled,
        buys_missed=aggregate.buys_missed,
        signals=aggregate.signals,
        notes=tuple(_dedupe_notes(notes)),
    )


def _dedupe_notes(notes: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for note in notes:
        if note in seen:
            continue
        seen.add(note)
        deduped.append(note)
    return deduped


def _is_aws_actual_row(old: sqlite3.Row, new: sqlite3.Row) -> bool:
    return old["station"] == "HKO" and new["station"] == "HKO"


def _process_actual_min_transition(
    db: sqlite3.Connection, today_hkt: date, old: sqlite3.Row, new: sqlite3.Row
) -> RunnerResult:
    old_min = float(old["since_midnight_min_c"])
    new_min = float(new["since_midnight_min_c"])
    latest_forecast = _latest_effective_forecast_value(db, today_hkt.isoformat(), "lowest")
    if latest_forecast is None:
        return RunnerResult(notes=("missing current decimal low forecast for actual check",))
    forecast_min = float(latest_forecast["forecast_value_c"])
    if old_min <= new_min:
        return RunnerResult(notes=("actual min has not decreased",))
    event_key = (
        f"actual_low_cross:{today_hkt.isoformat()}:"
        f"forecast:{forecast_min}:"
        f"{old['id']}:{old_min}->{new['id']}:{new_min}"
    )
    if has_processed_event(db, event_key):
        return RunnerResult(notes=("actual min event already processed",))

    buys_filled = 0
    buys_missed = 0
    signals = 0
    event_marked = False
    for row in _lowest_temperature_rows(list_outcomes_for_date(db, today_hkt.isoformat())):
        predicate = parse_outcome_label(row["label"])
        side = _actual_low_cross_side(predicate, old_min, new_min)
        if side is None:
            continue
        token_id = row["yes_token_id"] if side == "YES" else row["no_token_id"]
        signals += 1
        prices = latest_two_orderbook_prices(db, token_id)
        if len(prices) < 2 or prices[0]["best_ask"] is None or prices[1]["best_ask"] is None:
            store_trading_decision(
                db,
                "actual_low_cross",
                token_id,
                row["label"],
                side,
                "BUY",
                "missed",
                f"missing prior/current {side.lower()} ask",
                {"old_min": old_min, "new_min": new_min, "forecast_min": forecast_min},
                event_key=event_key,
            )
            buys_missed += 1
            continue
        prior = float(prices[1]["best_ask"])
        current = float(prices[0]["best_ask"])
        is_invalidated_bucket = side == "NO"
        stale_move_threshold = (
            None
            if is_invalidated_bucket
            else Settings.actual_new_bucket_stale_price_min_move
        )
        max_buy_price = (
            Settings.actual_invalidated_bucket_max_entry_price
            if is_invalidated_bucket
            else Settings.actual_new_bucket_max_entry_price
        )
        if not event_marked:
            _mark_event_processed(
                db,
                event_type="actual_low_cross",
                event_key=event_key,
                reason=f"observed min changed {old_min} -> {new_min}",
                details={"old_min": old_min, "new_min": new_min, "forecast_min": forecast_min},
            )
            event_marked = True
        if stale_move_threshold is not None and current - prior >= stale_move_threshold:
            store_trading_decision(
                db,
                "actual_low_cross",
                token_id,
                row["label"],
                side,
                "BUY",
                "missed",
                "price moved with actual low cross",
                {"old_min": old_min, "new_min": new_min, "forecast_min": forecast_min},
                event_key=event_key,
            )
            buys_missed += 1
            continue
        candidate = TradeCandidate(
            outcome=_outcome_from_row(row),
            side=f"BUY_{side}",
            impact=(
                DirectionalImpact.INCREASES_YES_PROBABILITY
                if side == "YES"
                else DirectionalImpact.DECREASES_YES_PROBABILITY
            ),
            price_response=PriceResponse.PRICE_NOT_MOVED_WITH_EVENT,
            prior_yes_ask=prior,
            current_yes_ask=current,
            reason=(
                "actual min crossed settling threshold before price moved"
                if side == "YES"
                else "actual min invalidated warmer bucket before price moved"
            ),
        )
        filled = _execute_candidate_buy(
            db,
            candidate,
            event_type="actual_low_cross",
            event_key=event_key,
            max_buy_price=max_buy_price,
        )
        if filled is True:
            buys_filled += 1
        elif filled is False:
            buys_missed += 1
    return RunnerResult(
        buys_filled=buys_filled,
        buys_missed=buys_missed,
        signals=signals,
        notes=(f"observed min changed {old_min} -> {new_min}",) if signals else ("no actual low cross candidates",),
    )


def _process_actual_transition(
    db: sqlite3.Connection, today_hkt: date, old: sqlite3.Row, new: sqlite3.Row
) -> RunnerResult:
    old_max = float(old["since_midnight_max_c"])
    new_max = float(new["since_midnight_max_c"])
    latest_forecast = _latest_effective_forecast_high(db, today_hkt.isoformat())
    forecast_max = (
        None if latest_forecast is None else float(latest_forecast["forecast_max_c"])
    )
    actual_details = {
        "old_max": old_max,
        "new_max": new_max,
        "forecast_max": forecast_max,
    }
    event_key = (
        f"actual_cross:{today_hkt.isoformat()}:"
        f"forecast:{forecast_max}:"
        f"{old['id']}:{old_max}->{new['id']}:{new_max}"
    )
    if has_processed_event(db, event_key):
        return RunnerResult(notes=("actual max event already processed",))

    buys_filled = 0
    buys_missed = 0
    signals = 0
    event_marked = False
    for row in _highest_temperature_rows(list_outcomes_for_date(db, today_hkt.isoformat())):
        predicate = parse_outcome_label(row["label"])
        side = _actual_cross_side(predicate, old_max, new_max)
        if side is None:
            continue
        token_id = row["yes_token_id"] if side == "YES" else row["no_token_id"]
        signals += 1
        if _actual_cross_guarded_by_forecast(predicate, side, forecast_max):
            if not event_marked:
                _mark_event_processed(
                    db,
                    event_type="actual_cross",
                    event_key=event_key,
                    reason=f"observed max changed {old_max} -> {new_max}",
                    details=actual_details,
                )
                event_marked = True
            store_trading_decision(
                db,
                "actual_cross",
                token_id,
                row["label"],
                side,
                "BUY",
                "missed",
                "forecast signal already above crossed bucket",
                actual_details,
                event_key=event_key,
            )
            buys_missed += 1
            continue
        prices = latest_two_orderbook_prices(db, token_id)
        if len(prices) < 2 or prices[0]["best_ask"] is None or prices[1]["best_ask"] is None:
            store_trading_decision(
                db,
                "actual_cross",
                token_id,
                row["label"],
                side,
                "BUY",
                "missed",
                f"missing prior/current {side.lower()} ask",
                actual_details,
                event_key=event_key,
            )
            buys_missed += 1
            continue
        prior = float(prices[1]["best_ask"])
        current = float(prices[0]["best_ask"])
        is_invalidated_bucket = side == "NO"
        stale_move_threshold = (
            None
            if is_invalidated_bucket
            else Settings.actual_new_bucket_stale_price_min_move
        )
        if not event_marked:
            _mark_event_processed(
                db,
                event_type="actual_cross",
                event_key=event_key,
                reason=f"observed max changed {old_max} -> {new_max}",
                details=actual_details,
            )
            event_marked = True
        if stale_move_threshold is not None and current - prior >= stale_move_threshold:
            store_trading_decision(
                db,
                "actual_cross",
                token_id,
                row["label"],
                side,
                "BUY",
                "missed",
                "price moved with actual cross",
                {
                    **actual_details,
                    "prior_ask": prior,
                    "current_ask": current,
                    "side": side,
                },
                event_key=event_key,
            )
            buys_missed += 1
            continue
        candidate = TradeCandidate(
            outcome=_outcome_from_row(row),
            side=f"BUY_{side}",
            impact=(
                DirectionalImpact.INCREASES_YES_PROBABILITY
                if side == "YES"
                else DirectionalImpact.DECREASES_YES_PROBABILITY
            ),
            price_response=PriceResponse.PRICE_NOT_MOVED_WITH_EVENT,
            prior_yes_ask=prior,
            current_yes_ask=current,
            reason=(
                "actual max crossed settling threshold before price moved"
                if side == "YES"
                else "actual max invalidated lower bucket before price moved"
            ),
        )
        max_buy_price = (
            Settings.actual_invalidated_bucket_max_entry_price
            if is_invalidated_bucket
            else Settings.peak_hour_actual_cross_max_yes_ask
            if _actual_cross_is_peak_hour_sure_bet(
                db, today_hkt.isoformat(), new["observed_at_hkt"], new_max
            )
            else Settings.actual_new_bucket_max_entry_price
        )
        filled = _execute_candidate_buy(
            db,
            candidate,
            event_type="actual_cross",
            event_key=event_key,
            max_buy_price=max_buy_price,
        )
        if filled is True:
            buys_filled += 1
        elif filled is False:
            buys_missed += 1
    return RunnerResult(
        buys_filled=buys_filled,
        buys_missed=buys_missed,
        signals=signals,
        notes=(f"observed max changed {old_max} -> {new_max}",) if signals else ("no actual cross candidates",),
    )


def _actual_cross_side(predicate, old_max: float, new_max: float) -> str | None:
    if predicate.value_c is None:
        return None
    if predicate.type == PredicateType.GTE_C:
        if old_max < predicate.value_c <= new_max:
            return "YES"
        return None
    if predicate.type in {PredicateType.EXACT_C, PredicateType.BOTTOM_BUCKET_LTE_C}:
        upper_boundary = predicate.value_c + 1
        if old_max < upper_boundary <= new_max:
            return "NO"
    return None


def _actual_cross_guarded_by_forecast(
    predicate, side: str, forecast_max: float | None
) -> bool:
    if side != "YES" or predicate.value_c is None or forecast_max is None:
        return False
    return forecast_max >= predicate.value_c + 1


def _actual_cross_is_peak_hour_sure_bet(
    db: sqlite3.Connection,
    target_date_hkt: str,
    observed_at_hkt: str,
    actual_max_c: float,
) -> bool:
    relevant = _latest_hourly_forecast_values(db, target_date_hkt)
    if not relevant:
        return False
    observed_hour = _hkt_hour(observed_at_hkt)
    if observed_hour is None:
        return False
    peak = max(value for _, value in relevant)
    if actual_max_c < peak:
        return False
    peak_hours = [hour for hour, value in relevant if value >= peak]
    if observed_hour not in peak_hours:
        return False
    future_values = [value for hour, value in relevant if hour > observed_hour]
    if not future_values:
        return False
    return max(future_values) < peak


def _hkt_hour(value: str) -> int | None:
    try:
        return datetime.fromisoformat(value).astimezone(HKT).hour
    except ValueError:
        return None


def _actual_low_cross_side(predicate, old_min: float, new_min: float) -> str | None:
    if predicate.value_c is None:
        return None
    lower = predicate.value_c
    upper = predicate.value_c + 1
    if predicate.type == PredicateType.GTE_C:
        if old_min >= lower > new_min:
            return "NO"
        return None
    if predicate.type == PredicateType.BOTTOM_BUCKET_LTE_C:
        if old_min >= upper > new_min:
            return "YES"
        return None
    if predicate.type == PredicateType.EXACT_C:
        if old_min >= upper and lower <= new_min < upper:
            return "YES"
        if old_min >= lower > new_min:
            return "NO"
    return None



def process_open_position_exits(
    db: sqlite3.Connection, today_hkt: date | None = None
) -> RunnerResult:
    today = today_hkt or datetime.now(HKT).date()
    sells_filled = 0
    sells_missed = 0
    notes: list[str] = []
    positions = list_open_live_positions(db) if _LIVE_CLIENT is not None else list_open_paper_positions(db)
    for pos in positions:
        token_id = pos["outcome_id"]
        outcome = find_outcome_by_token(db, token_id)
        if outcome is None:
            store_trading_decision(
                db, "exit_check", token_id, None, None, "SELL", "missed", "unknown token"
            )
            sells_missed += 1
            continue
        side = "YES" if token_id == outcome["yes_token_id"] else "NO"
        actual_applies = outcome["target_date_hkt"] == today.isoformat()
        market_kind = _temperature_market_kind_for_row(outcome)
        latest_actual = None
        actual_value_for_outcome = None
        if actual_applies and market_kind == "lowest":
            latest_actual = latest_observed_min_for_date(db, outcome["target_date_hkt"])
            actual_value_for_outcome = (
                float(latest_actual["since_midnight_min_c"]) if latest_actual else None
            )
        elif actual_applies:
            latest_actual = latest_observed_max_for_date(db, outcome["target_date_hkt"])
            actual_value_for_outcome = (
                float(latest_actual["since_midnight_max_c"]) if latest_actual else None
            )
        invalidated = _position_invalidated(outcome, side, actual_value_for_outcome)
        hourly_reason = _hourly_forecast_invalidation_reason(db, outcome, side)
        hold_to_maturity = _position_can_hold_to_maturity(
            outcome, side, actual_value_for_outcome
        )
        if hold_to_maturity:
            notes.append(f"holding settled {outcome['label']} {side}")
            continue

        try:
            book = latest_orderbook(db, token_id)
        except ValueError:
            store_trading_decision(
                db,
                "exit_check",
                token_id,
                outcome["label"],
                side,
                "SELL",
                "missed",
                "missing orderbook",
            )
            sells_missed += 1
            continue
        if book.best_bid is None:
            store_trading_decision(
                db,
                "exit_check",
                token_id,
                outcome["label"],
                side,
                "SELL",
                "missed",
                "no bid depth",
            )
            sells_missed += 1
            continue
        if not invalidated and hourly_reason is None:
            continue
        reason = hourly_reason or (
            "position invalidated by observed min"
            if market_kind == "lowest"
            else "position invalidated by observed max"
        )
        if _LIVE_CLIENT is not None:
            result = execute_live_sell(
                db,
                _LIVE_CLIENT,
                token_id=token_id,
                bids=book.bids,
                reason=reason,
                label=outcome["label"],
                event_type="exit_check",
            )
        else:
            result = execute_paper_sell(db, token_id, book.bids, reason)
        if result.status == "filled":
            store_trading_decision(
                db,
                "exit_check",
                token_id,
                outcome["label"],
                side,
                "SELL",
                "filled",
                reason,
                {
                    "current_value": actual_value_for_outcome,
                    "bid": book.best_bid,
                    "hourly_forecast_reason": hourly_reason,
                },
            )
            sells_filled += 1
        else:
            store_trading_decision(
                db,
                "exit_check",
                token_id,
                outcome["label"],
                side,
                "SELL",
                "missed",
                result.reason,
                {
                    "current_value": actual_value_for_outcome,
                    "bid": book.best_bid,
                    "hourly_forecast_reason": hourly_reason,
                },
            )
            sells_missed += 1
    return RunnerResult(sells_filled=sells_filled, sells_missed=sells_missed, notes=tuple(notes))


def build_forecast_move_candidates(
    outcomes: list[Outcome],
    old_forecast_max_c: float,
    new_forecast_max_c: float,
    prior_yes_asks: dict[str, float],
    current_yes_asks: dict[str, float],
    max_move: float,
) -> list[TradeCandidate]:
    candidates: list[TradeCandidate] = []
    if old_forecast_max_c == new_forecast_max_c:
        return candidates
    if floor(old_forecast_max_c) == floor(new_forecast_max_c):
        return candidates
    moved_up = new_forecast_max_c > old_forecast_max_c
    for outcome in outcomes:
        prior = prior_yes_asks.get(outcome.market_id)
        current = current_yes_asks.get(outcome.market_id)
        if prior is None or current is None:
            continue
        side = _forecast_move_side(outcome, old_forecast_max_c, new_forecast_max_c, moved_up)
        if side is None:
            continue
        impact = (
            DirectionalImpact.INCREASES_YES_PROBABILITY
            if side == "BUY_YES"
            else DirectionalImpact.DECREASES_YES_PROBABILITY
        )
        price_move = current - prior if side == "BUY_YES" else prior - current
        response = PriceResponse.PRICE_NOT_MOVED_WITH_EVENT
        if price_move > max_move:
            response = PriceResponse.PRICE_MOVED_WITH_EVENT
        if response != PriceResponse.PRICE_NOT_MOVED_WITH_EVENT:
            continue
        candidates.append(
            TradeCandidate(
                outcome=outcome,
                side=side,
                impact=impact,
                price_response=response,
                prior_yes_ask=prior,
                current_yes_ask=current,
                reason="price has not moved with HKO event",
            )
        )
    return candidates


def _forecast_move_side(
    outcome: Outcome,
    old_forecast_max_c: float,
    new_forecast_max_c: float,
    moved_up: bool,
) -> str | None:
    predicate = outcome.predicate
    old_matches = predicate_matches(predicate, old_forecast_max_c)
    new_matches = predicate_matches(predicate, new_forecast_max_c)
    if new_matches and not old_matches:
        return "BUY_YES"
    if predicate.value_c is None:
        return None
    if moved_up:
        if predicate.type in {PredicateType.EXACT_C, PredicateType.BOTTOM_BUCKET_LTE_C}:
            if predicate.value_c < new_forecast_max_c:
                return "BUY_NO"
        return None
    if predicate.type in {PredicateType.EXACT_C, PredicateType.GTE_C}:
        if predicate.value_c > new_forecast_max_c:
            return "BUY_NO"
    return None


def _latest_yes_books_by_market(db: sqlite3.Connection, rows: list[sqlite3.Row]) -> dict[str, object]:
    books = {}
    for row in rows:
        try:
            books[row["polymarket_market_id"]] = latest_orderbook(db, row["yes_token_id"])
        except ValueError:
            continue
    return books


def _forecast_matching_row(
    rows: list[sqlite3.Row], forecast_high: float
) -> sqlite3.Row | None:
    for row in rows:
        if predicate_matches(parse_outcome_label(row["label"]), forecast_high):
            return row
    return None


def _favorite_yes_row(
    rows: list[sqlite3.Row], books: dict[str, object]
) -> sqlite3.Row | None:
    best_row = None
    best_price = None
    for row in rows:
        book = books.get(row["polymarket_market_id"])
        if book is None:
            continue
        price = _implied_yes_price(book)
        if price is None:
            continue
        if best_price is None or price > best_price:
            best_price = price
            best_row = row
    return best_row


def _implied_yes_price(book) -> float | None:
    if book.best_bid is not None and book.best_ask is not None:
        return (book.best_bid + book.best_ask) / 2
    if book.best_bid is not None:
        return book.best_bid
    return book.best_ask


def _latest_effective_forecast_high(
    db: sqlite3.Connection, forecast_date_hkt: str
) -> dict | None:
    rows = _effective_forecast_rows(db, forecast_date_hkt)
    return rows[0] if rows else None


def _latest_effective_forecast_value(
    db: sqlite3.Connection, forecast_date_hkt: str, market_kind: str
) -> dict | None:
    rows = _effective_forecast_rows(db, forecast_date_hkt, market_kind=market_kind)
    return rows[0] if rows else None


def _latest_two_effective_forecast_highs(
    db: sqlite3.Connection, forecast_date_hkt: str
) -> list[dict]:
    return _effective_forecast_rows(db, forecast_date_hkt, limit=2)


def _ocf_forecast_stale_reason(
    db: sqlite3.Connection,
    forecast_date_hkt: str,
    now_utc: datetime | None = None,
) -> str | None:
    row = db.execute(
        """
        select fetched_at_utc
        from ocf_forecast_samples
        where forecast_date_hkt = ?
        order by fetched_at_utc desc, id desc
        limit 1
        """,
        (forecast_date_hkt,),
    ).fetchone()
    if row is None:
        return None
    try:
        fetched_at = datetime.fromisoformat(row["fetched_at_utc"])
    except (TypeError, ValueError):
        return "OCF forecast sample has invalid fetch timestamp"
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    now = now_utc or datetime.now(timezone.utc)
    age_minutes = (now - fetched_at.astimezone(timezone.utc)).total_seconds() / 60
    if age_minutes >= Settings.ocf_forecast_freshness_max_age_minutes:
        return (
            "OCF forecast sample stale "
            f"age={age_minutes:.1f}m >= "
            f"{Settings.ocf_forecast_freshness_max_age_minutes:.0f}m"
        )
    return None


def _effective_forecast_rows(
    db: sqlite3.Connection,
    forecast_date_hkt: str,
    limit: int = 1,
    market_kind: str = "highest",
) -> list[dict]:
    actual_value = _latest_actual_value_for_effective_forecast(
        db, forecast_date_hkt, market_kind
    )
    rows = db.execute(
        """
        select id, forecast_date_hkt, fetched_at_utc, forecast_min_c, forecast_max_c,
               raw_min_c, raw_max_c,
               hourly_temperatures_json, raw_daily_forecast
        from ocf_forecast_samples
        where forecast_date_hkt = ?
        order by fetched_at_utc desc, id desc
        """,
        (forecast_date_hkt,),
    )
    result: list[dict] = []
    seen_update_times: set[str] = set()
    for row in rows:
        effective_value = _effective_forecast_value_from_sample(row, market_kind)
        if effective_value is None:
            continue
        if actual_value is not None:
            effective_value = (
                min(actual_value, effective_value)
                if market_kind == "lowest"
                else max(actual_value, effective_value)
            )
        update_time = _forecast_sample_update_time(row)
        if update_time in seen_update_times:
            continue
        seen_update_times.add(update_time)
        result.append(
            {
                "id": row["id"],
                "forecast_date_hkt": row["forecast_date_hkt"],
                "forecast_max_c": effective_value,
                "forecast_value_c": effective_value,
                "update_time": update_time,
            }
        )
        if len(result) >= limit:
            break
    return result


def _latest_actual_value_for_effective_forecast(
    db: sqlite3.Connection, forecast_date_hkt: str, market_kind: str
) -> float | None:
    row = (
        latest_observed_min_for_date(db, forecast_date_hkt)
        if market_kind == "lowest"
        else latest_observed_max_for_date(db, forecast_date_hkt)
    )
    if row is None:
        return None
    column = "since_midnight_min_c" if market_kind == "lowest" else "since_midnight_max_c"
    return None if row[column] is None else float(row[column])


def _highest_temperature_rows(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    return [
        row
        for row in rows
        if str(row["slug"]).startswith("highest-temperature-in-hong-kong-on-")
    ]


def _lowest_temperature_rows(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    return [
        row
        for row in rows
        if str(row["slug"]).startswith("lowest-temperature-in-hong-kong-on-")
    ]


def _temperature_market_kind_for_row(row: sqlite3.Row) -> str:
    slug = str(row["slug"])
    if slug.startswith("lowest-temperature-in-hong-kong-on-"):
        return "lowest"
    return "highest"


def _effective_forecast_value_from_sample(
    row: sqlite3.Row, market_kind: str
) -> float | None:
    hourly_values = [
        value
        for _, value in _hourly_values_from_json(
            row["forecast_date_hkt"], row["hourly_temperatures_json"]
        )
    ]
    if hourly_values:
        return min(hourly_values) if market_kind == "lowest" else max(hourly_values)
    if market_kind == "lowest":
        return _as_float(row["raw_min_c"])
    return _as_float(row["raw_max_c"])


def _effective_forecast_max_from_sample(row: sqlite3.Row) -> float | None:
    hourly_values = [
        value
        for _, value in _hourly_values_from_json(
            row["forecast_date_hkt"], row["hourly_temperatures_json"]
        )
    ]
    if hourly_values:
        return max(hourly_values)
    return _as_float(row["raw_max_c"])


def _forecast_sample_update_time(row: sqlite3.Row) -> str:
    try:
        raw = json.loads(row["raw_daily_forecast"] or "{}")
    except json.JSONDecodeError:
        raw = {}
    last_modified = raw.get("LastModified") if isinstance(raw, dict) else None
    return str(last_modified or row["fetched_at_utc"] or row["id"])


def _merge_runner_results(first: RunnerResult, second: RunnerResult) -> RunnerResult:
    return RunnerResult(
        buys_filled=first.buys_filled + second.buys_filled,
        buys_missed=first.buys_missed + second.buys_missed,
        sells_filled=first.sells_filled + second.sells_filled,
        sells_missed=first.sells_missed + second.sells_missed,
        signals=first.signals + second.signals,
        notes=first.notes + second.notes,
    )


def _forecast_change_max_entry_price(target_date: date, today_hkt: date) -> float:
    lead_days = (target_date - today_hkt).days
    if lead_days >= 2:
        return Settings.forecast_change_d2_max_entry_price
    return Settings.forecast_change_max_entry_price


def run_paper_loop(
    db: sqlite3.Connection,
    tick_seconds: float,
    max_ticks: int | None = None,
    today_hkt: date | None = None,
) -> None:
    tick = 0
    while max_ticks is None or tick < max_ticks:
        result = run_paper_tick(db, today_hkt=today_hkt)
        print(
            "paper-tick "
            f"buys={result.buys_filled}/{result.buys_missed} "
            f"sells={result.sells_filled}/{result.sells_missed} "
            f"signals={result.signals} notes={'; '.join(result.notes)}"
        )
        tick += 1
        if max_ticks is None or tick < max_ticks:
            time.sleep(tick_seconds)


def render_dashboard(db: sqlite3.Connection) -> str:
    stats = dashboard_stats(db)
    forecast = stats["latest_forecast"] or {}
    observation = stats["latest_observation"] or {}
    counts = stats["counts"]
    lines = [
        "HK high-temp paper dashboard",
        f"latest forecast date: {forecast.get('forecast_date_hkt', 'n/a')}",
        f"latest forecast high: {forecast.get('forecast_max_c', 'n/a')}",
        f"forecast update time: {forecast.get('update_time', 'n/a')}",
        f"latest since-midnight max: {observation.get('since_midnight_max_c', 'n/a')}",
        f"observation time: {observation.get('observed_at_hkt', 'n/a')}",
        f"unique HKO forecasts ingested: {counts['hko_forecasts']}",
        f"markets/outcomes: {counts['markets']}/{counts['outcomes']}",
        f"orderbook snapshots: {counts['orderbooks']}",
        f"buy orders filled/missed: {counts['buy_filled']}/{counts['buy_missed']}",
        f"sell orders filled/missed: {counts['sell_filled']}/{counts['sell_missed']}",
        f"open positions: {stats['open_positions']}",
        f"realized PnL: ${stats['realized_pnl']:.2f}",
        f"executable unrealized PnL: ${stats['executable_unrealized_pnl']:.2f}",
        f"total profit estimate: ${stats['total_profit']:.2f}",
        f"worst-case open loss: ${stats['worst_case_open_loss']:.2f}",
    ]
    return "\n".join(lines)


def _execute_candidate_buy(
    db: sqlite3.Connection,
    candidate: TradeCandidate,
    event_type: str = "forecast_change",
    event_key: str | None = None,
    max_buy_price: float | None = None,
    allow_existing_position: bool = False,
    size_usd: float | None = None,
) -> bool | None:
    side = "YES" if candidate.side == "BUY_YES" else "NO"
    token_id = candidate.outcome.yes_token_id if side == "YES" else candidate.outcome.no_token_id
    outcome_row = find_outcome_by_token(db, token_id)
    actual_guard_reason = (
        _actual_entry_guard_reason(db, outcome_row, side)
        if outcome_row is not None
        else None
    )
    if actual_guard_reason is not None:
        store_trading_decision(
            db,
            event_type,
            token_id,
            candidate.outcome.label,
            side,
            "BUY",
            "ignored",
            actual_guard_reason,
            event_key=event_key,
        )
        return None
    forecast_guard_reason = (
        _forecast_value_guard_reason(db, outcome_row["target_date_hkt"], outcome_row, side)
        if outcome_row is not None
        and event_type in {
            "forecast_change",
            "lowest_forecast_change",
            "forecast_value",
            "highest_forecast_value",
            "lowest_forecast_value",
        }
        else None
    )
    if forecast_guard_reason is not None:
        store_trading_decision(
            db,
            event_type,
            token_id,
            candidate.outcome.label,
            side,
            "BUY",
            "ignored",
            forecast_guard_reason,
            event_key=event_key,
        )
        return None
    if _LIVE_CLIENT is not None:
        existing = get_live_position(db, token_id)
        if existing is not None and float(existing["net_shares"]) <= 0:
            existing = None
    else:
        existing = db.execute(
            "select net_shares from paper_positions where outcome_id = ? and net_shares > 0",
            (token_id,),
        ).fetchone()
    if existing and not allow_existing_position:
        store_trading_decision(
            db,
            event_type,
            token_id,
            candidate.outcome.label,
            side,
            "BUY",
            "ignored",
            "duplicate open position",
            event_key=event_key,
        )
        return None
    base_order_cap = _LIVE_ORDER_CAP_USD if _LIVE_ORDER_CAP_USD is not None else Settings.max_order_usd
    requested_size = base_order_cap if size_usd is None else min(size_usd, base_order_cap)
    if requested_size <= Settings.dust_order_epsilon_usd:
        store_trading_decision(
            db,
            event_type,
            token_id,
            candidate.outcome.label,
            side,
            "BUY",
            "ignored",
            "position budget reached",
            event_key=event_key,
        )
        return None
    try:
        book = latest_orderbook(db, token_id)
    except ValueError:
        if _LIVE_CLIENT is None:
            store_trading_decision(
                db,
                event_type,
                token_id,
                candidate.outcome.label,
                side,
                "BUY",
                "missed",
                "missing orderbook",
                event_key=event_key,
            )
            return False
        book = None
    reference_best_ask = book.best_ask if book is not None else None
    if _LIVE_CLIENT is not None:
        try:
            book = fetch_orderbook(token_id)
            store_orderbook(db, token_id, book)
        except Exception as exc:
            store_trading_decision(
                db,
                event_type,
                token_id,
                candidate.outcome.label,
                side,
                "BUY",
                "missed",
                f"fresh orderbook fetch failed: {type(exc).__name__}",
                event_key=event_key,
            )
            return False
    hard_entry_cap = max(Settings.max_entry_price, max_buy_price or 0)
    if book.best_ask is not None and book.best_ask > hard_entry_cap:
        store_trading_decision(
            db,
            event_type,
            token_id,
            candidate.outcome.label,
            side,
            "BUY",
            "missed",
            "entry price above max",
            {
                "best_ask": book.best_ask,
                "max_entry_price": hard_entry_cap,
                "impact": candidate.impact.value,
                "prior_yes_ask": candidate.prior_yes_ask,
                "current_yes_ask": candidate.current_yes_ask,
            },
            event_key=event_key,
        )
        return False
    dynamic_max_buy_price = max_buy_price
    slippage_anchor = reference_best_ask if reference_best_ask is not None else book.best_ask
    if slippage_anchor is not None:
        slippage_cap = slippage_anchor + Settings.max_entry_limit_slippage
        dynamic_max_buy_price = (
            slippage_cap
            if dynamic_max_buy_price is None
            else min(dynamic_max_buy_price, slippage_cap)
        )
    if _LIVE_CLIENT is not None:
        result = execute_live_buy(
            db,
            _LIVE_CLIENT,
            token_id=token_id,
            side=side,
            size_usd=requested_size,
            asks=book.asks,
            reason=candidate.reason,
            max_price=dynamic_max_buy_price,
            min_fill_usd=min(requested_size, Settings.min_entry_fill_usd),
            order_cap_usd=base_order_cap,
            label=candidate.outcome.label,
            event_type=event_type,
            event_key=event_key,
        )
    else:
        result = execute_paper_buy(
            db,
            token_id=token_id,
            side=side,
            size_usd=requested_size,
            asks=book.asks,
            max_order_usd=Settings.max_order_usd,
            reason=candidate.reason,
            max_price=dynamic_max_buy_price,
            min_fill_usd=min(requested_size, Settings.min_entry_fill_usd),
        )
    status = "filled" if result.status == "filled" else "missed"
    store_trading_decision(
        db,
        event_type,
        token_id,
        candidate.outcome.label,
        side,
        "BUY",
        status,
        result.reason,
        {
            "impact": candidate.impact.value,
            "prior_yes_ask": candidate.prior_yes_ask,
            "current_yes_ask": candidate.current_yes_ask,
        },
        event_key=event_key,
    )
    return result.status == "filled"


def _remaining_position_budget(db: sqlite3.Connection, token_id: str) -> float:
    if _LIVE_CLIENT is not None:
        position = get_live_position(db, token_id)
        cap = _LIVE_ORDER_CAP_USD if _LIVE_ORDER_CAP_USD is not None else Settings.live_scheduler_order_cap_usd
    else:
        position = db.execute(
            "select net_shares, avg_price from paper_positions where outcome_id = ?",
            (token_id,),
        ).fetchone()
        cap = Settings.max_order_usd
    if position is None:
        return cap
    invested = float(position["net_shares"]) * float(position["avg_price"])
    return max(cap - invested, 0.0)


def _actual_entry_guard_reason(
    db: sqlite3.Connection, outcome: sqlite3.Row, side: str
) -> str | None:
    target_date_hkt = outcome["target_date_hkt"]
    market_kind = _temperature_market_kind_for_row(outcome)
    latest_actual = (
        latest_observed_min_for_date(db, target_date_hkt)
        if market_kind == "lowest"
        else latest_observed_max_for_date(db, target_date_hkt)
    )
    if latest_actual is None:
        return None
    current_value = (
        float(latest_actual["since_midnight_min_c"])
        if market_kind == "lowest"
        else float(latest_actual["since_midnight_max_c"])
    )
    if not _position_invalidated(outcome, side, current_value):
        return None
    return (
        "entry invalidated by observed min"
        if market_kind == "lowest"
        else "entry invalidated by observed max"
    )


def _forecast_value_guard_reason(
    db: sqlite3.Connection,
    target_date_hkt: str,
    outcome: sqlite3.Row,
    side: str,
) -> str | None:
    if side != "YES":
        return None
    predicate = parse_outcome_label(outcome["label"])
    if predicate.value_c is None:
        return None
    market_kind = _temperature_market_kind_for_row(outcome)
    if market_kind == "lowest":
        return _lowest_forecast_value_guard_reason(db, target_date_hkt, predicate)
    bucket_floor = float(predicate.value_c)
    rows = _latest_hourly_forecast_for_date(db, target_date_hkt)
    relevant = _hourly_values_from_items(target_date_hkt, rows)
    if not relevant:
        return None

    if max(value for _, value in relevant) < bucket_floor:
        return "hourly forecast below bucket guard"

    if _late_day_forecast_guard_applies(relevant, bucket_floor):
        return "late-day forecast peak guard"

    return None


def _lowest_forecast_value_guard_reason(
    db: sqlite3.Connection,
    target_date_hkt: str,
    predicate,
) -> str | None:
    rows = _latest_hourly_forecast_for_date(db, target_date_hkt)
    relevant = _hourly_values_from_items(target_date_hkt, rows)
    if not relevant:
        return None
    forecast_min = min(value for _, value in relevant)
    if not predicate_matches(predicate, forecast_min):
        return "hourly forecast does not reach low bucket guard"
    return None


def _hourly_forecast_invalidation_reason(
    db: sqlite3.Connection,
    outcome: sqlite3.Row,
    side: str,
) -> str | None:
    predicate = parse_outcome_label(outcome["label"])
    if predicate.value_c is None:
        return None
    market_kind = _temperature_market_kind_for_row(outcome)
    if _ocf_forecast_stale_reason(db, outcome["target_date_hkt"]) is not None:
        return None
    relevant = _latest_hourly_forecast_values(db, outcome["target_date_hkt"])
    latest = (
        None
        if relevant
        else _latest_effective_forecast_value(
            db, outcome["target_date_hkt"], market_kind
        )
    )
    if not relevant and latest is None:
        return None
    forecast_value = (
        (min(value for _, value in relevant) if market_kind == "lowest" else max(value for _, value in relevant))
        if relevant
        else float(latest["forecast_value_c"])
    )
    matches_forecast = predicate_matches(predicate, forecast_value)
    if side == "YES":
        if not matches_forecast:
            return (
                "position invalidated by hourly forecast"
                if relevant
                else "position invalidated by decimal forecast"
            )
        if market_kind == "highest" and _late_day_forecast_guard_applies(
            relevant, float(predicate.value_c)
        ):
            return "late-day forecast peak guard"
        return None
    if side == "NO" and matches_forecast:
        return (
            "position invalidated by hourly forecast"
            if relevant
            else "position invalidated by decimal forecast"
        )
    return None


def _latest_hourly_forecast_values(
    db: sqlite3.Connection, target_date_hkt: str
) -> list[tuple[int, float]]:
    rows = _latest_hourly_forecast_for_date(db, target_date_hkt)
    return _hourly_values_from_items(target_date_hkt, rows)


def _hourly_values_from_json(
    target_date_hkt: str, hourly_temperatures_json: str | None
) -> list[tuple[int, float]]:
    try:
        rows = json.loads(hourly_temperatures_json or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(rows, list):
        return []
    return _hourly_values_from_items(target_date_hkt, rows)


def _hourly_values_from_items(
    target_date_hkt: str, rows: list[dict]
) -> list[tuple[int, float]]:
    relevant: list[tuple[int, float]] = []
    for item in rows:
        hour_text = str(item.get("forecast_hour_hkt") or "")
        if not hour_text.startswith(target_date_hkt) or len(hour_text) < 13:
            continue
        value = _as_float(item.get("temperature_c"))
        if value is None:
            continue
        try:
            hour = int(hour_text[11:13])
        except ValueError:
            continue
        relevant.append((hour, value))
    return relevant


def _late_day_forecast_guard_applies(
    relevant: list[tuple[int, float]],
    bucket_floor: float,
    late_start_hour: int = 21,
) -> bool:
    if not relevant:
        return False

    breach_hours = [
        hour for hour, value in sorted(relevant, key=lambda item: item[0])
        if value >= bucket_floor
    ]
    if not breach_hours:
        return False
    return breach_hours[0] >= late_start_hour


def _latest_hourly_forecast_for_date(
    db: sqlite3.Connection, target_date_hkt: str
) -> list[dict]:
    row = db.execute(
        """
        select hourly_temperatures_json
        from ocf_forecast_samples
        where forecast_date_hkt = ?
          and hourly_temperatures_json is not null
          and hourly_temperatures_json != '[]'
        order by fetched_at_utc desc, id desc
        limit 1
        """,
        (target_date_hkt,),
    ).fetchone()
    if row is None:
        return []
    try:
        payload = json.loads(row["hourly_temperatures_json"] or "[]")
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mark_event_processed(
    db: sqlite3.Connection,
    event_type: str,
    event_key: str,
    reason: str,
    details: dict,
) -> None:
    store_trading_decision(
        db,
        event_type,
        None,
        None,
        None,
        "EVENT",
        "processed",
        reason,
        details,
        event_key=event_key,
    )


def _outcome_from_row(row: sqlite3.Row) -> Outcome:
    return Outcome(
        market_id=str(row["polymarket_market_id"]),
        label=str(row["label"]),
        predicate=parse_outcome_label(str(row["label"])),
        yes_token_id=str(row["yes_token_id"]),
        no_token_id=str(row["no_token_id"]),
    )


def _position_invalidated(outcome: sqlite3.Row, side: str, current_value: float | None) -> bool:
    if current_value is None:
        return False
    predicate = parse_outcome_label(outcome["label"])
    if _temperature_market_kind_for_row(outcome) == "lowest":
        if side == "NO":
            return predicate_matches(predicate, current_value)
        if predicate.value_c is None:
            return False
        if predicate.type.value == "GTE_C":
            return current_value < predicate.value_c
        if predicate.type.value == "EXACT_C":
            return current_value < predicate.value_c
        return False
    if side == "NO":
        return predicate_matches(predicate, current_value)
    if predicate.value_c is None:
        return False
    if predicate.type.value == "EXACT_C":
        return current_value >= predicate.value_c + 1
    if predicate.type.value == "BOTTOM_BUCKET_LTE_C":
        return current_value >= predicate.value_c + 1
    return False


def _position_invalidated_by_forecast(
    outcome: sqlite3.Row, side: str, forecast_value: float
) -> bool:
    predicate = parse_outcome_label(outcome["label"])
    matches_forecast = predicate_matches(predicate, forecast_value)
    if side == "YES":
        return not matches_forecast
    if side == "NO":
        return matches_forecast
    return False


def _position_can_hold_to_maturity(
    outcome: sqlite3.Row, side: str, current_value: float | None
) -> bool:
    if side != "YES" or current_value is None:
        return False
    predicate = parse_outcome_label(outcome["label"])
    if _temperature_market_kind_for_row(outcome) == "lowest":
        return predicate.type.value == "BOTTOM_BUCKET_LTE_C" and predicate_matches(
            predicate, current_value
        )
    return predicate.type.value == "GTE_C" and predicate_matches(predicate, current_value)
