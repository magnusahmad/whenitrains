import json
import unittest

from whenitrains.market_websocket import (
    POLYMARKET_MARKET_WS_URL,
    MarketWebSocketClient,
)
from whenitrains.orderbook_cache import OrderBookCache


class _FakeConnection:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)


class MarketWebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscribes_and_applies_market_messages_to_cache(self):
        connection = _FakeConnection(
            [
                json.dumps(
                    {
                        "event_type": "book",
                        "asset_id": "yes26",
                        "bids": [{"price": "0.40", "size": "15"}],
                        "asks": [{"price": "0.42", "size": "10"}],
                    }
                )
            ]
        )
        cache = OrderBookCache(monotonic_fn=lambda: 10.0, persist_snapshots=False)
        client = MarketWebSocketClient(
            cache=cache,
            token_ids_fn=lambda: ["yes26"],
            connect_factory=lambda url: connection,
        )

        applied = await client.run_once()

        self.assertEqual(applied, 1)
        self.assertEqual(client.status.connection_attempts, 1)
        self.assertTrue(client.status.connected_once)
        self.assertEqual(client.status.messages_applied, 1)
        self.assertIsNone(client.status.last_error)
        self.assertEqual(
            connection.sent,
            [
                {
                    "type": "market",
                    "assets_ids": ["yes26"],
                    "custom_feature_enabled": True,
                }
            ],
        )
        book = cache.latest_orderbook("yes26", max_age_seconds=1, now_monotonic=10.0)
        self.assertEqual(book.best_bid, 0.40)
        self.assertEqual(book.best_ask, 0.42)

    async def test_ignores_messages_for_unsubscribed_asset_ids(self):
        connection = _FakeConnection(
            [
                json.dumps(
                    [
                        {
                            "event_type": "price_change",
                            "asset_id": "0xnot-a-subscribed-token",
                            "changes": [],
                        },
                        {
                            "event_type": "book",
                            "asset_id": "yes26",
                            "bids": [{"price": "0.40", "size": "15"}],
                            "asks": [{"price": "0.42", "size": "10"}],
                        },
                    ]
                )
            ]
        )
        cache = OrderBookCache(monotonic_fn=lambda: 10.0, persist_snapshots=False)
        client = MarketWebSocketClient(
            cache=cache,
            token_ids_fn=lambda: ["yes26"],
            connect_factory=lambda url: connection,
        )

        applied = await client.run_once()

        self.assertEqual(applied, 1)
        self.assertEqual(client.status.messages_applied, 1)
        with self.assertRaises(KeyError):
            cache.latest_orderbook(
                "0xnot-a-subscribed-token", max_age_seconds=1, now_monotonic=10.0
            )

    async def test_empty_token_set_does_not_connect(self):
        connections = []
        cache = OrderBookCache(persist_snapshots=False)
        client = MarketWebSocketClient(
            cache=cache,
            token_ids_fn=lambda: [],
            connect_factory=lambda url: connections.append(url),
        )

        applied = await client.run_once()

        self.assertEqual(applied, 0)
        self.assertEqual(client.status.connection_attempts, 0)
        self.assertFalse(client.status.connected_once)
        self.assertEqual(connections, [])

    async def test_run_forever_records_connection_errors(self):
        class StopAfterError:
            def __init__(self):
                self.calls = 0

            def is_set(self):
                self.calls += 1
                return self.calls > 2

        cache = OrderBookCache(persist_snapshots=False)
        client = MarketWebSocketClient(
            cache=cache,
            token_ids_fn=lambda: ["yes26"],
            connect_factory=lambda url: (_ for _ in ()).throw(RuntimeError("offline")),
        )

        await client.run_forever(StopAfterError(), reconnect_delay_seconds=0)

        self.assertEqual(client.status.connection_attempts, 1)
        self.assertFalse(client.status.connected_once)
        self.assertIn("RuntimeError: offline", client.status.last_error)

    def test_default_url_is_polymarket_market_channel(self):
        self.assertEqual(
            POLYMARKET_MARKET_WS_URL,
            "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        )


if __name__ == "__main__":
    unittest.main()
