from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from .markets import Predicate, parse_outcome_label


GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
CLOB_BOOK = "https://clob.polymarket.com/book"


@dataclass(frozen=True)
class Outcome:
    market_id: str
    label: str
    predicate: Predicate
    yes_token_id: str
    no_token_id: str
    best_bid: float | None = None
    best_ask: float | None = None


@dataclass(frozen=True)
class TemperatureMarket:
    event_id: str
    event_slug: str
    title: str
    target_date: date | None
    outcomes: list[Outcome] = field(default_factory=list)


@dataclass(frozen=True)
class OrderBook:
    token_id: str
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]
    tick_size: float
    min_order_size: float

    @property
    def best_bid(self) -> float | None:
        return max((price for price, _ in self.bids), default=None)

    @property
    def best_ask(self) -> float | None:
        return min((price for price, _ in self.asks), default=None)


def _json_loads_field(value: Any) -> list[str]:
    if isinstance(value, str):
        return json.loads(value)
    return list(value or [])


def fetch_json(url: str) -> Any:
    request = Request(url, headers={"User-Agent": "whenitrains/0.1"})
    with urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def event_slug_for_date(target: date) -> str:
    return f"highest-temperature-in-hong-kong-on-{target.strftime('%B').lower()}-{target.day}-{target.year}"


def is_current_day_market(target: date | None, today_hkt: date) -> bool:
    return target == today_hkt


def fetch_hk_temperature_event(slug: str) -> dict[str, Any] | None:
    items = fetch_json(f"{GAMMA_EVENTS}?slug={quote(slug)}")
    return items[0] if items else None


def parse_event_markets(event: dict[str, Any]) -> list[TemperatureMarket]:
    target_date = None
    if event.get("eventDate"):
        target_date = date.fromisoformat(event["eventDate"])
    outcomes: list[Outcome] = []
    for market in event.get("markets", []):
        tokens = _json_loads_field(market.get("clobTokenIds"))
        if len(tokens) < 2:
            continue
        label = market.get("groupItemTitle") or market.get("question", "")
        outcomes.append(
            Outcome(
                market_id=str(market["id"]),
                label=label,
                predicate=parse_outcome_label(label),
                yes_token_id=tokens[0],
                no_token_id=tokens[1],
                best_bid=_optional_float(market.get("bestBid")),
                best_ask=_optional_float(market.get("bestAsk")),
            )
        )
    return [
        TemperatureMarket(
            event_id=str(event["id"]),
            event_slug=event["slug"],
            title=event["title"],
            target_date=target_date,
            outcomes=outcomes,
        )
    ]


def fetch_orderbook(token_id: str) -> OrderBook:
    payload = fetch_json(f"{CLOB_BOOK}?token_id={quote(token_id)}")
    return parse_orderbook(payload)


def parse_orderbook(payload: dict[str, Any]) -> OrderBook:
    return OrderBook(
        token_id=str(payload["asset_id"]),
        bids=[(float(row["price"]), float(row["size"])) for row in payload.get("bids", [])],
        asks=[(float(row["price"]), float(row["size"])) for row in payload.get("asks", [])],
        tick_size=float(payload.get("tick_size", 0.01)),
        min_order_size=float(payload.get("min_order_size", 5)),
    )


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)
