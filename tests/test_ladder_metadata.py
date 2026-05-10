import tempfile
import unittest
from datetime import date
from pathlib import Path

from whenitrains.ladder_metadata import build_active_ladder_metadata
from whenitrains.markets import parse_outcome_label
from whenitrains.polymarket import OrderBook, Outcome, TemperatureMarket
from whenitrains.storage import (
    connect,
    migrate,
    store_orderbook,
    store_polymarket_event,
    upsert_paper_position,
)


class LadderMetadataTests(unittest.TestCase):
    def test_builds_token_side_budget_and_orderbook_metadata_for_active_ladder(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            store_polymarket_event(
                db,
                TemperatureMarket(
                    event_id="event",
                    event_slug="highest-temperature-in-hong-kong-on-2026-05-04",
                    title="Highest temperature",
                    target_date=date(2026, 5, 4),
                    outcomes=[
                        Outcome(
                            market_id="m29",
                            label="29°C",
                            predicate=parse_outcome_label("29°C"),
                            yes_token_id="yes29",
                            no_token_id="no29",
                        )
                    ],
                ),
            )
            store_orderbook(
                db,
                "yes29",
                OrderBook(
                    "yes29",
                    bids=[(0.39, 100)],
                    asks=[(0.41, 100)],
                    tick_size=0.001,
                    min_order_size=10,
                ),
            )
            store_orderbook(
                db,
                "no29",
                OrderBook(
                    "no29",
                    bids=[(0.58, 100)],
                    asks=[(0.60, 100)],
                    tick_size=0.01,
                    min_order_size=5,
                ),
            )
            upsert_paper_position(db, "yes29", 20.0, 0.40, 0.0)

            entries = build_active_ladder_metadata(
                db,
                target_date_hkt="2026-05-04",
                max_order_usd=250.0,
            )

            by_token = {entry.token_id: entry for entry in entries}
            self.assertEqual(set(by_token), {"yes29", "no29"})
            yes = by_token["yes29"]
            self.assertEqual(yes.side, "YES")
            self.assertEqual(yes.label, "29°C")
            self.assertEqual(yes.market_kind, "highest")
            self.assertEqual(yes.best_ask, 0.41)
            self.assertEqual(yes.tick_size, 0.001)
            self.assertEqual(yes.min_order_size, 10)
            self.assertTrue(yes.has_open_position)
            self.assertAlmostEqual(yes.held_shares, 20.0)
            self.assertAlmostEqual(yes.remaining_budget_usd, 242.0)
            self.assertFalse(yes.neg_risk)
            self.assertFalse(by_token["no29"].has_open_position)
            self.assertAlmostEqual(by_token["no29"].remaining_budget_usd, 250.0)


if __name__ == "__main__":
    unittest.main()
