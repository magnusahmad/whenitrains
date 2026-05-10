from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from html import unescape
from urllib.parse import quote
from urllib.request import Request, urlopen

from .markets import Predicate, parse_outcome_label


GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
CLOB_BOOK = "https://clob.polymarket.com/book"


TEMPERATURE_MARKET_KINDS = ("highest", "lowest")


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
    resolution_rules_text: str = ""
    raw_event: dict[str, Any] = field(default_factory=dict)
    outcomes: list[Outcome] = field(default_factory=list)
    status: str = "active"


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


def event_slug_for_date(target: date, market_kind: str = "highest") -> str:
    if market_kind not in TEMPERATURE_MARKET_KINDS:
        raise ValueError(f"unknown temperature market kind: {market_kind}")
    return (
        f"{market_kind}-temperature-in-hong-kong-on-"
        f"{target.strftime('%B').lower()}-{target.day}-{target.year}"
    )


def event_slugs_for_date(target: date) -> list[str]:
    return [event_slug_for_date(target, market_kind) for market_kind in TEMPERATURE_MARKET_KINDS]


def temperature_market_kind(slug: str) -> str | None:
    for market_kind in TEMPERATURE_MARKET_KINDS:
        if slug.startswith(f"{market_kind}-temperature-in-hong-kong-on-"):
            return market_kind
    return None


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
            status=_event_status(event),
            resolution_rules_text=extract_resolution_rules_text(event),
            raw_event=event,
            outcomes=outcomes,
        )
    ]


EXPECTED_RESOLUTION_TAIL_BY_KIND = {
    "highest": """
The resolution source for this market will be information from the Hong Kong Observatory,
specifically the "Absolute Daily Max (deg. C)" the specified date once information is finalized
in the relevant "Daily Extract", available here: https://www.weather.gov.hk/en/cis/climat.htm

This market can not resolve to "Yes" until data for this date has been finalized.

The resolution source for this market measures temperatures in Celsius to one decimal place
(eg, 9.1°C). Thus, this is the level of precision that will be used when resolving the market.

Any revisions to temperatures recorded after data is finalized for this market's timeframe
will not be considered for this market's resolution.
""",
    "lowest": """
The resolution source for this market will be information from the Hong Kong Observatory,
specifically the "Absolute Daily Min (deg. C)" the specified date once information is finalized
in the relevant "Daily Extract", available here: https://www.weather.gov.hk/en/cis/climat.htm

This market can not resolve to "Yes" until data for this date has been finalized.

The resolution source for this market measures temperatures in Celsius to one decimal place
(eg, 9.1°C). Thus, this is the level of precision that will be used when resolving the market.

Any revisions to temperatures recorded after data is finalized for this market's timeframe
will not be considered for this market's resolution.
""",
}


def extract_resolution_rules_text(event: dict[str, Any]) -> str:
    candidates: list[str] = []
    for key in ("description", "rules", "resolutionSource", "marketContext"):
        value = event.get(key)
        if isinstance(value, str):
            candidates.append(value)
    for market in event.get("markets", []):
        for key in ("description", "rules", "resolutionSource", "marketContext"):
            value = market.get(key)
            if isinstance(value, str):
                candidates.append(value)
    return "\n\n".join(dict.fromkeys(item for item in candidates if item.strip()))


def _event_status(event: dict[str, Any]) -> str:
    status = event.get("status")
    if isinstance(status, str) and status:
        return status.lower()
    if event.get("resolved"):
        return "resolved"
    if event.get("closed"):
        return "closed"
    if event.get("active") is False:
        return "inactive"
    return "active"


def resolution_rules_match_expected(text: str, market_kind: str = "highest") -> bool:
    if market_kind not in EXPECTED_RESOLUTION_TAIL_BY_KIND:
        return False
    normalized = _normalize_resolution_text(text)
    if not normalized:
        return False
    first_sentence_pattern = (
        r"this market will resolve to the temperature range that contains the "
        rf"{market_kind} temperature recorded by the hong kong observatory in degrees "
        r"celsius on [^.]+\."
    )
    if re.search(first_sentence_pattern, normalized) is None:
        return False
    expected_tail = EXPECTED_RESOLUTION_TAIL_BY_KIND[market_kind]
    return _normalize_resolution_text(expected_tail) in normalized


def resolution_rules_warning(market: TemperatureMarket) -> str | None:
    market_kind = temperature_market_kind(market.event_slug) or "highest"
    if resolution_rules_match_expected(market.resolution_rules_text, market_kind):
        return None
    source_name = "Max" if market_kind == "highest" else "Min"
    return (
        "resolution rules mismatch for "
        f"{market.event_slug}: expected HKO Daily Extract Absolute Daily {source_name} "
        "one-decimal finalized-data wording"
    )


def _normalize_resolution_text(text: str) -> str:
    text = unescape(text or "")
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


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
