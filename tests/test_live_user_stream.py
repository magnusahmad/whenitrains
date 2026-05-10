import tempfile
import unittest
from pathlib import Path

from whenitrains.live_user_stream import apply_user_channel_event
from whenitrains.storage import connect, get_live_position, migrate, store_live_order


class LiveUserStreamTests(unittest.TestCase):
    def test_order_lifecycle_events_are_stored_independently_and_update_status(self):
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

            applied = apply_user_channel_event(
                db,
                {
                    "event_type": "order",
                    "id": "evt-placement",
                    "order_id": "order-1",
                    "asset_id": "yes25",
                    "status": "PLACEMENT",
                },
            )
            apply_user_channel_event(
                db,
                {
                    "event_type": "order",
                    "id": "evt-cancel",
                    "order_id": "order-1",
                    "asset_id": "yes25",
                    "status": "CANCELLATION",
                },
            )

            self.assertTrue(applied.stored)
            events = db.execute("select status from live_user_events order by id").fetchall()
            self.assertEqual([row["status"] for row in events], ["PLACEMENT", "CANCELLATION"])
            order = db.execute("select status from live_orders where clob_order_id = 'order-1'").fetchone()
            self.assertEqual(order["status"], "cancelled")
            self.assertIsNone(get_live_position(db, "yes25"))

    def test_order_lifecycle_status_fixtures_are_mapped(self):
        expected = {
            "PLACEMENT": "submitted",
            "UPDATE": "submitted",
            "CANCELLATION": "cancelled",
            "MATCHED": "filled",
            "MINED": "filled",
            "CONFIRMED": "filled",
            "RETRYING": "submitted",
            "FAILED": "failed",
        }
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            for status, local in expected.items():
                store_live_order(
                    db,
                    outcome_id="yes25",
                    side="BUY_YES",
                    action="BUY",
                    status="submitted",
                    clob_order_id=f"order-{status}",
                    requested_size_usd=5.0,
                    limit_price=0.40,
                )

                apply_user_channel_event(
                    db,
                    {
                        "event_type": "order",
                        "id": f"evt-{status}",
                        "order_id": f"order-{status}",
                        "asset_id": "yes25",
                        "status": status,
                    },
                )
                row = db.execute(
                    "select status from live_orders where clob_order_id = ?",
                    (f"order-{status}",),
                ).fetchone()

                self.assertEqual(row["status"], local)

    def test_matched_trade_event_applies_buy_delta_once(self):
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
            payload = {
                "event_type": "trade",
                "id": "trade-1",
                "order_id": "order-1",
                "asset_id": "yes25",
                "side": "BUY",
                "status": "MATCHED",
                "size": "12.5",
                "price": "0.40",
            }

            first = apply_user_channel_event(db, payload)
            second = apply_user_channel_event(db, payload)

            self.assertTrue(first.position_applied)
            self.assertFalse(second.position_applied)
            pos = get_live_position(db, "yes25")
            self.assertIsNotNone(pos)
            self.assertAlmostEqual(pos["net_shares"], 12.5)
            order = db.execute("select status, fill_size_usd, fill_shares from live_orders").fetchone()
            self.assertEqual(order["status"], "filled")
            self.assertAlmostEqual(order["fill_size_usd"], 5.0)
            self.assertAlmostEqual(order["fill_shares"], 12.5)

    def test_submitted_order_converges_from_later_user_trade_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            db.close()

            restarted = connect(Path(tmp) / "test.db")
            store_live_order(
                restarted,
                outcome_id="yes25",
                side="BUY_YES",
                action="BUY",
                status="submitted",
                clob_order_id="order-1",
                requested_size_usd=5.0,
                limit_price=0.40,
            )

            result = apply_user_channel_event(
                restarted,
                {
                    "event_type": "trade",
                    "id": "trade-after-restart",
                    "order_id": "order-1",
                    "asset_id": "yes25",
                    "side": "BUY",
                    "status": "CONFIRMED",
                    "size": "12.5",
                    "price": "0.40",
                },
            )

            self.assertTrue(result.position_applied)
            pos = get_live_position(restarted, "yes25")
            self.assertIsNotNone(pos)
            self.assertAlmostEqual(pos["net_shares"], 12.5)
