import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from whenitrains.storage import connect, get_live_position, migrate, store_live_order
from whenitrains.user_websocket import (
    POLYMARKET_USER_WS_URL,
    UserWebSocketAuth,
    UserWebSocketClient,
)


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


class UserWebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscribes_and_applies_user_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            store_live_order(
                db,
                outcome_id="yes25",
                side="BUY_YES",
                action="BUY",
                status="submitted",
                clob_order_id="order-1",
                requested_size_usd=5.0,
                limit_price=0.40,
            )
            connection = _FakeConnection(
                [
                    json.dumps(
                        {
                            "event_type": "trade",
                            "id": "trade-1",
                            "order_id": "order-1",
                            "asset_id": "yes25",
                            "side": "BUY",
                            "status": "MATCHED",
                            "size": "12.5",
                            "price": "0.40",
                        }
                    )
                ]
            )
            client = UserWebSocketClient(
                db=db,
                auth=UserWebSocketAuth(
                    api_key="key",
                    api_secret="secret",
                    api_passphrase="passphrase",
                ),
                market_ids_fn=lambda: ["condition-1"],
                connect_factory=lambda url: connection,
            )

            applied = await client.run_once()

            self.assertEqual(applied, 1)
            self.assertEqual(
                connection.sent,
                [
                    {
                        "auth": {
                            "apiKey": "key",
                            "secret": "secret",
                            "passphrase": "passphrase",
                        },
                        "markets": ["condition-1"],
                        "type": "user",
                    }
                ],
            )
            pos = get_live_position(db, "yes25")
            self.assertIsNotNone(pos)
            self.assertAlmostEqual(pos["net_shares"], 12.5)

    def test_default_url_is_polymarket_user_channel(self):
        self.assertEqual(
            POLYMARKET_USER_WS_URL,
            "wss://ws-subscriptions-clob.polymarket.com/ws/user",
        )

    def test_close_releases_owned_database_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            client = UserWebSocketClient(
                db=db,
                auth=UserWebSocketAuth("key", "secret", "passphrase"),
                market_ids_fn=lambda: [],
                connect_factory=lambda url: None,
            )

            client.close()

            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute("select 1")


if __name__ == "__main__":
    unittest.main()
