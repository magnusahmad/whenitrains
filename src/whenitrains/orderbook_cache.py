from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .polymarket import OrderBook
from .storage import connect, store_orderbook


class BookCacheMiss(KeyError):
    pass


class BookCacheStale(ValueError):
    pass


@dataclass(frozen=True)
class CachedOrderBook:
    book: OrderBook
    updated_monotonic: float
    last_trade_price: float | None = None


@dataclass(frozen=True)
class MarketWebSocketSubscription:
    token_ids: list[str]

    def payload(self) -> dict:
        return {
            "type": "market",
            "assets_ids": self.token_ids,
            "custom_feature_enabled": True,
        }


class SubscriptionManager:
    def __init__(self) -> None:
        self._token_ids: tuple[str, ...] = ()

    def update(self, token_ids: list[str]) -> dict | None:
        normalized = tuple(dict.fromkeys(token_ids))
        if set(normalized) == set(self._token_ids):
            return None
        self._token_ids = normalized
        return MarketWebSocketSubscription(list(self._token_ids)).payload()


class OrderBookCache:
    def __init__(
        self,
        *,
        db: sqlite3.Connection | None = None,
        db_path: Path | None = None,
        monotonic_fn: Callable[[], float] = time.monotonic,
        persist_snapshots: bool = True,
    ) -> None:
        self._books: dict[str, CachedOrderBook] = {}
        self._db = db
        self._db_path = db_path
        self._monotonic_fn = monotonic_fn
        self._persist_snapshots = persist_snapshots

    def seed(self, book: OrderBook) -> None:
        self._set_book(book, "rest_seed")

    def apply_message(self, message: dict[str, Any]) -> OrderBook | None:
        event_type = str(message.get("event_type") or message.get("type") or "")
        token_id = _token_id(message)
        if token_id is None:
            return None
        if event_type == "book":
            return self._apply_book_snapshot(token_id, message, event_type)
        if event_type == "price_change":
            return self._apply_price_change(token_id, message, event_type)
        if event_type == "best_bid_ask":
            return self._apply_best_bid_ask(token_id, message, event_type)
        if event_type == "last_trade_price":
            return self._apply_last_trade_price(token_id, message, event_type)
        return None

    def latest_orderbook(
        self,
        token_id: str,
        *,
        max_age_seconds: float,
        now_monotonic: float | None = None,
    ) -> OrderBook:
        cached = self._books.get(token_id)
        if cached is None:
            raise BookCacheMiss(token_id)
        now = self._monotonic_fn() if now_monotonic is None else now_monotonic
        age = now - cached.updated_monotonic
        if age > max_age_seconds:
            raise BookCacheStale(f"book {token_id} stale by {age:.3f}s")
        return cached.book

    def _apply_book_snapshot(
        self, token_id: str, message: dict[str, Any], event_type: str
    ) -> OrderBook:
        book = OrderBook(
            token_id=token_id,
            bids=_levels(message.get("bids") or message.get("buys") or []),
            asks=_levels(message.get("asks") or message.get("sells") or []),
            tick_size=float(message.get("tick_size", 0.01)),
            min_order_size=float(message.get("min_order_size", 5)),
        )
        self._set_book(book, event_type)
        return book

    def _apply_price_change(
        self, token_id: str, message: dict[str, Any], event_type: str
    ) -> OrderBook:
        cached = self._books.get(token_id)
        bids = dict(cached.book.bids) if cached is not None else {}
        asks = dict(cached.book.asks) if cached is not None else {}
        for change in message.get("changes") or []:
            side = str(change.get("side") or "").upper()
            price = float(change["price"])
            size = float(change["size"])
            levels = bids if side in ("BUY", "BID", "BIDS") else asks
            if size == 0:
                levels.pop(price, None)
            else:
                levels[price] = size
        book = OrderBook(
            token_id=token_id,
            bids=sorted(bids.items(), reverse=True),
            asks=sorted(asks.items()),
            tick_size=cached.book.tick_size if cached is not None else 0.01,
            min_order_size=cached.book.min_order_size if cached is not None else 5,
        )
        self._set_book(book, event_type)
        return book

    def _apply_best_bid_ask(
        self, token_id: str, message: dict[str, Any], event_type: str
    ) -> OrderBook:
        cached = self._books.get(token_id)
        bid = _optional_float(message.get("best_bid"))
        ask = _optional_float(message.get("best_ask"))
        book = OrderBook(
            token_id=token_id,
            bids=[] if bid is None else [(bid, 0.0)],
            asks=[] if ask is None else [(ask, 0.0)],
            tick_size=cached.book.tick_size if cached is not None else 0.01,
            min_order_size=cached.book.min_order_size if cached is not None else 5,
        )
        self._set_book(book, event_type)
        return book

    def _apply_last_trade_price(
        self, token_id: str, message: dict[str, Any], event_type: str
    ) -> OrderBook:
        cached = self._books.get(token_id)
        book = (
            cached.book
            if cached is not None
            else OrderBook(token_id, bids=[], asks=[], tick_size=0.01, min_order_size=5)
        )
        self._set_book(book, event_type, last_trade_price=_optional_float(message.get("price")))
        return book

    def _set_book(
        self,
        book: OrderBook,
        websocket_event_type: str,
        last_trade_price: float | None = None,
    ) -> None:
        updated = self._monotonic_fn()
        previous = self._books.get(book.token_id)
        trade_price = (
            last_trade_price
            if last_trade_price is not None
            else previous.last_trade_price if previous is not None else None
        )
        self._books[book.token_id] = CachedOrderBook(
            book=book,
            updated_monotonic=updated,
            last_trade_price=trade_price,
        )
        if not self._persist_snapshots:
            return
        metadata = {
            "source": "polymarket_market_websocket",
            "websocket_event_type": websocket_event_type,
            "received_monotonic": updated,
            "last_trade_price": trade_price,
        }
        if self._db is not None:
            store_orderbook(self._db, book.token_id, book, metadata=metadata)
        elif self._db_path is not None:
            db = connect(self._db_path)
            try:
                store_orderbook(db, book.token_id, book, metadata=metadata)
            finally:
                db.close()


def _token_id(message: dict[str, Any]) -> str | None:
    value = (
        message.get("asset_id")
        or message.get("asset")
        or message.get("token_id")
        or message.get("market")
    )
    return None if value is None else str(value)


def _levels(raw: list[Any]) -> list[tuple[float, float]]:
    levels: list[tuple[float, float]] = []
    for row in raw:
        if isinstance(row, dict):
            price = row["price"]
            size = row["size"]
        else:
            price, size = row
        size_float = float(size)
        if size_float != 0:
            levels.append((float(price), size_float))
    return sorted(levels)


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)
