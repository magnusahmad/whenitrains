import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from sqlite3 import ProgrammingError
from unittest.mock import patch

from whenitrains.hko import (
    HkoCurrentTemperature,
    HkoForecast,
    HkoObservation,
    HKT,
    OcfForecastSample,
)
from whenitrains.markets import parse_outcome_label
from whenitrains.polymarket import OrderBook, Outcome, TemperatureMarket
from whenitrains.storage import (
    BackupResult,
    backup_sqlite_database,
    connect,
    ensure_recent_sqlite_backup,
    is_clob_tradeable_token,
    list_active_market_token_ids,
    list_hko_update_times,
    find_outcome_by_label_and_filters,
    list_outcomes_from_date,
    list_tradeable_forecast_dates,
    mark_clob_tokens_untradeable,
    migrate,
    record_hko_update_minute,
    store_hko_current_temperature,
    store_hko_forecasts,
    store_hko_observation,
    store_ocf_forecast_samples,
    store_orderbook,
    store_polymarket_event,
    store_raw_snapshot,
    store_signal,
)


def _plan_text(rows):
    return "\n".join(row["detail"] for row in rows)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self._opened_dbs = []
        original_connect = connect

        def tracked_connect(*args, **kwargs):
            db = original_connect(*args, **kwargs)
            self._opened_dbs.append(db)
            return db

        self._connect_patcher = patch(f"{__name__}.connect", tracked_connect)
        self._connect_patcher.start()
        self.addCleanup(self._connect_patcher.stop)
        self.addCleanup(self._close_opened_dbs)

    def _close_opened_dbs(self):
        for db in reversed(self._opened_dbs):
            try:
                db.close()
            except ProgrammingError:
                pass

    def test_migrate_creates_scheduler_latency_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)

            index_names = {
                row["name"]
                for row in db.execute(
                    "select name from sqlite_master where type = 'index'"
                )
            }

            self.assertIn("idx_orderbook_snapshots_latest", index_names)
            self.assertIn("idx_ocf_forecast_samples_latest", index_names)
            self.assertIn("idx_hko_forecasts_latest", index_names)
            self.assertIn("idx_hko_current_observations_latest", index_names)
            self.assertIsNotNone(
                db.execute(
                    """
                    select name from sqlite_master
                    where type = 'table' and name = 'orderbook_latest'
                    """
                ).fetchone()
            )

    def test_latest_scheduler_queries_use_latency_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)

            plans = {
                "orderbook": db.execute(
                    """
                    explain query plan
                    select depth_json
                    from orderbook_snapshots
                    where outcome_id = ?
                    order by fetched_at_utc desc, id desc
                    limit 1
                    """,
                    ("yes",),
                ).fetchall(),
                "ocf": db.execute(
                    """
                    explain query plan
                    select fetched_at_utc
                    from ocf_forecast_samples
                    where forecast_date_hkt = ?
                    order by fetched_at_utc desc, id desc
                    limit 1
                    """,
                    ("2026-05-08",),
                ).fetchall(),
                "hko_forecast": db.execute(
                    """
                    explain query plan
                    select forecast_date_hkt, forecast_max_c, update_time, parse_warning, id
                    from hko_forecasts
                    where source_type = 'ocf_station'
                      and forecast_date_hkt = ?
                      and forecast_max_c is not null
                      and coalesce(parse_warning, 0) = 0
                    order by id desc
                    limit 1
                    """,
                    ("2026-05-08",),
                ).fetchall(),
                "observation": db.execute(
                    """
                    explain query plan
                    select observed_at_hkt, since_midnight_max_c, station, id
                    from hko_current_observations
                    where since_midnight_max_c is not null
                      and observed_at_hkt >= ?
                      and observed_at_hkt < ?
                    order by id desc
                    limit 1
                    """,
                    ("2026-05-08", "2026-05-09"),
                ).fetchall(),
            }

            self.assertIn("idx_orderbook_snapshots_latest", _plan_text(plans["orderbook"]))
            self.assertIn("idx_ocf_forecast_samples_latest", _plan_text(plans["ocf"]))
            self.assertIn("idx_hko_forecasts_latest", _plan_text(plans["hko_forecast"]))
            self.assertIn(
                "idx_hko_current_observations_latest",
                _plan_text(plans["observation"]),
            )

    def test_backup_sqlite_database_creates_integrity_checked_copy_and_prunes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "live.sqlite3"
            backup_dir = Path(tmp) / "backups"
            db = connect(db_path)
            migrate(db)
            db.execute("insert into risk_events (created_at_utc, event_type, severity, details_json) values (?, ?, ?, ?)", ("now", "test", "info", "{}"))
            db.commit()
            db.close()

            first = backup_sqlite_database(db_path, backup_dir=backup_dir, keep=2)
            second = backup_sqlite_database(db_path, backup_dir=backup_dir, keep=2)
            third = backup_sqlite_database(db_path, backup_dir=backup_dir, keep=2)

            backups = sorted(backup_dir.glob("live-*.sqlite3"))
            self.assertEqual(len(backups), 2)
            self.assertNotIn(first, backups)
            self.assertIn(second, backups)
            self.assertIn(third, backups)
            restored = connect(third)
            count = restored.execute("select count(*) from risk_events").fetchone()[0]
            self.assertEqual(count, 1)

    def test_ensure_recent_sqlite_backup_reuses_fresh_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "live.sqlite3"
            backup_dir = Path(tmp) / "backups"
            db = connect(db_path)
            migrate(db)
            db.close()

            first = ensure_recent_sqlite_backup(
                db_path,
                backup_dir=backup_dir,
                min_interval=timedelta(hours=6),
            )
            second = ensure_recent_sqlite_backup(
                db_path,
                backup_dir=backup_dir,
                min_interval=timedelta(hours=6),
            )

            self.assertIsInstance(first, BackupResult)
            self.assertTrue(first.created)
            self.assertFalse(second.created)
            self.assertEqual(second.path, first.path)
            self.assertEqual(list(backup_dir.glob("live-*.sqlite3")), [first.path])

    def test_ensure_recent_sqlite_backup_creates_when_backup_is_too_old(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "live.sqlite3"
            backup_dir = Path(tmp) / "backups"
            db = connect(db_path)
            migrate(db)
            db.close()
            first = ensure_recent_sqlite_backup(
                db_path,
                backup_dir=backup_dir,
                min_interval=timedelta(hours=6),
            )
            old_mtime = (datetime.now(timezone.utc) - timedelta(hours=7)).timestamp()
            os.utime(first.path, (old_mtime, old_mtime))

            second = ensure_recent_sqlite_backup(
                db_path,
                backup_dir=backup_dir,
                min_interval=timedelta(hours=6),
            )

            self.assertTrue(second.created)
            self.assertNotEqual(second.path, first.path)
            self.assertEqual(len(list(backup_dir.glob("live-*.sqlite3"))), 2)

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

    def test_store_raw_snapshot_records_fetch_timing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)

            snapshot = store_raw_snapshot(
                db,
                "hko",
                "endpoint",
                "{}",
                fetch_started_at_utc="2026-05-11T00:00:00+00:00",
                headers_received_at_utc="2026-05-11T00:00:00.050000+00:00",
                payload_received_at_utc="2026-05-11T00:00:00.125000+00:00",
                response_elapsed_ms=125.4,
            )

            row = db.execute(
                """
                select fetch_started_at_utc, headers_received_at_utc,
                       payload_received_at_utc, response_elapsed_ms
                from raw_snapshots
                where id = ?
                """,
                (snapshot.id,),
            ).fetchone()
            self.assertEqual(row["fetch_started_at_utc"], "2026-05-11T00:00:00+00:00")
            self.assertEqual(row["headers_received_at_utc"], "2026-05-11T00:00:00.050000+00:00")
            self.assertEqual(row["payload_received_at_utc"], "2026-05-11T00:00:00.125000+00:00")
            self.assertAlmostEqual(row["response_elapsed_ms"], 125.4)

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

    def test_find_outcome_by_label_requires_filters_for_duplicate_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            high = TemperatureMarket(
                event_id="high",
                event_slug="highest-temperature-in-hong-kong-on-may-7-2026",
                title="High",
                target_date=date(2026, 5, 7),
                outcomes=[
                    Outcome("m_high", "29°C", parse_outcome_label("29°C"), "high_yes", "high_no")
                ],
            )
            low = TemperatureMarket(
                event_id="low",
                event_slug="lowest-temperature-in-hong-kong-on-may-7-2026",
                title="Low",
                target_date=date(2026, 5, 7),
                outcomes=[
                    Outcome("m_low", "29°C", parse_outcome_label("29°C"), "low_yes", "low_no")
                ],
            )
            store_polymarket_event(db, high)
            store_polymarket_event(db, low)

            with self.assertRaises(ValueError):
                find_outcome_by_label_and_filters(db, "29°C")
            row = find_outcome_by_label_and_filters(
                db, "29°C", target_date_hkt="2026-05-07", slug_contains="highest"
            )
            self.assertEqual(row["yes_token_id"], "high_yes")

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

    def test_store_hko_current_temperature_uses_temperature_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            snapshot = store_raw_snapshot(db, "hko", "rhrread", "{}")

            store_hko_current_temperature(
                db,
                snapshot.id,
                HkoCurrentTemperature(
                    observed_at_hkt=datetime(2026, 5, 5, 17, 2, tzinfo=HKT),
                    station="HKO",
                    temperature_c=21.4,
                    since_midnight_min_c=19.8,
                    since_midnight_max_c=23.1,
                    raw={"temperature_row": {"value": 21.4}},
                ),
            )

            row = db.execute(
                """
                select observed_at_hkt, station, temperature_c,
                       since_midnight_min_c, since_midnight_max_c
                from hko_current_observations
                """
            ).fetchone()
            self.assertEqual(row["observed_at_hkt"], "2026-05-05T17:02:00+08:00")
            self.assertEqual(row["station"], "HKO")
            self.assertEqual(row["temperature_c"], 21.4)
            self.assertEqual(row["since_midnight_min_c"], 19.8)
            self.assertEqual(row["since_midnight_max_c"], 23.1)

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

    def test_store_orderbook_maintains_latest_hot_row(self):
        from whenitrains.storage import latest_orderbook

        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)

            store_orderbook(
                db,
                "yes",
                OrderBook(
                    "yes",
                    bids=[(0.20, 5)],
                    asks=[(0.40, 5)],
                    tick_size=0.01,
                    min_order_size=5,
                ),
            )
            store_orderbook(
                db,
                "yes",
                OrderBook(
                    "yes",
                    bids=[(0.30, 5)],
                    asks=[(0.50, 5)],
                    tick_size=0.01,
                    min_order_size=5,
                ),
            )

            self.assertEqual(
                db.execute("select count(*) from orderbook_snapshots").fetchone()[0],
                2,
            )
            latest_row = db.execute(
                """
                select snapshot_id, best_bid, best_ask, depth_json
                from orderbook_latest
                where outcome_id = 'yes'
                """
            ).fetchone()
            self.assertIsNotNone(latest_row)
            self.assertEqual(latest_row["snapshot_id"], 2)
            self.assertEqual(latest_row["best_bid"], 0.30)
            self.assertEqual(latest_row["best_ask"], 0.50)

            book = latest_orderbook(db, "yes")
            self.assertEqual(book.bids, [(0.30, 5)])
            self.assertEqual(book.asks, [(0.50, 5)])

    def test_store_orderbook_does_not_move_hot_row_backwards(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            store_orderbook(
                db,
                "yes",
                OrderBook(
                    "yes",
                    bids=[(0.30, 5)],
                    asks=[(0.50, 5)],
                    tick_size=0.01,
                    min_order_size=5,
                ),
            )
            db.execute(
                """
                update orderbook_latest
                set fetched_at_utc = '2999-01-01T00:00:00+00:00',
                    best_bid = 0.99
                where outcome_id = 'yes'
                """
            )
            db.commit()

            store_orderbook(
                db,
                "yes",
                OrderBook(
                    "yes",
                    bids=[(0.10, 5)],
                    asks=[(0.20, 5)],
                    tick_size=0.01,
                    min_order_size=5,
                ),
            )

            latest_row = db.execute(
                "select fetched_at_utc, best_bid from orderbook_latest where outcome_id = 'yes'"
            ).fetchone()
            self.assertEqual(latest_row["fetched_at_utc"], "2999-01-01T00:00:00+00:00")
            self.assertEqual(latest_row["best_bid"], 0.99)

    def test_latest_orderbook_falls_back_to_archive_when_hot_row_missing(self):
        from whenitrains.storage import latest_orderbook

        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            store_orderbook(
                db,
                "yes",
                OrderBook(
                    "yes",
                    bids=[(0.25, 5)],
                    asks=[(0.45, 5)],
                    tick_size=0.01,
                    min_order_size=5,
                ),
            )
            db.execute("delete from orderbook_latest where outcome_id = 'yes'")
            db.commit()

            book = latest_orderbook(db, "yes")

            self.assertEqual(book.best_bid, 0.25)
            self.assertEqual(book.best_ask, 0.45)

    def test_list_outcomes_from_date_excludes_past_markets(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            for target_date, token in [
                (date(2026, 5, 4), "past"),
                (date(2026, 5, 5), "today"),
                (date(2026, 5, 6), "future"),
            ]:
                store_polymarket_event(
                    db,
                    TemperatureMarket(
                        event_id=f"event-{token}",
                        event_slug=f"slug-{token}",
                        title="Highest temperature",
                        target_date=target_date,
                        outcomes=[
                            Outcome(
                                market_id=f"m-{token}",
                                label="25°C",
                                predicate=parse_outcome_label("25°C"),
                                yes_token_id=f"yes-{token}",
                                no_token_id=f"no-{token}",
                            )
                        ],
                    ),
                )

            rows = list_outcomes_from_date(db, "2026-05-05")

            self.assertEqual([row["yes_token_id"] for row in rows], ["yes-today", "yes-future"])

    def test_mark_clob_tokens_untradeable_removes_tokens_from_active_subscription_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            store_polymarket_event(
                db,
                TemperatureMarket(
                    event_id="event",
                    event_slug="highest-temperature-in-hong-kong-on-may-5-2026",
                    title="Highest temperature",
                    target_date=date(2026, 5, 5),
                    outcomes=[
                        Outcome(
                            market_id="m25",
                            label="25°C",
                            predicate=parse_outcome_label("25°C"),
                            yes_token_id="yes25",
                            no_token_id="no25",
                        ),
                        Outcome(
                            market_id="m26",
                            label="26°C",
                            predicate=parse_outcome_label("26°C"),
                            yes_token_id="yes26",
                            no_token_id="no26",
                        ),
                    ],
                ),
            )

            mark_clob_tokens_untradeable(db, ["yes25", "no25"], reason="no orderbook")

            self.assertFalse(is_clob_tradeable_token(db, "yes25"))
            self.assertFalse(is_clob_tradeable_token(db, "no25"))
            self.assertEqual(
                list_active_market_token_ids(db, "2026-05-05"),
                ["yes26", "no26"],
            )

    def test_list_tradeable_forecast_dates_allows_min_only_forecast(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            target = date(2026, 5, 5)
            store_polymarket_event(
                db,
                TemperatureMarket(
                    event_id="event-low",
                    event_slug="lowest-temperature-in-hong-kong-on-may-5-2026",
                    title="Lowest temperature",
                    target_date=target,
                    outcomes=[
                        Outcome(
                            market_id="m-low",
                            label="25°C",
                            predicate=parse_outcome_label("25°C"),
                            yes_token_id="yes-low",
                            no_token_id="no-low",
                        )
                    ],
                ),
            )
            snapshot = store_raw_snapshot(db, "hko", "forecast-min-only", "{}")
            store_hko_forecasts(
                db,
                snapshot.id,
                [
                    HkoForecast(
                        source_type="ocf_station",
                        forecast_date_hkt=target,
                        forecast_min_c=25,
                        forecast_max_c=None,
                    )
                ],
            )

            self.assertEqual(list_tradeable_forecast_dates(db, "2026-05-05"), ["2026-05-05"])


if __name__ == "__main__":
    unittest.main()
