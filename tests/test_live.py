import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from whenitrains.live import (
    LiveConfig,
    execute_live_buy,
    execute_live_sell,
    load_live_config,
)
from whenitrains.storage import (
    connect,
    get_live_position,
    live_dashboard_stats,
    live_total_open_exposure,
    migrate,
    set_live_setting,
)


class FakeClobClient:
    def __init__(self, fill=True):
        self.fill = fill
        self.buys = []
        self.sells = []

    def signer_address(self):
        return "0xsigner"

    def balance_usd(self):
        return 100.0

    def allowance_ok(self):
        return True

    def buy_fak(self, token_id, price, size_usd):
        self.buys.append((token_id, price, size_usd))
        return {"orderID": "buy-1", "status": "matched"}

    def sell_fak(self, token_id, price, shares):
        self.sells.append((token_id, price, shares))
        return {"orderID": "sell-1", "status": "matched"}

    def reconcile_order(self, order_id, token_id):
        if not self.fill:
            return {"order_id": order_id, "token_id": token_id, "status": "submitted"}
        return {"order_id": order_id, "token_id": token_id, "status": "filled"}


class LiveTests(unittest.TestCase):
    def test_migrate_adds_live_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)

            tables = {
                row["name"]
                for row in db.execute(
                    "select name from sqlite_master where type = 'table'"
                )
            }
            self.assertIn("live_orders", tables)
            self.assertIn("live_positions", tables)
            self.assertIn("live_settings", tables)

    def test_load_live_config_requires_prederived_credentials_and_keychain_key(self):
        env = {
            "WHENITRAINS_TRADING_MODE": "live",
            "POLYMARKET_SIGNATURE_TYPE": "1",
            "POLYMARKET_FUNDER_ADDRESS": "0xfunder",
            "POLYMARKET_API_KEY": "api",
            "POLYMARKET_API_SECRET": "secret",
            "POLYMARKET_API_PASSPHRASE": "passphrase",
        }
        with patch("whenitrains.live.read_keychain_secret", return_value="0xabc"):
            config = load_live_config(env)

        self.assertEqual(config.signature_type, 1)
        self.assertEqual(config.private_key, "0xabc")
        self.assertEqual(config.api_key, "api")

    def test_execute_live_buy_uses_fak_and_updates_position_from_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            client = FakeClobClient()

            result = execute_live_buy(
                db,
                client,
                token_id="yes25",
                side="YES",
                size_usd=5,
                asks=[(0.40, 100)],
                reason="test live buy",
                max_price=0.40,
                min_fill_usd=5,
                order_cap_usd=5,
                label="25C",
            )

            self.assertEqual(result.status, "filled")
            self.assertEqual(client.buys, [("yes25", 0.40, 5.0)])
            pos = get_live_position(db, "yes25")
            self.assertIsNotNone(pos)
            self.assertAlmostEqual(pos["net_shares"], 12.5)
            self.assertAlmostEqual(live_total_open_exposure(db), 5.0)

    def test_execute_live_buy_blocks_when_kill_switch_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            set_live_setting(db, "block_new_entries", True)
            client = FakeClobClient()

            result = execute_live_buy(
                db,
                client,
                token_id="yes25",
                side="YES",
                size_usd=5,
                asks=[(0.40, 100)],
                reason="test live buy",
                max_price=0.40,
                min_fill_usd=5,
                order_cap_usd=5,
                label="25C",
            )

            self.assertEqual(result.status, "blocked")
            self.assertEqual(client.buys, [])

    def test_execute_live_sell_closes_position_from_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            client = FakeClobClient()
            execute_live_buy(
                db,
                client,
                token_id="yes25",
                side="YES",
                size_usd=5,
                asks=[(0.40, 100)],
                reason="test live buy",
                max_price=0.40,
                min_fill_usd=5,
                order_cap_usd=5,
                label="25C",
            )

            result = execute_live_sell(
                db,
                client,
                token_id="yes25",
                bids=[(0.45, 100)],
                reason="test live sell",
                label="25C",
            )

            self.assertEqual(result.status, "filled")
            self.assertEqual(client.sells, [("yes25", 0.45, 12.5)])
            pos = get_live_position(db, "yes25")
            self.assertAlmostEqual(pos["net_shares"], 0.0)
            self.assertGreater(pos["realized_pnl"], 0)

    def test_live_dashboard_stats_separate_from_paper(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            stats = live_dashboard_stats(db)

            self.assertEqual(stats["mode"], "live")
            self.assertEqual(stats["open_positions"], 0)
            self.assertFalse(stats["block_new_entries"])


if __name__ == "__main__":
    unittest.main()
