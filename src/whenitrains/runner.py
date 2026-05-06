from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .config import Settings
from .engine import TradeCandidate
from .hko import HKT
from .markets import PredicateType, parse_outcome_label, predicate_matches
from .paper_db import execute_paper_buy, execute_paper_sell
from .polymarket import Outcome
from .signals import DirectionalImpact, PriceResponse
from .storage import (
    dashboard_stats,
    find_outcome_by_token,
    has_processed_event,
    latest_orderbook,
    latest_forecast_high,
    latest_observed_max_for_date,
    latest_two_forecast_highs,
    latest_two_orderbook_prices,
    list_open_paper_positions,
    list_outcomes_for_date,
    list_tradeable_forecast_dates,
    observed_max_increases,
    store_paper_decision,
    store_signal,
)


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
    latest = latest_two_forecast_highs(db, target_date.isoformat())
    value_result = process_forecast_value_entry(db, target_date, today)
    if len(latest) < 2:
        return _merge_runner_results(
            value_result, RunnerResult(notes=("need two forecast highs",))
        )
    new, old = latest[0], latest[1]
    if new["forecast_date_hkt"] != target_date.isoformat():
        return _merge_runner_results(
            value_result, RunnerResult(notes=("latest forecast is not for target day",))
        )
    old_high = float(old["forecast_max_c"])
    new_high = float(new["forecast_max_c"])
    if old_high == new_high:
        return _merge_runner_results(
            value_result, RunnerResult(notes=("forecast high unchanged",))
        )
    event_key = (
        f"forecast_change:{target_date.isoformat()}:"
        f"{old['id']}:{old_high}->{new['id']}:{new_high}"
    )
    if has_processed_event(db, event_key):
        return _merge_runner_results(
            value_result, RunnerResult(notes=("forecast change already processed",))
        )
    _mark_event_processed(
        db,
        event_type="forecast_change",
        event_key=event_key,
        reason=f"forecast high changed {old_high} -> {new_high}",
        details={"old_high": old_high, "new_high": new_high},
    )
    exit_result = process_forecast_position_exits(
        db, target_date, new_high, event_key=event_key
    )

    rows = list_outcomes_for_date(db, target_date.isoformat())
    outcomes = [_outcome_from_row(row) for row in rows]
    prior_yes_asks: dict[str, float] = {}
    current_yes_asks: dict[str, float] = {}
    for row in rows:
        prices = latest_two_orderbook_prices(db, row["yes_token_id"])
        if len(prices) < 2 or prices[0]["best_ask"] is None or prices[1]["best_ask"] is None:
            store_paper_decision(
                db,
                "forecast_change",
                row["yes_token_id"],
                row["label"],
                "YES",
                "BUY",
                "missed",
                "missing prior/current yes ask",
                {"old_high": old_high, "new_high": new_high},
                event_key=event_key,
            )
            continue
        current_yes_asks[row["polymarket_market_id"]] = float(prices[0]["best_ask"])
        prior_yes_asks[row["polymarket_market_id"]] = float(prices[1]["best_ask"])

    candidates = build_forecast_move_candidates(
        outcomes=outcomes,
        old_forecast_max_c=old_high,
        new_forecast_max_c=new_high,
        prior_yes_asks=prior_yes_asks,
        current_yes_asks=current_yes_asks,
        max_move=Settings.forecast_change_max_price_move,
    )
    if not candidates:
        return _merge_runner_results(
            value_result,
            RunnerResult(
                buys_missed=1,
                sells_filled=exit_result.sells_filled,
                sells_missed=exit_result.sells_missed,
                notes=("no stale forecast candidates",) + exit_result.notes,
            ),
        )

    store_signal(
        db,
        market_id=target_date.isoformat(),
        trigger_type="forecast_change",
        current_max_c=None,
        forecast_max_c=new_high,
        affected_outcomes={candidate.outcome.label: candidate.impact.value for candidate in candidates},
        price_response={candidate.outcome.label: candidate.price_response.value for candidate in candidates},
        notes=f"forecast high changed {old_high} -> {new_high}",
    )

    buys_filled = 0
    buys_missed = 0
    for candidate in candidates:
        filled = _execute_candidate_buy(
            db,
            candidate,
            event_key=event_key,
            max_buy_price=Settings.forecast_change_max_entry_price,
        )
        if filled is True:
            buys_filled += 1
        elif filled is False:
            buys_missed += 1
    return _merge_runner_results(
        value_result,
        RunnerResult(
        buys_filled=buys_filled,
        buys_missed=buys_missed,
        sells_filled=exit_result.sells_filled,
        sells_missed=exit_result.sells_missed,
        signals=1,
        notes=(f"forecast high changed {old_high} -> {new_high}",) + exit_result.notes,
        ),
    )


def process_forecast_value_entry(
    db: sqlite3.Connection, target_date: date, today_hkt: date
) -> RunnerResult:
    lead_days = (target_date - today_hkt).days
    if lead_days < 0:
        return RunnerResult(
            notes=(f"forecast value skipped: {target_date.isoformat()} is before today",)
        )
    if lead_days > Settings.forecast_value_max_lead_days:
        return RunnerResult(
            notes=(
                "forecast value skipped: "
                f"{target_date.isoformat()} lead_days={lead_days} "
                f"> max={Settings.forecast_value_max_lead_days}",
            )
        )
    latest = latest_forecast_high(db, target_date.isoformat())
    if latest is None:
        return RunnerResult(
            notes=(f"forecast value skipped: {target_date.isoformat()} missing latest forecast",)
        )
    forecast_high = float(latest["forecast_max_c"])
    rows = list_outcomes_for_date(db, target_date.isoformat())
    if not rows:
        return RunnerResult(
            notes=(f"forecast value skipped: {target_date.isoformat()} missing outcomes",)
        )
    books = _latest_yes_books_by_market(db, rows)
    forecast_row = _forecast_matching_row(rows, forecast_high)
    if forecast_row is None:
        return RunnerResult(
            notes=(
                "forecast value skipped: "
                f"{target_date.isoformat()} forecast_high={forecast_high:g} missing matching bucket",
            )
        )
    forecast_book = books.get(forecast_row["polymarket_market_id"])
    if forecast_book is None or forecast_book.best_ask is None:
        return RunnerResult(
            notes=(
                "forecast value skipped: "
                f"{target_date.isoformat()} {forecast_row['label']} missing YES ask",
            )
        )
    if forecast_book.best_ask > Settings.forecast_value_max_yes_ask:
        return RunnerResult(
            notes=(
                "forecast value skipped: "
                f"{target_date.isoformat()} {forecast_row['label']} "
                f"ask={forecast_book.best_ask:.3f} "
                f"> cheap_threshold={Settings.forecast_value_max_yes_ask:.3f}",
            )
        )
    favorite = _favorite_yes_row(rows, books)
    if favorite is None:
        return RunnerResult(
            notes=(f"forecast value skipped: {target_date.isoformat()} missing market favorite",)
        )
    favorite_predicate = parse_outcome_label(favorite["label"])
    forecast_predicate = parse_outcome_label(forecast_row["label"])
    if favorite_predicate.value_c is None or forecast_predicate.value_c is None:
        return RunnerResult(
            notes=(
                "forecast value skipped: "
                f"{target_date.isoformat()} unparseable favorite={favorite['label']} "
                f"or forecast_bucket={forecast_row['label']}",
            )
        )
    if favorite["polymarket_market_id"] == forecast_row["polymarket_market_id"]:
        return RunnerResult(
            notes=(
                "forecast value skipped: "
                f"{target_date.isoformat()} {forecast_row['label']} already market favorite",
            )
        )
    if (
        favorite_predicate.value_c > forecast_predicate.value_c
        and forecast_predicate.type != PredicateType.GTE_C
    ):
        return RunnerResult(
            notes=(
                "forecast value skipped: "
                f"{target_date.isoformat()} favorite {favorite['label']} is above "
                f"forecast bucket {forecast_row['label']}; threshold risk",
            )
        )
    if favorite_predicate.value_c >= forecast_predicate.value_c:
        return RunnerResult(
            notes=(
                "forecast value skipped: "
                f"{target_date.isoformat()} favorite {favorite['label']} is not below "
                f"forecast bucket {forecast_row['label']}",
            )
        )
    event_key = (
        f"forecast_value:{target_date.isoformat()}:"
        f"{latest['id']}:{forecast_row['polymarket_market_id']}:"
        f"{favorite['polymarket_market_id']}:{forecast_book.best_ask}"
    )
    if has_processed_event(db, event_key):
        return RunnerResult(
            notes=(
                "forecast value skipped: "
                f"{target_date.isoformat()} {forecast_row['label']} already processed "
                f"at ask={forecast_book.best_ask:.3f}",
            )
        )
    _mark_event_processed(
        db,
        event_type="forecast_value",
        event_key=event_key,
        reason=(
            f"forecast bucket {forecast_row['label']} cheap at "
            f"{forecast_book.best_ask:.3f}; favorite is {favorite['label']}"
        ),
        details={
            "forecast_high": forecast_high,
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
        reason="forecast bucket priced unrealistically low vs HKO forecast",
    )
    filled = _execute_candidate_buy(
        db,
        candidate,
        event_type="forecast_value",
        event_key=event_key,
        max_buy_price=Settings.forecast_value_max_yes_ask,
        allow_existing_position=True,
        size_usd=_remaining_position_budget(db, forecast_row["yes_token_id"]),
    )
    store_signal(
        db,
        market_id=target_date.isoformat(),
        trigger_type="forecast_value",
        current_max_c=None,
        forecast_max_c=forecast_high,
        affected_outcomes={forecast_row["label"]: "INCREASES_YES_PROBABILITY"},
        price_response={forecast_row["label"]: "PRICE_NOT_MOVED_WITH_EVENT"},
        notes=f"forecast bucket {forecast_row['label']} cheap vs lower favorite {favorite['label']}",
    )
    return RunnerResult(
        buys_filled=1 if filled is True else 0,
        buys_missed=1 if filled is False else 0,
        signals=1,
        notes=(f"forecast value entry {forecast_row['label']} YES",),
    )


def process_forecast_position_exits(
    db: sqlite3.Connection,
    target_date: date,
    new_forecast_max_c: float,
    event_key: str | None = None,
) -> RunnerResult:
    sells_filled = 0
    sells_missed = 0
    notes: list[str] = []
    for pos in list_open_paper_positions(db):
        token_id = pos["outcome_id"]
        outcome = find_outcome_by_token(db, token_id)
        if outcome is None or outcome["target_date_hkt"] != target_date.isoformat():
            continue
        side = "YES" if token_id == outcome["yes_token_id"] else "NO"
        if not _position_invalidated_by_forecast(outcome, side, new_forecast_max_c):
            continue
        try:
            book = latest_orderbook(db, token_id)
        except ValueError:
            store_paper_decision(
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
        result = execute_paper_sell(
            db, token_id, book.bids, "position invalidated by forecast change"
        )
        if result.status == "filled":
            store_paper_decision(
                db,
                "forecast_exit",
                token_id,
                outcome["label"],
                side,
                "SELL",
                "filled",
                "position invalidated by forecast change",
                {"forecast_max": new_forecast_max_c, "bid": book.best_bid},
                event_key=event_key,
            )
            sells_filled += 1
            notes.append(f"sold forecast-invalidated {outcome['label']} {side}")
        else:
            store_paper_decision(
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
    if not transitions:
        return RunnerResult(notes=("need two observed maxes",))
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
    if not notes:
        notes.append("no actual cross candidates")
    return RunnerResult(
        buys_filled=aggregate.buys_filled,
        buys_missed=aggregate.buys_missed,
        signals=aggregate.signals,
        notes=tuple(notes),
    )


def _process_actual_transition(
    db: sqlite3.Connection, today_hkt: date, old: sqlite3.Row, new: sqlite3.Row
) -> RunnerResult:
    old_max = float(old["since_midnight_max_c"])
    new_max = float(new["since_midnight_max_c"])
    latest_forecast = latest_forecast_high(db, today_hkt.isoformat())
    if latest_forecast is None:
        return RunnerResult(notes=("missing current forecast for actual check",))
    forecast_max = float(latest_forecast["forecast_max_c"])
    if not (old_max <= forecast_max < new_max):
        return RunnerResult(notes=("actual max has not crossed above forecast max",))
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
    for row in list_outcomes_for_date(db, today_hkt.isoformat()):
        predicate = parse_outcome_label(row["label"])
        side = _actual_cross_side(predicate, old_max, new_max)
        if side is None:
            continue
        token_id = row["yes_token_id"] if side == "YES" else row["no_token_id"]
        if signals == 0:
            _mark_event_processed(
                db,
                event_type="actual_cross",
                event_key=event_key,
                reason=f"observed max changed {old_max} -> {new_max}",
                details={"old_max": old_max, "new_max": new_max, "forecast_max": forecast_max},
            )
        signals += 1
        prices = latest_two_orderbook_prices(db, token_id)
        if len(prices) < 2 or prices[0]["best_ask"] is None or prices[1]["best_ask"] is None:
            store_paper_decision(
                db,
                "actual_cross",
                token_id,
                row["label"],
                side,
                "BUY",
                "missed",
                f"missing prior/current {side.lower()} ask",
                {"old_max": old_max, "new_max": new_max, "forecast_max": forecast_max},
                event_key=event_key,
            )
            buys_missed += 1
            continue
        prior = float(prices[1]["best_ask"])
        current = float(prices[0]["best_ask"])
        if current - prior >= Settings.stale_price_min_move:
            store_paper_decision(
                db,
                "actual_cross",
                token_id,
                row["label"],
                side,
                "BUY",
                "missed",
                "price moved with actual cross",
                {
                    "old_max": old_max,
                    "new_max": new_max,
                    "forecast_max": forecast_max,
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
        filled = _execute_candidate_buy(
            db, candidate, event_type="actual_cross", event_key=event_key
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


def process_open_position_exits(
    db: sqlite3.Connection, today_hkt: date | None = None
) -> RunnerResult:
    today = today_hkt or datetime.now(HKT).date()
    sells_filled = 0
    sells_missed = 0
    notes: list[str] = []
    for pos in list_open_paper_positions(db):
        token_id = pos["outcome_id"]
        outcome = find_outcome_by_token(db, token_id)
        if outcome is None:
            store_paper_decision(
                db, "exit_check", token_id, None, None, "SELL", "missed", "unknown token"
            )
            sells_missed += 1
            continue
        side = "YES" if token_id == outcome["yes_token_id"] else "NO"
        actual_applies = outcome["target_date_hkt"] == today.isoformat()
        latest_actual = (
            latest_observed_max_for_date(db, outcome["target_date_hkt"])
            if actual_applies
            else None
        )
        actual_max_for_outcome = (
            float(latest_actual["since_midnight_max_c"]) if latest_actual else None
        )
        invalidated = _position_invalidated(outcome, side, actual_max_for_outcome)
        hold_to_maturity = _position_can_hold_to_maturity(
            outcome, side, actual_max_for_outcome
        )
        if hold_to_maturity:
            notes.append(f"holding settled {outcome['label']} {side}")
            continue

        try:
            book = latest_orderbook(db, token_id)
        except ValueError:
            store_paper_decision(
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
            store_paper_decision(
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
        if not invalidated:
            continue
        reason = "position invalidated by observed max"
        result = execute_paper_sell(db, token_id, book.bids, reason)
        if result.status == "filled":
            store_paper_decision(
                db,
                "exit_check",
                token_id,
                outcome["label"],
                side,
                "SELL",
                "filled",
                reason,
                {"current_max": actual_max_for_outcome, "bid": book.best_bid},
            )
            sells_filled += 1
        else:
            store_paper_decision(
                db,
                "exit_check",
                token_id,
                outcome["label"],
                side,
                "SELL",
                "missed",
                result.reason,
                {"current_max": actual_max_for_outcome, "bid": book.best_bid},
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
    moved_up = new_forecast_max_c > old_forecast_max_c
    for outcome in outcomes:
        prior = prior_yes_asks.get(outcome.market_id)
        current = current_yes_asks.get(outcome.market_id)
        if prior is None or current is None:
            continue
        side = _forecast_move_side(outcome, new_forecast_max_c, moved_up)
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
    outcome: Outcome, new_forecast_max_c: float, moved_up: bool
) -> str | None:
    predicate = outcome.predicate
    if predicate_matches(predicate, new_forecast_max_c):
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


def _merge_runner_results(first: RunnerResult, second: RunnerResult) -> RunnerResult:
    return RunnerResult(
        buys_filled=first.buys_filled + second.buys_filled,
        buys_missed=first.buys_missed + second.buys_missed,
        sells_filled=first.sells_filled + second.sells_filled,
        sells_missed=first.sells_missed + second.sells_missed,
        signals=first.signals + second.signals,
        notes=first.notes + second.notes,
    )


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
    existing = db.execute(
        "select net_shares from paper_positions where outcome_id = ? and net_shares > 0",
        (token_id,),
    ).fetchone()
    if existing and not allow_existing_position:
        store_paper_decision(
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
    requested_size = Settings.max_order_usd if size_usd is None else size_usd
    if requested_size <= 0:
        store_paper_decision(
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
        store_paper_decision(
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
    if book.best_ask is not None and book.best_ask > Settings.max_entry_price:
        store_paper_decision(
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
                "max_entry_price": Settings.max_entry_price,
                "impact": candidate.impact.value,
                "prior_yes_ask": candidate.prior_yes_ask,
                "current_yes_ask": candidate.current_yes_ask,
            },
            event_key=event_key,
        )
        return False
    result = execute_paper_buy(
        db,
        token_id=token_id,
        side=side,
        size_usd=requested_size,
        asks=book.asks,
        max_order_usd=Settings.max_order_usd,
        reason=candidate.reason,
        max_price=max_buy_price,
    )
    status = "filled" if result.status == "filled" else "missed"
    store_paper_decision(
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
    position = db.execute(
        "select net_shares, avg_price from paper_positions where outcome_id = ?",
        (token_id,),
    ).fetchone()
    if position is None:
        return Settings.max_order_usd
    invested = float(position["net_shares"]) * float(position["avg_price"])
    return max(Settings.max_order_usd - invested, 0.0)


def _mark_event_processed(
    db: sqlite3.Connection,
    event_type: str,
    event_key: str,
    reason: str,
    details: dict,
) -> None:
    store_paper_decision(
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


def _position_invalidated(outcome: sqlite3.Row, side: str, current_max: float | None) -> bool:
    if current_max is None:
        return False
    predicate = parse_outcome_label(outcome["label"])
    if side == "NO":
        return predicate_matches(predicate, current_max)
    if predicate.value_c is None:
        return False
    if predicate.type.value == "EXACT_C":
        return current_max >= predicate.value_c + 1
    if predicate.type.value == "BOTTOM_BUCKET_LTE_C":
        return current_max >= predicate.value_c + 1
    return False


def _position_invalidated_by_forecast(
    outcome: sqlite3.Row, side: str, forecast_max: float
) -> bool:
    predicate = parse_outcome_label(outcome["label"])
    matches_forecast = predicate_matches(predicate, forecast_max)
    if side == "YES":
        return not matches_forecast
    if side == "NO":
        return matches_forecast
    return False


def _position_can_hold_to_maturity(
    outcome: sqlite3.Row, side: str, current_max: float | None
) -> bool:
    if side != "YES" or current_max is None:
        return False
    predicate = parse_outcome_label(outcome["label"])
    return predicate.type.value == "GTE_C" and predicate_matches(predicate, current_max)
