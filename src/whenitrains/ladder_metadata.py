from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .config import Settings
from .storage import (
    get_live_position,
    get_paper_position,
    latest_orderbook,
    list_outcomes_for_date,
)


@dataclass(frozen=True)
class LadderTokenMetadata:
    target_date_hkt: str
    market_kind: str
    label: str
    polymarket_market_id: str
    token_id: str
    side: str
    predicate_type: str
    predicate_value_c: float | None
    best_bid: float | None
    best_ask: float | None
    tick_size: float | None
    min_order_size: float | None
    has_open_position: bool
    held_shares: float
    avg_price: float
    remaining_budget_usd: float
    neg_risk: bool


def build_active_ladder_metadata(
    db: sqlite3.Connection,
    *,
    target_date_hkt: str,
    max_order_usd: float = Settings.max_order_usd,
    live: bool = False,
) -> list[LadderTokenMetadata]:
    entries: list[LadderTokenMetadata] = []
    for row in list_outcomes_for_date(db, target_date_hkt):
        market_kind = _market_kind_from_slug(str(row["slug"] or ""))
        for side, token_id in (("YES", row["yes_token_id"]), ("NO", row["no_token_id"])):
            book = _latest_orderbook_or_none(db, token_id)
            position = (
                get_live_position(db, token_id)
                if live
                else get_paper_position(db, token_id)
            )
            held_shares = float(position["net_shares"]) if position is not None else 0.0
            avg_price = float(position["avg_price"]) if position is not None else 0.0
            invested = max(held_shares, 0.0) * avg_price
            entries.append(
                LadderTokenMetadata(
                    target_date_hkt=target_date_hkt,
                    market_kind=market_kind,
                    label=row["label"],
                    polymarket_market_id=row["polymarket_market_id"],
                    token_id=token_id,
                    side=side,
                    predicate_type=row["predicate_type"],
                    predicate_value_c=row["predicate_value_c"],
                    best_bid=book.best_bid if book is not None else None,
                    best_ask=book.best_ask if book is not None else None,
                    tick_size=book.tick_size if book is not None else None,
                    min_order_size=book.min_order_size if book is not None else None,
                    has_open_position=held_shares > 0,
                    held_shares=held_shares,
                    avg_price=avg_price,
                    remaining_budget_usd=max(max_order_usd - invested, 0.0),
                    neg_risk=False,
                )
            )
    return entries


def _latest_orderbook_or_none(db: sqlite3.Connection, token_id: str):
    try:
        return latest_orderbook(db, token_id)
    except ValueError:
        return None


def _market_kind_from_slug(slug: str) -> str:
    if slug.startswith("lowest-temperature-in-hong-kong-on-"):
        return "lowest"
    return "highest"
