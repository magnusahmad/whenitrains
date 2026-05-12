import tempfile
import unittest
import json
from pathlib import Path

from whenitrains.live_user_stream import (
    apply_user_channel_event,
    reconcile_unapplied_user_trades,
)
from whenitrains.storage import connect, get_live_position, migrate, store_live_order


class LiveUserStreamTests(unittest.TestCase):
    def connect_db(self, path: Path):
        db = connect(path)
        self.addCleanup(db.close)
        return db

    def test_order_lifecycle_events_are_stored_independently_and_update_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self.connect_db(Path(tmp) / "test.db")
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

    def test_order_lifecycle_uses_raw_id_for_order_events_without_order_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self.connect_db(Path(tmp) / "test.db")
            migrate(db)
            store_live_order(
                db,
                outcome_id="yes25",
                side="SELL",
                action="SELL",
                status="submitted",
                clob_order_id="order-raw-id",
                requested_shares=10.0,
                limit_price=0.40,
            )

            result = apply_user_channel_event(
                db,
                {
                    "event_type": "order",
                    "id": "order-raw-id",
                    "asset_id": "yes25",
                    "side": "SELL",
                    "status": (
                        "CANCELED_no orders found to match with FAK order. "
                        "FAK orders are partially filled or killed if no match is found."
                    ),
                },
            )

            self.assertTrue(result.stored)
            event = db.execute("select clob_order_id from live_user_events").fetchone()
            self.assertEqual(event["clob_order_id"], "order-raw-id")
            order = db.execute(
                "select status from live_orders where clob_order_id = 'order-raw-id'"
            ).fetchone()
            self.assertEqual(order["status"], "canceled")

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
            db = self.connect_db(Path(tmp) / "test.db")
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

    def test_stored_order_lifecycle_events_repair_old_blank_order_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self.connect_db(Path(tmp) / "test.db")
            migrate(db)
            store_live_order(
                db,
                outcome_id="yes25",
                side="SELL",
                action="SELL",
                status="submitted",
                clob_order_id="order-from-raw-json",
                requested_shares=10.0,
                limit_price=0.40,
            )
            db.execute(
                """
                insert into live_user_events
                (event_id, received_at_utc, event_type, clob_order_id, outcome_id,
                 status, side, price, size, raw_event_json)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "old-event",
                    "2026-05-12T00:00:00+00:00",
                    "order",
                    "",
                    "yes25",
                    (
                        "CANCELED_NO ORDERS FOUND TO MATCH WITH FAK ORDER. "
                        "FAK ORDERS ARE PARTIALLY FILLED OR KILLED IF NO MATCH IS FOUND."
                    ),
                    "SELL",
                    0.40,
                    None,
                    json.dumps(
                        {
                            "event_type": "order",
                            "id": "order-from-raw-json",
                            "asset_id": "yes25",
                            "status": (
                                "CANCELED_no orders found to match with FAK order. "
                                "FAK orders are partially filled or killed if no match is found."
                            ),
                        }
                    ),
                ),
            )
            db.commit()

            result = reconcile_unapplied_user_trades(db)

            self.assertEqual(result.checked, 0)
            order = db.execute(
                "select status from live_orders where clob_order_id = 'order-from-raw-json'"
            ).fetchone()
            self.assertEqual(order["status"], "canceled")
            event = db.execute("select clob_order_id from live_user_events").fetchone()
            self.assertEqual(event["clob_order_id"], "order-from-raw-json")

    def test_matched_trade_event_applies_buy_delta_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self.connect_db(Path(tmp) / "test.db")
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
            db = self.connect_db(Path(tmp) / "test.db")
            migrate(db)
            db.close()

            restarted = self.connect_db(Path(tmp) / "test.db")
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

    def test_unapplied_user_trade_is_marked_after_rest_reconcile_filled_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self.connect_db(Path(tmp) / "test.db")
            migrate(db)
            apply_user_channel_event(
                db,
                {
                    "event_type": "trade",
                    "id": "trade-before-order",
                    "order_id": "order-1",
                    "asset_id": "yes25",
                    "side": "BUY",
                    "status": "MATCHED",
                    "size": "12.5",
                    "price": "0.40",
                },
            )
            store_live_order(
                db,
                outcome_id="yes25",
                side="BUY_YES",
                action="BUY",
                status="filled",
                clob_order_id="order-1",
                fill_price=0.40,
                fill_size_usd=5.0,
                fill_shares=12.5,
            )

            result = reconcile_unapplied_user_trades(db)

            self.assertEqual(result.checked, 1)
            self.assertEqual(result.applied, 1)
            event = db.execute(
                "select applied_position_delta from live_user_events"
            ).fetchone()
            self.assertEqual(event["applied_position_delta"], 1)
            self.assertIsNone(get_live_position(db, "yes25"))
