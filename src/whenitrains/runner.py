from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone

from .config import Settings
from .engine import TradeCandidate, build_trade_candidates
from .hko import HKT
from .markets import parse_outcome_label, predicate_matches
from .paper_db import calculate_exit, execute_paper_buy, execute_paper_sell
from .polymarket import Outcome
from .signals import DirectionalImpact, PriceResponse
from .storage import (
    dashboard_stats,
    find_outcome_by_token,
    has_processed_event,
    latest_orderbook,
    latest_two_forecast_highs,
    latest_two_observed_maxes,
    latest_two_orderbook_prices,
    list_open_paper_positions,
    list_outcomes_for_date,
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
    forecast_result = process_forecast_entries(db, today)
    actual_result = process_actual_entries(db, today)
    exit_result = process_open_position_exits(db)
    return RunnerResult(
        buys_filled=forecast_result.buys_filled + actual_result.buys_filled,
        buys_missed=forecast_result.buys_missed + actual_result.buys_missed,
        sells_filled=exit_result.sells_filled,
        sells_missed=exit_result.sells_missed,
        signals=forecast_result.signals + actual_result.signals,
        notes=forecast_result.notes + actual_result.notes + exit_result.notes,
    )


def process_forecast_entries(db: sqlite3.Connection, today_hkt: date) -> RunnerResult:
    latest = latest_two_forecast_highs(db)
    if len(latest) < 2:
        return RunnerResult(notes=("need two forecast highs",))
    new, old = latest[0], latest[1]
    if new["forecast_date_hkt"] != today_hkt.isoformat():
        return RunnerResult(notes=("latest forecast is not for current day",))
    old_high = float(old["forecast_max_c"])
    new_high = float(new["forecast_max_c"])
    if old_high == new_high:
        return RunnerResult(notes=("forecast high unchanged",))
    event_key = (
        f"forecast_change:{today_hkt.isoformat()}:"
        f"{old['id']}:{old_high}->{new['id']}:{new_high}"
    )
    if has_processed_event(db, event_key):
        return RunnerResult(notes=("forecast change already processed",))
    _mark_event_processed(
        db,
        event_type="forecast_change",
        event_key=event_key,
        reason=f"forecast high changed {old_high} -> {new_high}",
        details={"old_high": old_high, "new_high": new_high},
    )

    rows = list_outcomes_for_date(db, today_hkt.isoformat())
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

    candidates = build_trade_candidates(
        outcomes=outcomes,
        old_forecast_max_c=old_high,
        new_forecast_max_c=new_high,
        prior_yes_asks=prior_yes_asks,
        current_yes_asks=current_yes_asks,
        min_move=Settings.stale_price_min_move,
    )
    if not candidates:
        return RunnerResult(buys_missed=1, notes=("no stale forecast candidates",))

    store_signal(
        db,
        market_id=today_hkt.isoformat(),
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
        filled = _execute_candidate_buy(db, candidate, event_key=event_key)
        if filled is True:
            buys_filled += 1
        elif filled is False:
            buys_missed += 1
    return RunnerResult(
        buys_filled=buys_filled,
        buys_missed=buys_missed,
        signals=1,
        notes=(f"forecast high changed {old_high} -> {new_high}",),
    )


def process_actual_entries(db: sqlite3.Connection, today_hkt: date) -> RunnerResult:
    latest = latest_two_observed_maxes(db)
    if len(latest) < 2:
        return RunnerResult(notes=("need two observed maxes",))
    new, old = latest[0], latest[1]
    old_max = float(old["since_midnight_max_c"])
    new_max = float(new["since_midnight_max_c"])
    if new_max <= old_max:
        return RunnerResult(notes=("observed max unchanged",))
    event_key = (
        f"actual_cross:{today_hkt.isoformat()}:"
        f"{old['id']}:{old_max}->{new['id']}:{new_max}"
    )
    if has_processed_event(db, event_key):
        return RunnerResult(notes=("actual max event already processed",))
    _mark_event_processed(
        db,
        event_type="actual_cross",
        event_key=event_key,
        reason=f"observed max changed {old_max} -> {new_max}",
        details={"old_max": old_max, "new_max": new_max},
    )

    buys_filled = 0
    buys_missed = 0
    signals = 0
    for row in list_outcomes_for_date(db, today_hkt.isoformat()):
        predicate = parse_outcome_label(row["label"])
        if predicate.type.value != "GTE_C" or predicate.value_c is None:
            continue
        if not (old_max < predicate.value_c <= new_max):
            continue
        signals += 1
        prices = latest_two_orderbook_prices(db, row["yes_token_id"])
        if len(prices) < 2 or prices[0]["best_ask"] is None or prices[1]["best_ask"] is None:
            store_paper_decision(
                db,
                "actual_cross",
                row["yes_token_id"],
                row["label"],
                "YES",
                "BUY",
                "missed",
                "missing prior/current yes ask",
                {"old_max": old_max, "new_max": new_max},
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
                row["yes_token_id"],
                row["label"],
                "YES",
                "BUY",
                "missed",
                "price moved with actual cross",
                {"old_max": old_max, "new_max": new_max, "prior_yes_ask": prior, "current_yes_ask": current},
                event_key=event_key,
            )
            buys_missed += 1
            continue
        candidate = TradeCandidate(
            outcome=_outcome_from_row(row),
            side="BUY_YES",
            impact=DirectionalImpact.INCREASES_YES_PROBABILITY,
            price_response=PriceResponse.PRICE_NOT_MOVED_WITH_EVENT,
            prior_yes_ask=prior,
            current_yes_ask=current,
            reason="actual max crossed settling threshold before price moved",
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


def process_open_position_exits(db: sqlite3.Connection) -> RunnerResult:
    latest_actual = latest_two_observed_maxes(db)
    current_max = float(latest_actual[0]["since_midnight_max_c"]) if latest_actual else None
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
        invalidated = _position_invalidated(outcome, side, current_max)
        hold_to_maturity = _position_can_hold_to_maturity(outcome, side, current_max)
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
        current_bid = book.best_bid
        if current_bid is None:
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
        quote = calculate_exit(
            db,
            token_id,
            current_bid,
            Settings.take_profit_move,
            max_hold_minutes=Settings.max_hold_minutes,
        )
        if not (quote.should_sell or invalidated):
            continue
        reason = "position invalidated by observed max" if invalidated else quote.reason
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
                {"current_max": current_max, "bid": current_bid},
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
                {"current_max": current_max, "bid": current_bid},
            )
            sells_missed += 1
    return RunnerResult(sells_filled=sells_filled, sells_missed=sells_missed, notes=tuple(notes))


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
) -> bool | None:
    side = "YES" if candidate.side == "BUY_YES" else "NO"
    token_id = candidate.outcome.yes_token_id if side == "YES" else candidate.outcome.no_token_id
    existing = db.execute(
        "select net_shares from paper_positions where outcome_id = ? and net_shares > 0",
        (token_id,),
    ).fetchone()
    if existing:
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
    result = execute_paper_buy(
        db,
        token_id=token_id,
        side=side,
        size_usd=Settings.max_order_usd,
        asks=book.asks,
        max_order_usd=Settings.max_order_usd,
        reason=candidate.reason,
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


def _position_can_hold_to_maturity(
    outcome: sqlite3.Row, side: str, current_max: float | None
) -> bool:
    if side != "YES" or current_max is None:
        return False
    predicate = parse_outcome_label(outcome["label"])
    return predicate.type.value == "GTE_C" and predicate_matches(predicate, current_max)
