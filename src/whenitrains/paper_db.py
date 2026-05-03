from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .storage import (
    get_paper_position,
    store_paper_order_result,
    upsert_paper_position,
)


@dataclass(frozen=True)
class EntryQuote:
    status: str
    token_id: str
    side: str
    requested_size_usd: float
    limit_price: float | None
    estimated_avg_price: float | None
    estimated_shares: float
    estimated_cost_usd: float
    reason: str


@dataclass(frozen=True)
class ExitQuote:
    token_id: str
    current_bid: float
    avg_entry_price: float
    net_shares: float
    price_move: float
    should_sell: bool
    reason: str


@dataclass(frozen=True)
class PersistedPaperResult:
    status: str
    token_id: str
    side: str
    fill_price: float | None
    fill_size_usd: float
    shares: float
    reason: str


def calculate_entry(
    token_id: str,
    size_usd: float,
    asks: list[tuple[float, float]],
    max_order_usd: float,
) -> EntryQuote:
    if size_usd > max_order_usd:
        return EntryQuote(
            "rejected", token_id, "BUY", size_usd, None, None, 0, 0, "order exceeds max"
        )
    remaining = size_usd
    spent = 0.0
    shares = 0.0
    limit_price = None
    for price, available_shares in sorted(asks):
        if remaining <= 0:
            break
        take = min(available_shares, remaining / price)
        if take <= 0:
            continue
        spent += take * price
        shares += take
        remaining -= take * price
        limit_price = price
    if shares <= 0:
        return EntryQuote(
            "rejected", token_id, "BUY", size_usd, None, None, 0, 0, "no ask depth"
        )
    return EntryQuote(
        "fillable",
        token_id,
        "BUY",
        size_usd,
        limit_price,
        spent / shares,
        shares,
        spent,
        "visible ask depth supports order",
    )


def execute_paper_buy(
    db: sqlite3.Connection,
    token_id: str,
    side: str,
    size_usd: float,
    asks: list[tuple[float, float]],
    max_order_usd: float,
    reason: str,
) -> PersistedPaperResult:
    quote = calculate_entry(token_id, size_usd, asks, max_order_usd)
    if quote.status != "fillable":
        store_paper_order_result(
            db, token_id, f"BUY_{side}", None, size_usd, None, 0, "rejected", quote.reason
        )
        return PersistedPaperResult("rejected", token_id, f"BUY_{side}", None, 0, 0, quote.reason)

    pos = get_paper_position(db, token_id)
    old_shares = float(pos["net_shares"]) if pos else 0.0
    old_avg = float(pos["avg_price"]) if pos else 0.0
    old_realized = float(pos["realized_pnl"]) if pos else 0.0
    new_shares = old_shares + quote.estimated_shares
    new_avg = (old_avg * old_shares + quote.estimated_cost_usd) / new_shares
    upsert_paper_position(db, token_id, new_shares, new_avg, old_realized)
    store_paper_order_result(
        db,
        token_id,
        f"BUY_{side}",
        quote.limit_price,
        size_usd,
        quote.estimated_avg_price,
        quote.estimated_cost_usd,
        "filled",
        reason,
    )
    return PersistedPaperResult(
        "filled",
        token_id,
        f"BUY_{side}",
        quote.estimated_avg_price,
        quote.estimated_cost_usd,
        quote.estimated_shares,
        reason,
    )


def calculate_exit(
    db: sqlite3.Connection,
    token_id: str,
    current_bid: float,
    take_profit: float,
    max_hold_minutes: float = 10.0,
    now: datetime | None = None,
) -> ExitQuote:
    pos = get_paper_position(db, token_id)
    if pos is None or float(pos["net_shares"]) <= 0:
        return ExitQuote(token_id, current_bid, 0, 0, 0, False, "no position")
    avg_price = float(pos["avg_price"])
    shares = float(pos["net_shares"])
    move = current_bid - avg_price
    if move >= take_profit:
        return ExitQuote(
            token_id,
            current_bid,
            avg_price,
            shares,
            move,
            True,
            "take profit reached",
        )
    entry_time = _parse_utc_timestamp(pos["updated_at_utc"])
    current_time = now or datetime.now(timezone.utc)
    if current_time - entry_time >= timedelta(minutes=max_hold_minutes):
        return ExitQuote(
            token_id,
            current_bid,
            avg_price,
            shares,
            move,
            True,
            "max hold time reached",
        )
    should_sell = False
    reason = "hold"
    return ExitQuote(token_id, current_bid, avg_price, shares, move, should_sell, reason)


def _parse_utc_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def execute_paper_sell(
    db: sqlite3.Connection,
    token_id: str,
    bids: list[tuple[float, float]],
    reason: str,
) -> PersistedPaperResult:
    pos = get_paper_position(db, token_id)
    if pos is None or float(pos["net_shares"]) <= 0:
        store_paper_order_result(
            db, token_id, "SELL", None, 0, None, 0, "rejected", "no position"
        )
        return PersistedPaperResult("rejected", token_id, "SELL", None, 0, 0, "no position")
    remaining = float(pos["net_shares"])
    avg_price = float(pos["avg_price"])
    old_realized = float(pos["realized_pnl"])
    proceeds = 0.0
    sold = 0.0
    limit_price = None
    for price, available_shares in sorted(bids, reverse=True):
        if remaining <= 0:
            break
        take = min(remaining, available_shares)
        proceeds += take * price
        sold += take
        remaining -= take
        limit_price = price
    if sold <= 0:
        store_paper_order_result(
            db, token_id, "SELL", None, 0, None, 0, "rejected", "no bid depth"
        )
        return PersistedPaperResult("rejected", token_id, "SELL", None, 0, 0, "no bid depth")
    fill_price = proceeds / sold
    realized = old_realized + proceeds - sold * avg_price
    upsert_paper_position(db, token_id, remaining, avg_price, realized)
    store_paper_order_result(
        db, token_id, "SELL", limit_price, proceeds, fill_price, proceeds, "filled", reason
    )
    return PersistedPaperResult("filled", token_id, "SELL", fill_price, proceeds, sold, reason)
