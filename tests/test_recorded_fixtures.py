import json
import tempfile
import unittest
from pathlib import Path

from whenitrains.hko import parse_aws_gis_current_temperature
from whenitrains.polymarket import (
    parse_event_markets,
    parse_orderbook,
    resolution_rules_match_expected,
)
from whenitrains.storage import (
    connect,
    migrate,
    store_hko_current_temperature,
    store_orderbook,
    store_polymarket_event,
    store_raw_snapshot,
)


FIXTURES = Path(__file__).parent / "fixtures" / "low_latency"


class RecordedFixtureIntegrationTests(unittest.TestCase):
    def test_recorded_hko_gamma_and_clob_payloads_parse_and_persist(self):
        aws_payload = (FIXTURES / "hko_aws_gis_readings.txt").read_text()
        gamma_event = json.loads((FIXTURES / "gamma_highest_event.json").read_text())
        clob_book = json.loads((FIXTURES / "clob_book_yes25.json").read_text())

        observation = parse_aws_gis_current_temperature(aws_payload)
        markets = parse_event_markets(gamma_event)
        orderbook = parse_orderbook(clob_book)

        self.assertEqual(observation.observed_at_hkt.isoformat(), "2026-05-11T14:30:00+08:00")
        self.assertEqual(observation.temperature_c, 28.7)
        self.assertEqual(observation.since_midnight_max_c, 29.4)
        self.assertEqual(markets[0].event_slug, "highest-temperature-in-hong-kong-on-may-11-2026")
        self.assertTrue(resolution_rules_match_expected(markets[0].resolution_rules_text))
        self.assertEqual(markets[0].outcomes[0].yes_token_id, "YES25RECORDED")
        self.assertEqual(orderbook.best_bid, 0.35)
        self.assertEqual(orderbook.best_ask, 0.37)

        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            snapshot = store_raw_snapshot(db, "hko", "aws_gis_actual", aws_payload)
            store_hko_current_temperature(db, snapshot.id, observation)
            for market in markets:
                store_polymarket_event(db, market)
            store_orderbook(db, orderbook.token_id, orderbook, metadata={"source": "recorded_fixture"})

            self.assertEqual(
                db.execute("select count(*) from hko_current_observations").fetchone()[0],
                1,
            )
            self.assertEqual(db.execute("select count(*) from markets").fetchone()[0], 1)
            self.assertEqual(db.execute("select count(*) from outcomes").fetchone()[0], 1)
            self.assertEqual(
                db.execute("select count(*) from orderbook_snapshots").fetchone()[0],
                1,
            )
            row = db.execute(
                """
                select o.yes_token_id, s.best_bid, s.best_ask, s.depth_json
                from orderbook_snapshots s
                join outcomes o on o.yes_token_id = s.outcome_id
                """
            ).fetchone()
            self.assertEqual(row["yes_token_id"], "YES25RECORDED")
            self.assertEqual(row["best_bid"], 0.35)
            self.assertEqual(row["best_ask"], 0.37)
            self.assertIn('"source": "recorded_fixture"', row["depth_json"])
            db.close()


if __name__ == "__main__":
    unittest.main()
