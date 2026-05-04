import tempfile
import unittest
from pathlib import Path

from whenitrains.hko import HkoForecast, HkoObservation, HKT, OcfForecastSample
from whenitrains.markets import parse_outcome_label
from whenitrains.polymarket import OrderBook, Outcome, TemperatureMarket
from whenitrains.storage import (
    connect,
    list_hko_update_times,
    migrate,
    record_hko_update_minute,
    store_hko_forecasts,
    store_hko_observation,
    store_ocf_forecast_samples,
    store_orderbook,
    store_polymarket_event,
    store_raw_snapshot,
    store_signal,
)
from datetime import date, datetime


class StorageTests(unittest.TestCase):
    def test_store_raw_snapshot_keeps_every_fetch_with_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            first = store_raw_snapshot(
                db,
                "hko",
                "endpoint",
                '{"a":1}',
                {"Date": "Mon, 04 May 2026 05:45:06 GMT", "ETag": "abc"},
            )
            second = store_raw_snapshot(db, "hko", "endpoint", '{"a":1}')
            self.assertEqual(first.content_hash, second.content_hash)
            self.assertNotEqual(first.id, second.id)
            count = db.execute("select count(*) from raw_snapshots").fetchone()[0]
            self.assertEqual(count, 2)
            row = db.execute(
                "select http_date, http_etag from raw_snapshots where id = ?",
                (first.id,),
            ).fetchone()
            self.assertEqual(row["http_date"], "Mon, 04 May 2026 05:45:06 GMT")
            self.assertEqual(row["http_etag"], "abc")

    def test_record_hko_update_minute_upserts_learned_scheduler_times(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            update_time = datetime(2026, 5, 4, 13, 12, 19, tzinfo=HKT)

            record_hko_update_minute(
                db, "ocf_station", update_time, {"kind": "http_Last-Modified"}
            )
            record_hko_update_minute(
                db, "ocf_station", update_time, {"kind": "payload_LastModified"}
            )

            rows = db.execute(
                "select update_minute_hkt, seen_count from hko_source_update_minutes"
            ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["update_minute_hkt"], "13:12")
            self.assertEqual(rows[0]["seen_count"], 2)
            self.assertEqual([item.strftime("%H:%M") for item in list_hko_update_times(db, "ocf_station")], ["13:12"])

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

    def test_store_ocf_forecast_samples_keeps_daily_and_hourly_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            snapshot = store_raw_snapshot(db, "hko", "ocf", "{}")

            store_ocf_forecast_samples(
                db,
                snapshot.id,
                [
                    OcfForecastSample(
                        forecast_date_hkt=date(2026, 5, 4),
                        forecast_min_c=22,
                        forecast_max_c=27,
                        raw_min_c=21.9,
                        raw_max_c=27.1,
                        hourly_temperatures=[
                            {
                                "forecast_hour_hkt": "2026-05-04T13:00:00+08:00",
                                "temperature_c": 27.1,
                            }
                        ],
                        raw={"ForecastDate": "20260504"},
                    )
                ],
            )

            row = db.execute(
                """
                select forecast_date_hkt, forecast_max_c, raw_max_c, hourly_temperatures_json
                from ocf_forecast_samples
                """
            ).fetchone()
            self.assertEqual(row["forecast_date_hkt"], "2026-05-04")
            self.assertEqual(row["forecast_max_c"], 27)
            self.assertEqual(row["raw_max_c"], 27.1)
            self.assertIn("2026-05-04T13:00:00+08:00", row["hourly_temperatures_json"])

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
