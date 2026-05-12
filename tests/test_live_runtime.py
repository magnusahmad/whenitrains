import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from whenitrains.live import LiveConfig
from whenitrains.live_runtime import LiveWebSocketRuntime
from whenitrains.market_websocket import WebSocketConnectionStatus
from whenitrains.storage import connect, migrate, store_live_order


class _BlockingClient:
    def __init__(self):
        self.started = 0
        self.status = WebSocketConnectionStatus()

    async def run_forever(self, stop_event, *, reconnect_delay_seconds=1.0):
        self.started += 1
        self.status.connection_attempts += 1
        self.status.connected_once = True
        while not stop_event.is_set():
            await asyncio.sleep(0.001)


class LiveRuntimeTests(unittest.TestCase):
    def test_runtime_starts_and_stops_market_and_user_clients(self):
        market = _BlockingClient()
        user = _BlockingClient()
        runtime = LiveWebSocketRuntime(
            market_client_factory=lambda cache: market,
            user_client_factory=lambda: user,
        )

        runtime.start()
        deadline = time.monotonic() + 1
        while len(runtime.client_statuses) < 2 and time.monotonic() < deadline:
            time.sleep(0.001)
        runtime.stop(timeout=2)

        self.assertEqual(market.started, 1)
        self.assertEqual(user.started, 1)
        self.assertEqual(len(runtime.client_statuses), 2)
        self.assertFalse(runtime.running)

    def test_all_running_requires_both_websocket_workers_alive(self):
        market = _BlockingClient()

        class ExitingClient:
            async def run_forever(self, stop_event, *, reconnect_delay_seconds=1.0):
                return None

        runtime = LiveWebSocketRuntime(
            market_client_factory=lambda cache: market,
            user_client_factory=ExitingClient,
        )

        runtime.start()
        deadline = time.monotonic() + 1
        while runtime.all_running and time.monotonic() < deadline:
            time.sleep(0.001)

        self.assertFalse(runtime.all_running)
        runtime.stop(timeout=2)

    def test_default_user_client_uses_own_database_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            store_live_order(
                db,
                outcome_id="yes25",
                side="BUY_YES",
                action="BUY",
                status="submitted",
                clob_order_id="order-1",
            )
            config = LiveConfig(
                trading_mode="live",
                private_key="private",
                signature_type=1,
                funder_address="0xfunder",
                api_key="key",
                api_secret="secret",
                api_passphrase="passphrase",
            )
            runtime = LiveWebSocketRuntime.for_live_scheduler(
                db_path=db_path,
                config=config,
                min_date_hkt="2026-05-04",
            )

            user_client = runtime.user_client_factory()

            self.assertIsNot(user_client.db, db)
            self.assertEqual(user_client.auth.api_key, "key")
            user_client.db.close()
            db.close()

    def test_live_scheduler_market_cache_persists_websocket_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            config = LiveConfig(
                trading_mode="live",
                private_key="private",
                signature_type=1,
                funder_address="0xfunder",
                api_key="key",
                api_secret="secret",
                api_passphrase="passphrase",
            )
            runtime = LiveWebSocketRuntime.for_live_scheduler(
                db_path=db_path,
                config=config,
                min_date_hkt="2026-05-04",
            )

            runtime.book_cache.apply_message(
                {
                    "event_type": "book",
                    "asset_id": "yes30",
                    "bids": [{"price": "0.36", "size": "100"}],
                    "asks": [{"price": "0.37", "size": "100"}],
                }
            )

            row = db.execute(
                """
                select best_bid, best_ask, depth_json
                from orderbook_snapshots
                where outcome_id = 'yes30'
                """
            ).fetchone()

            self.assertIsNotNone(row)
            self.assertEqual(row["best_bid"], 0.36)
            self.assertEqual(row["best_ask"], 0.37)
            self.assertIn('"source": "polymarket_market_websocket"', row["depth_json"])
            db.close()


if __name__ == "__main__":
    unittest.main()
