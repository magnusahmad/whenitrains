import tempfile
import unittest
from pathlib import Path

from whenitrains.hko import HkoForecast, HkoObservation, HKT
from whenitrains.markets import parse_outcome_label
from whenitrains.polymarket import OrderBook, Outcome, TemperatureMarket
from whenitrains.storage import (
    connect,
    migrate,
    store_hko_forecasts,
    store_hko_observation,
    store_orderbook,
    store_polymarket_event,
    store_raw_snapshot,
    store_signal,
)
from datetime import date, datetime


class StorageTests(unittest.TestCase):
    def test_store_raw_snapshot_deduplicates_by_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            first = store_raw_snapshot(db, "hko", "endpoint", '{"a":1}')
            second = store_raw_snapshot(db, "hko", "endpoint", '{"a":1}')
            self.assertEqual(first.content_hash, second.content_hash)
            count = db.execute("select count(*) from raw_snapshots").fetchone()[0]
            self.assertEqual(count, 1)

    def test_persist_hko_market_orderbook_and_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            snapshot = store_raw_snapshot(db, "hko", "endpoint", "{}")
            store_hko_observation(
                db,
                snapshot.id,
                HkoObservation(
                    observed_at_hkt=datetime(2026, 5, 3, 20, 30, tzinfo=HKT),
                    station="HK Observatory",
                    since_midnight_max_c=29.6,
                    since_midnight_min_c=23.4,
                    raw={},
                ),
            )
            store_hko_forecasts(
                db,
                snapshot.id,
                [
                    HkoForecast(
                        source_type="flw_page",
                        forecast_date_hkt=date(2026, 5, 4),
                        forecast_min_c=None,
                        forecast_max_c=25,
                        update_time="2026-05-04T00:45:00+08:00",
                        parse_warning=False,
                    )
                ],
            )
            market = TemperatureMarket(
                event_id="event",
                event_slug="slug",
                title="Highest temperature",
                target_date=date(2026, 5, 4),
                outcomes=[
                    Outcome(
                        market_id="m1",
                        label="25°C",
                        predicate=parse_outcome_label("25°C"),
                        yes_token_id="yes",
                        no_token_id="no",
                    )
                ],
            )
            store_polymarket_event(db, market)
            store_orderbook(
                db,
                "yes",
                OrderBook("yes", bids=[(0.36, 10)], asks=[(0.38, 10)], tick_size=0.01, min_order_size=5),
            )
            store_signal(
                db,
                market_id="m1",
                trigger_type="forecast_change",
                current_max_c=24.5,
                forecast_max_c=25,
                affected_outcomes={"25°C": "INCREASES_YES_PROBABILITY"},
                price_response={"25°C": "PRICE_NOT_MOVED_WITH_EVENT"},
                notes="test",
            )
            self.assertEqual(db.execute("select count(*) from hko_current_observations").fetchone()[0], 1)
            self.assertEqual(db.execute("select count(*) from hko_forecasts").fetchone()[0], 1)
            forecast = db.execute(
                "select update_time, parse_warning from hko_forecasts"
            ).fetchone()
            self.assertEqual(forecast["update_time"], "2026-05-04T00:45:00+08:00")
            self.assertEqual(forecast["parse_warning"], 0)
            self.assertEqual(db.execute("select count(*) from outcomes").fetchone()[0], 1)
            self.assertEqual(db.execute("select count(*) from orderbook_snapshots").fetchone()[0], 1)
            self.assertEqual(db.execute("select count(*) from signals").fetchone()[0], 1)

    def test_latest_orderbook_sorts_executable_prices(self):
        from whenitrains.storage import latest_orderbook

        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            store_orderbook(
                db,
                "yes",
                OrderBook("yes", bids=[(0.20, 5), (0.30, 5)], asks=[(0.50, 5), (0.40, 5)], tick_size=0.01, min_order_size=5),
            )
            book = latest_orderbook(db, "yes")
            self.assertEqual(book.bids[0], (0.30, 5))
            self.assertEqual(book.asks[0], (0.40, 5))


if __name__ == "__main__":
    unittest.main()
