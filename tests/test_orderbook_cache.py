import tempfile
import unittest
from datetime import date
from pathlib import Path

from whenitrains.orderbook_cache import (
    BookCacheStale,
    MarketWebSocketSubscription,
    OrderBookCache,
    SubscriptionManager,
)
from whenitrains.markets import parse_outcome_label
from whenitrains.polymarket import OrderBook, Outcome, TemperatureMarket
from whenitrains.storage import (
    connect,
    list_active_market_condition_ids,
    list_active_market_token_ids,
    migrate,
    store_polymarket_event,
)


class OrderBookCacheTests(unittest.TestCase):
    def test_subscription_payload_uses_custom_feature_flag(self):
        subscription = MarketWebSocketSubscription(["yes26", "no26"])

        self.assertEqual(
            subscription.payload(),
            {
                "type": "market",
                "assets_ids": ["yes26", "no26"],
                "custom_feature_enabled": True,
            },
        )

    def test_book_snapshot_and_price_change_remove_zero_size_levels(self):
        cache = OrderBookCache(monotonic_fn=_FakeMonotonic([10.0, 10.1]))
        cache.apply_message(
            {
                "event_type": "book",
                "asset_id": "yes26",
                "bids": [{"price": "0.30", "size": "100"}],
                "asks": [{"price": "0.35", "size": "100"}],
            }
        )
        cache.apply_message(
            {
                "event_type": "price_change",
                "asset_id": "yes26",
                "changes": [
                    {"side": "BUY", "price": "0.31", "size": "50"},
                    {"side": "SELL", "price": "0.35", "size": "0"},
                ],
            }
        )

        book = cache.latest_orderbook("yes26", max_age_seconds=1, now_monotonic=10.2)

        self.assertEqual(book.bids, [(0.31, 50.0), (0.3, 100.0)])
        self.assertEqual(book.asks, [])

    def test_best_bid_ask_update_and_stale_rejection(self):
        cache = OrderBookCache(monotonic_fn=_FakeMonotonic([20.0]))
        cache.apply_message(
            {
                "event_type": "best_bid_ask",
                "asset_id": "yes26",
                "best_bid": "0.42",
                "best_ask": "0.44",
            }
        )

        fresh = cache.latest_orderbook("yes26", max_age_seconds=0.25, now_monotonic=20.2)
        self.assertEqual(fresh.best_bid, 0.42)
        self.assertEqual(fresh.best_ask, 0.44)
        with self.assertRaises(BookCacheStale):
            cache.latest_orderbook("yes26", max_age_seconds=0.25, now_monotonic=20.3)

    def test_cache_persists_append_only_snapshots_for_market_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            cache = OrderBookCache(db=db, monotonic_fn=_FakeMonotonic([30.0, 30.1]))

            cache.apply_message(
                {
                    "event_type": "book",
                    "asset_id": "yes26",
                    "bids": [{"price": "0.30", "size": "100"}],
                    "asks": [{"price": "0.35", "size": "100"}],
                }
            )
            cache.apply_message(
                {
                    "event_type": "last_trade_price",
                    "asset_id": "yes26",
                    "price": "0.34",
                }
            )

            rows = db.execute(
                """
                select best_bid, best_ask, depth_json
                from orderbook_snapshots
                where outcome_id = 'yes26'
                order by id asc
                """
            ).fetchall()

            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["best_bid"], 0.30)
            self.assertEqual(rows[1]["best_ask"], 0.35)
            self.assertIn('"websocket_event_type": "last_trade_price"', rows[1]["depth_json"])

    def test_seed_reconnect_snapshot_replaces_existing_levels(self):
        cache = OrderBookCache(monotonic_fn=_FakeMonotonic([40.0, 40.1]))
        cache.seed(OrderBook("yes26", bids=[(0.30, 100)], asks=[(0.35, 100)], tick_size=0.01, min_order_size=5))
        cache.apply_message(
            {
                "event_type": "book",
                "asset_id": "yes26",
                "bids": [{"price": "0.25", "size": "25"}],
                "asks": [{"price": "0.40", "size": "40"}],
            }
        )

        book = cache.latest_orderbook("yes26", max_age_seconds=1, now_monotonic=40.2)

        self.assertEqual(book.bids, [(0.25, 25.0)])
        self.assertEqual(book.asks, [(0.40, 40.0)])

    def test_lists_active_yes_no_tokens_for_market_subscription(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            _store_market(db, "2026-05-04", "yes-today", "no-today")
            _store_market(db, "2026-05-03", "yes-past", "no-past")

            tokens = list_active_market_token_ids(db, min_date_hkt="2026-05-04")

            self.assertEqual(tokens, ["yes-today", "no-today"])

    def test_lists_active_condition_ids_for_user_subscription(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            _store_market(db, "2026-05-04", "yes-today", "no-today")
            _store_market(db, "2026-05-03", "yes-past", "no-past")

            condition_ids = list_active_market_condition_ids(
                db, min_date_hkt="2026-05-04"
            )

            self.assertEqual(condition_ids, ["market-2026-05-04-yes-today"])

    def test_subscription_manager_emits_payload_only_when_tokens_change(self):
        manager = SubscriptionManager()

        first = manager.update(["yes26", "no26"])
        duplicate = manager.update(["no26", "yes26"])
        changed = manager.update(["yes26", "no26", "yes27"])

        self.assertEqual(first["assets_ids"], ["yes26", "no26"])
        self.assertIsNone(duplicate)
        self.assertEqual(changed["assets_ids"], ["yes26", "no26", "yes27"])


class _FakeMonotonic:
    def __init__(self, values):
        self._values = list(values)

    def __call__(self):
        if not self._values:
            raise AssertionError("fake monotonic exhausted")
        return self._values.pop(0)


def _store_market(db, target_date: str, yes_token_id: str, no_token_id: str) -> None:
    store_polymarket_event(
        db,
        TemperatureMarket(
            event_id=f"event-{target_date}-{yes_token_id}",
            event_slug=f"highest-temperature-in-hong-kong-on-{target_date}",
            title=f"Highest temperature in Hong Kong on {target_date}?",
            target_date=date.fromisoformat(target_date),
            outcomes=[
                Outcome(
                    market_id=f"market-{target_date}-{yes_token_id}",
                    label="26°C",
                    predicate=parse_outcome_label("26°C"),
                    yes_token_id=yes_token_id,
                    no_token_id=no_token_id,
                )
            ],
        ),
    )
