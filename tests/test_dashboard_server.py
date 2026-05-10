import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from whenitrains.dashboard_server import (
    HISTORICALS_HTML,
    INDEX_HTML,
    LIVE_HTML,
    bucketed_orderbook_ask_points,
    dashboard_stats,
    forecast_series,
    forecast_panels,
    hourly_actual_series,
    hourly_error_series,
    hourly_forecast_series,
    paper_trade_rows,
    paper_order_markers,
    pnl_series,
    top_token_price_series,
    top_yes_price_series,
    latest_decimal_forecast_stats,
    latest_market_token_price_rows,
    historicals_payload,
    live_dashboard_payload,
    live_pnl_series,
    reconcile_live_dashboard_orders,
    live_trade_rows,
)
from whenitrains.hko import (
    HKT,
    HkoCurrentTemperature,
    HkoForecast,
    HkoObservation,
    OcfForecastSample,
)
from whenitrains.markets import parse_outcome_label
from whenitrains.polymarket import OrderBook, Outcome, TemperatureMarket
from whenitrains.storage import (
    connect,
    migrate,
    store_hko_forecasts,
    store_hko_current_temperature,
    store_hko_observation,
    store_ocf_forecast_samples,
    store_orderbook,
    store_paper_order_result,
    store_polymarket_event,
    store_raw_snapshot,
    upsert_live_position,
)


class DashboardServerTests(unittest.TestCase):
    def test_historicals_payload_measures_ocf_accuracy_against_final_max_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            _seed_historical_accuracy_day(db)

            payload = historicals_payload(db)
            max_payload = payload["series"]["max"]

            self.assertEqual(max_payload["summary"]["forecast_sample_count"], 2)
            self.assertEqual(max_payload["summary"]["actual_day_count"], 1)
            self.assertAlmostEqual(max_payload["summary"]["mean_error_c"], 0.0)
            self.assertAlmostEqual(max_payload["summary"]["mae_c"], 0.5)
            self.assertEqual(
                [round(point["hours_before"], 1) for point in max_payload["accuracy_points"]],
                [3.0, 1.0],
            )
            self.assertEqual(
                [point["forecast_c"] for point in max_payload["accuracy_points"]],
                [27.0, 28.0],
            )
            self.assertEqual(
                [point["actual_c"] for point in max_payload["accuracy_points"]],
                [27.5, 27.5],
            )
            self.assertEqual(
                [point["error_c"] for point in max_payload["accuracy_points"]],
                [-0.5, 0.5],
            )

    def test_historicals_payload_measures_ocf_accuracy_against_final_min_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            _seed_historical_accuracy_day(db)

            payload = historicals_payload(db)
            min_payload = payload["series"]["min"]

            self.assertEqual(min_payload["summary"]["forecast_sample_count"], 2)
            self.assertEqual(
                [round(point["hours_before"], 1) for point in min_payload["accuracy_points"]],
                [4.0, 2.0],
            )
            self.assertEqual(
                [point["forecast_c"] for point in min_payload["accuracy_points"]],
                [24.0, 23.0],
            )
            self.assertEqual(
                [point["actual_c"] for point in min_payload["accuracy_points"]],
                [23.5, 23.5],
            )
            self.assertEqual(
                [point["error_c"] for point in min_payload["accuracy_points"]],
                [0.5, -0.5],
            )

    def test_historicals_payload_matches_forecast_bucket_token_price_by_lead_hours(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            _seed_historical_accuracy_day(db)
            _set_latest_orderbook_time(db, "yes27", "2026-05-06T01:55:00+00:00", 0.34)
            _set_latest_orderbook_time(db, "yes28", "2026-05-06T03:55:00+00:00", 0.47)
            _set_latest_orderbook_time(db, "lowyes24", "2026-05-06T01:55:00+00:00", 0.31)
            _set_latest_orderbook_time(db, "lowyes23", "2026-05-06T03:55:00+00:00", 0.46)

            payload = historicals_payload(db)

            prices = payload["series"]["max"]["forecast_price_points"]
            self.assertEqual(
                [(round(p["hours_before"], 1), p["label"], p["price"]) for p in prices],
                [(3.0, "27°C", 0.34), (1.0, "28°C", 0.47)],
            )
            low_prices = payload["series"]["min"]["forecast_price_points"]
            self.assertEqual(
                [(round(p["hours_before"], 1), p["label"], p["price"]) for p in low_prices],
                [(4.0, "24°C", 0.31), (2.0, "23°C", 0.46)],
            )

    def test_historicals_payload_groups_pnl_histograms_by_reason_and_lead_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            _insert_historical_order(
                db,
                created_at="2026-05-05T03:00:00+00:00",
                token_id="yes25",
                side="BUY_YES",
                fill_price=0.40,
                fill_size=40,
                reason="price has not moved with HKO event",
                decision_reason="forecast change",
            )
            _insert_historical_order(
                db,
                created_at="2026-05-06T04:00:00+00:00",
                token_id="yes25",
                side="SELL",
                fill_price=0.52,
                fill_size=52,
                reason="exit",
            )
            _insert_historical_order(
                db,
                created_at="2026-05-06T02:00:00+00:00",
                token_id="no26",
                side="BUY_NO",
                fill_price=0.50,
                fill_size=50,
                reason="forecast bucket ask below threshold",
                decision_reason="cheap forecast bucket",
            )
            _insert_historical_order(
                db,
                created_at="2026-05-06T06:00:00+00:00",
                token_id="no26",
                side="SELL",
                fill_price=0.25,
                fill_size=25,
                reason="exit",
            )

            payload = historicals_payload(db)

            groups = {
                (group["reason"], group["lead_day"]): group
                for group in payload["pnl_histograms"]
            }
            self.assertAlmostEqual(
                groups[("forecast change", "D+1")]["trades"][0]["pct_gain"], 30.0
            )
            self.assertAlmostEqual(
                groups[("cheap forecast bucket", "D+0")]["trades"][0]["pct_gain"],
                -50.0,
            )
            self.assertEqual(groups[("forecast change", "D+1")]["count"], 1)
            self.assertEqual(groups[("cheap forecast bucket", "D+0")]["count"], 1)

    def test_top_yes_price_series_returns_current_top_three_for_target_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            for token, ask in [
                ("yes24", 0.12),
                ("yes25", 0.42),
                ("yes26", 0.33),
                ("yes27", 0.21),
            ]:
                store_orderbook(
                    db,
                    token,
                    OrderBook(
                        token,
                        bids=[(ask - 0.02, 10)],
                        asks=[(ask, 10)],
                        tick_size=0.01,
                        min_order_size=5,
                    ),
                )

            series = top_yes_price_series(db, "2026-05-06")

            self.assertEqual([item["label"] for item in series], ["25°C", "26°C", "27°C"])
            self.assertEqual(series[0]["latest_yes"], 0.42)
            self.assertEqual(series[0]["points"][0]["value"], 0.42)

    def test_top_token_price_series_can_return_no_side_with_trade_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            store_orderbook(
                db,
                "no25",
                OrderBook(
                    "no25",
                    bids=[(0.58, 10)],
                    asks=[(0.60, 10)],
                    tick_size=0.01,
                    min_order_size=5,
                ),
            )
            store_paper_order_result(
                db,
                "no25",
                "BUY_NO",
                limit_price=0.60,
                size_usd=25,
                fill_price=0.60,
                fill_size_usd=25,
                status="filled",
                reason="test buy",
            )

            series = top_token_price_series(db, "2026-05-06", "NO")

            self.assertEqual(series[0]["label"], "25°C")
            self.assertEqual(series[0]["side"], "NO")
            self.assertEqual(series[0]["latest_price"], 0.60)
            self.assertEqual(series[0]["markers"][0]["text"], "B")
            self.assertEqual(series[0]["markers"][0]["price"], 0.60)

    def test_forecast_panels_split_d0_d1_d2(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            snapshot = store_raw_snapshot(db, "hko", "forecast", "{}")
            for target, high in [
                (date(2026, 5, 5), 24),
                (date(2026, 5, 6), 25),
                (date(2026, 5, 7), 26),
            ]:
                store_hko_forecasts(
                    db,
                    snapshot.id,
                    [
                        HkoForecast(
                            source_type="ocf_station",
                            forecast_date_hkt=target,
                            forecast_min_c=None,
                            forecast_max_c=high,
                            update_time="2026-05-05T12:00:00+08:00",
                            raw={"ForecastMaximumTemperature": high + 0.4},
                        )
                    ],
                )
            store_hko_observation(
                db,
                snapshot.id,
                HkoObservation(
                    observed_at_hkt=datetime(2026, 5, 5, 12, 0, tzinfo=HKT),
                    station="HK Observatory",
                    since_midnight_max_c=23.2,
                    since_midnight_min_c=21.0,
                    raw={},
                ),
            )

            payload = forecast_panels(db, today=date(2026, 5, 5))

            self.assertEqual([panel["lead_days"] for panel in payload["panels"]], [0, 1, 2])
            self.assertEqual([panel["target_date"] for panel in payload["panels"]], ["2026-05-05", "2026-05-06", "2026-05-07"])
            self.assertEqual(payload["panels"][0]["forecast"][0]["value"], 24.4)
            self.assertEqual(payload["panels"][1]["forecast"][0]["value"], 25.0)
            self.assertTrue(payload["panels"][0]["actual_max"])
            self.assertEqual(payload["panels"][1]["actual_max"], [])
            self.assertEqual(payload["token_side"], "YES")

            no_payload = forecast_panels(db, today=date(2026, 5, 5), token_side="NO")
            self.assertEqual(no_payload["token_side"], "NO")

    def test_d0_panel_includes_hourly_forecast_actual_and_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            snapshot = store_raw_snapshot(db, "hko", "ocf", "{}")
            store_ocf_forecast_samples(
                db,
                snapshot.id,
                [
                    OcfForecastSample(
                        forecast_date_hkt=date(2026, 5, 6),
                        forecast_min_c=23,
                        forecast_max_c=25,
                        raw_min_c=23.0,
                        raw_max_c=25.0,
                        hourly_temperatures=[
                            {
                                "forecast_hour_hkt": "2026-05-06T13:00:00+08:00",
                                "temperature_c": 24,
                            },
                            {
                                "forecast_hour_hkt": "2026-05-06T14:00:00+08:00",
                                "temperature_c": 25,
                            },
                        ],
                        raw={"LastModified": 20260506121145},
                    )
                ],
            )
            actual_snapshot = store_raw_snapshot(db, "hko", "rhrread", "{}")
            store_hko_current_temperature(
                db,
                actual_snapshot.id,
                HkoCurrentTemperature(
                    observed_at_hkt=datetime(2026, 5, 6, 13, 40, tzinfo=HKT),
                    station="Hong Kong Observatory",
                    temperature_c=24.6,
                    raw={},
                ),
            )

            panel = forecast_panels(db, today=date(2026, 5, 6))["panels"][0]

            self.assertEqual(panel["hourly_forecast"][0]["value"], 24.0)
            self.assertEqual(panel["hourly_actual"][0]["value"], 24.6)
            self.assertAlmostEqual(panel["hourly_error"][0]["value"], 0.6)
            self.assertEqual(hourly_forecast_series(db, "2026-05-06")[1]["value"], 25.0)
            actual = hourly_actual_series(db, "2026-05-06")
            self.assertEqual(actual[0]["value"], 24.6)
            self.assertAlmostEqual(
                hourly_error_series(panel["hourly_forecast"], actual)[0]["value"],
                0.6,
            )

    def test_forecast_series_uses_effective_ocf_sample_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            target = date(2026, 5, 6)
            snapshot = store_raw_snapshot(db, "hko", "ocf", "{}")
            store_ocf_forecast_samples(
                db,
                snapshot.id,
                [
                    OcfForecastSample(
                        forecast_date_hkt=target,
                        forecast_min_c=23,
                        forecast_max_c=28,
                        raw_min_c=23.0,
                        raw_max_c=28.0,
                        hourly_temperatures=[
                            {
                                "forecast_hour_hkt": "2026-05-06T06:00:00+08:00",
                                "temperature_c": 23.2,
                            },
                            {
                                "forecast_hour_hkt": "2026-05-06T14:00:00+08:00",
                                "temperature_c": 27.7,
                            },
                        ],
                        raw={"LastModified": "2026-05-06T09:14:00+08:00"},
                    )
                ],
            )

            high = forecast_series(db, target.isoformat(), value_kind="max")
            low = forecast_series(db, target.isoformat(), value_kind="min")

            self.assertEqual(high[0]["value"], 27.7)
            self.assertEqual(low[0]["value"], 23.2)

    def test_forecast_series_dedupes_ocf_samples_by_update_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            target = date(2026, 5, 6)
            snapshot = store_raw_snapshot(db, "hko", "ocf", "{}")
            for high in [27.2, 27.8]:
                store_ocf_forecast_samples(
                    db,
                    snapshot.id,
                    [
                        OcfForecastSample(
                            forecast_date_hkt=target,
                            forecast_min_c=23,
                            forecast_max_c=28,
                            raw_min_c=23.0,
                            raw_max_c=28.0,
                            hourly_temperatures=[
                                {
                                    "forecast_hour_hkt": "2026-05-06T14:00:00+08:00",
                                    "temperature_c": high,
                                }
                            ],
                            raw={"LastModified": "2026-05-06T09:14:00+08:00"},
                        )
                    ],
                )

            high = forecast_series(db, target.isoformat(), value_kind="max")

            self.assertEqual(len(high), 1)
            self.assertEqual(high[0]["value"], 27.8)

    def test_forecast_series_parses_compact_hko_update_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            target = date(2026, 5, 6)
            snapshot = store_raw_snapshot(db, "hko", "ocf", "{}")
            store_ocf_forecast_samples(
                db,
                snapshot.id,
                [
                    OcfForecastSample(
                        forecast_date_hkt=target,
                        forecast_min_c=24,
                        forecast_max_c=28,
                        raw_min_c=24.0,
                        raw_max_c=28.0,
                        hourly_temperatures=[
                            {
                                "forecast_hour_hkt": "2026-05-06T13:00:00+08:00",
                                "temperature_c": 27.7,
                            }
                        ],
                        raw={"LastModified": 20260506091143},
                    )
                ],
            )

            high = forecast_series(db, target.isoformat(), value_kind="max")
            stats = latest_decimal_forecast_stats(db, target.isoformat())

            self.assertEqual(high[0]["value"], 27.7)
            self.assertEqual(
                datetime.fromtimestamp(high[0]["time"], tz=HKT).strftime("%Y-%m-%d %H:%M:%S"),
                "2026-05-06 09:11:43",
            )
            self.assertEqual(stats["update_time"], "20260506091143")

    def test_dashboard_stats_use_decimal_forecast_high_and_low(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            target = date(2026, 5, 6)
            snapshot = store_raw_snapshot(db, "hko", "ocf", "{}")
            store_ocf_forecast_samples(
                db,
                snapshot.id,
                [
                    OcfForecastSample(
                        forecast_date_hkt=target,
                        forecast_min_c=23,
                        forecast_max_c=26,
                        raw_min_c=22.6,
                        raw_max_c=25.6,
                        hourly_temperatures=[
                            {
                                "forecast_hour_hkt": "2026-05-06T06:00:00+08:00",
                                "temperature_c": 22.4,
                            },
                            {
                                "forecast_hour_hkt": "2026-05-06T14:00:00+08:00",
                                "temperature_c": 25.7,
                            },
                        ],
                        raw={"LastModified": "2026-05-06T12:11:45+08:00"},
                    )
                ],
            )

            forecast = latest_decimal_forecast_stats(db, target.isoformat())

            self.assertEqual(forecast["forecast_min_c"], 22.4)
            self.assertEqual(forecast["forecast_max_c"], 25.7)
            self.assertEqual(forecast["display_forecast_min_c"], 23)
            self.assertEqual(forecast["display_forecast_max_c"], 26)

    def test_dashboard_stats_include_since_midnight_min(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            snapshot = store_raw_snapshot(db, "hko", "obs", "{}")
            store_hko_observation(
                db,
                snapshot.id,
                HkoObservation(
                    observed_at_hkt=datetime(2026, 5, 6, 13, 50, tzinfo=HKT),
                    station="HK Observatory",
                    since_midnight_min_c=21.6,
                    since_midnight_max_c=24.6,
                    raw={},
                ),
            )

            stats = dashboard_stats(db)

            self.assertEqual(stats["latest_observation"]["since_midnight_min_c"], 21.6)
            self.assertEqual(stats["latest_observation"]["since_midnight_max_c"], 24.6)

    def test_dashboard_stats_include_latest_current_temperature_with_newer_since_midnight_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            current_snapshot = store_raw_snapshot(db, "hko", "aws", "{}")
            store_hko_current_temperature(
                db,
                current_snapshot.id,
                HkoCurrentTemperature(
                    observed_at_hkt=datetime(2026, 5, 6, 13, 40, tzinfo=HKT),
                    station="HKO",
                    temperature_c=24.6,
                    since_midnight_min_c=21.6,
                    since_midnight_max_c=24.6,
                    raw={},
                ),
            )
            since_midnight_snapshot = store_raw_snapshot(db, "hko", "obs", "{}")
            store_hko_observation(
                db,
                since_midnight_snapshot.id,
                HkoObservation(
                    observed_at_hkt=datetime(2026, 5, 6, 13, 50, tzinfo=HKT),
                    station="HK Observatory",
                    since_midnight_min_c=21.6,
                    since_midnight_max_c=24.6,
                    raw={},
                ),
            )

            stats = dashboard_stats(db)

            self.assertEqual(stats["latest_observation"]["observed_at_hkt"], "2026-05-06T13:50:00+08:00")
            self.assertEqual(stats["latest_observation"]["temperature_c"], 24.6)
            self.assertEqual(stats["latest_observation"]["temperature_observed_at_hkt"], "2026-05-06T13:40:00+08:00")
            self.assertEqual(stats["latest_observation"]["temperature_station"], "HKO")

    def test_hourly_actual_ignores_since_midnight_max_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            snapshot = store_raw_snapshot(db, "hko", "obs", "{}")
            store_hko_observation(
                db,
                snapshot.id,
                HkoObservation(
                    observed_at_hkt=datetime(2026, 5, 6, 13, 50, tzinfo=HKT),
                    station="HK Observatory",
                    since_midnight_max_c=24.6,
                    since_midnight_min_c=21.6,
                    raw={},
                ),
            )

            actual = hourly_actual_series(db, "2026-05-06")

            self.assertEqual(actual, [])

    def test_forecast_panels_limit_tradeable_tokens_but_force_include_trades(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            snapshot = store_raw_snapshot(db, "hko", "forecast", "{}")
            store_hko_forecasts(
                db,
                snapshot.id,
                [
                    HkoForecast(
                        source_type="ocf_station",
                        forecast_date_hkt=date(2026, 5, 6),
                        forecast_min_c=None,
                        forecast_max_c=25,
                        update_time="2026-05-05T12:00:00+08:00",
                        raw={},
                    )
                ],
            )
            for token, ask in [
                ("yes24", 0.02),
                ("yes25", 0.20),
                ("yes26", 0.40),
                ("yes27", 0.60),
                ("yes28", 0.80),
                ("yes29", 0.995),
                ("yes30", 0.005),
            ]:
                store_orderbook(
                    db,
                    token,
                    OrderBook(
                        token,
                        bids=[(max(0.0, ask - 0.01), 10)],
                        asks=[(ask, 10)],
                        tick_size=0.01,
                        min_order_size=5,
                    ),
                )
            store_paper_order_result(
                db,
                "yes24",
                "BUY_YES",
                limit_price=0.02,
                size_usd=10,
                fill_price=0.02,
                fill_size_usd=10,
                status="filled",
                reason="test buy",
            )

            panel = forecast_panels(db, today=date(2026, 5, 6))["panels"][0]

            self.assertLessEqual(len(panel["top_tokens"]), 5)
            self.assertIn("24°C", [item["label"] for item in panel["top_tokens"]])
            self.assertNotIn("29°C", [item["label"] for item in panel["top_tokens"]])
            self.assertNotIn("30°C", [item["label"] for item in panel["top_tokens"]])

    def test_latest_market_token_price_rows_uses_token_scoped_latest_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            for token, ask in [("yes24", 0.20), ("yes25", 0.30), ("other", 0.99)]:
                store_orderbook(
                    db,
                    token,
                    OrderBook(
                        token,
                        bids=[(ask - 0.01, 10)],
                        asks=[(ask, 10)],
                        tick_size=0.01,
                        min_order_size=5,
                    ),
                )

            rows = latest_market_token_price_rows(
                db, "2026-05-06", "YES", "highest", sort_by_latest_price=True, limit=2
            )

            self.assertEqual([row["token_id"] for row in rows], ["yes25", "yes24"])
            plan = "\n".join(
                row[3]
                for row in db.execute(
                    """
                    explain query plan
                    select best_ask
                    from orderbook_snapshots
                    where outcome_id = ?
                      and best_ask is not null
                    order by fetched_at_utc desc, id desc
                    limit 1
                    """,
                    ("yes25",),
                ).fetchall()
            )
            self.assertIn("idx_orderbook_snapshots_latest", plan)

    def test_bucketed_orderbook_ask_points_collapses_raw_snapshots_in_sql(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            for ask, fetched_at in [
                (0.20, "2026-05-06T01:00:10+00:00"),
                (0.30, "2026-05-06T01:00:50+00:00"),
                (0.40, "2026-05-06T01:01:05+00:00"),
            ]:
                store_orderbook(
                    db,
                    "yes25",
                    OrderBook(
                        "yes25",
                        bids=[(ask - 0.01, 10)],
                        asks=[(ask, 10)],
                        tick_size=0.01,
                        min_order_size=5,
                    ),
                )
                db.execute(
                    """
                    update orderbook_snapshots
                    set fetched_at_utc = ?
                    where id = (select max(id) from orderbook_snapshots)
                    """,
                    (fetched_at,),
                )
            db.commit()

            points = bucketed_orderbook_ask_points(db, "yes25", bucket_seconds=60)

            self.assertEqual([point["value"] for point in points], [0.30, 0.40])
            self.assertEqual(len(points), 2)

    def test_paper_order_markers_include_decision_signal_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            store_paper_order_result(
                db,
                "no27",
                "BUY_NO",
                limit_price=0.60,
                size_usd=100,
                fill_price=0.55,
                fill_size_usd=100,
                status="filled",
                reason="price has not moved with HKO event",
            )
            order = db.execute(
                "select created_at_utc from paper_orders where outcome_id = 'no27'"
            ).fetchone()
            db.execute(
                """
                insert into paper_decisions (
                    created_at_utc, event_type, outcome_id, label, side, action,
                    status, reason, details_json, event_key
                )
                values (?, 'forecast_change', 'no27', '27°C', 'NO', 'BUY',
                        'filled', 'price has not moved with HKO event', '{}',
                        'forecast_change:2026-05-06:20260506083143:28.0->20260506091143:27.7')
                """,
                (order["created_at_utc"],),
            )
            db.commit()

            marker = paper_order_markers(db, "no27")[0]

            self.assertEqual(marker["signal"], "signal high 27.7°C")
            self.assertEqual(marker["signal_time_hkt"], "2026-05-06 09:11:43")
            self.assertEqual(
                marker["decision_reason"], "price has not moved with HKO event"
            )

    def test_paper_trade_rows_returns_open_position_buy_sell_activity(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            store_paper_order_result(
                db,
                "yes25",
                "BUY_YES",
                limit_price=0.30,
                size_usd=100,
                fill_price=0.30,
                fill_size_usd=100,
                status="filled",
                reason="forecast value",
            )
            store_orderbook(
                db,
                "yes25",
                OrderBook(
                    "yes25",
                    bids=[(0.42, 10)],
                    asks=[(0.44, 10)],
                    tick_size=0.01,
                    min_order_size=5,
                ),
            )
            db.execute(
                """
                insert into paper_positions
                (outcome_id, net_shares, avg_price, realized_pnl, updated_at_utc)
                values ('yes25', 333.3333, 0.30, 0, '2026-05-06T10:00:00+00:00')
                """
            )
            db.commit()

            payload = paper_trade_rows(db, "open")

            self.assertEqual(payload["title"], "Open Position Trades")
            self.assertEqual(len(payload["rows"]), 1)
            row = payload["rows"][0]
            self.assertEqual(row["label"], "25°C")
            self.assertEqual(row["token_side"], "YES")
            self.assertEqual(row["action"], "BUY_YES")
            self.assertRegex(row["created_at_hkt"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
            self.assertAlmostEqual(row["latest_bid"], 0.42)
            self.assertAlmostEqual(row["unrealized_pnl"], 40)

    def test_paper_trade_rows_unrealized_pnl_is_per_buy_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            store_paper_order_result(
                db,
                "yes25",
                "BUY_YES",
                limit_price=0.25,
                size_usd=50,
                fill_price=0.25,
                fill_size_usd=50,
                status="filled",
                reason="first entry",
            )
            store_paper_order_result(
                db,
                "yes25",
                "BUY_YES",
                limit_price=0.40,
                size_usd=80,
                fill_price=0.40,
                fill_size_usd=80,
                status="filled",
                reason="second entry",
            )
            store_orderbook(
                db,
                "yes25",
                OrderBook(
                    "yes25",
                    bids=[(0.50, 10)],
                    asks=[(0.52, 10)],
                    tick_size=0.01,
                    min_order_size=5,
                ),
            )
            db.execute(
                """
                insert into paper_positions
                (outcome_id, net_shares, avg_price, realized_pnl, updated_at_utc)
                values ('yes25', 400, 0.325, 0, '2026-05-06T10:00:00+00:00')
                """
            )
            db.commit()

            payload = paper_trade_rows(db, "unrealized")

            self.assertEqual([row["reason"] for row in payload["rows"]], ["second entry", "first entry"])
            self.assertAlmostEqual(payload["rows"][0]["unrealized_pnl"], 20)
            self.assertAlmostEqual(payload["rows"][1]["unrealized_pnl"], 50)

    def test_paper_trade_rows_do_not_use_stale_bid_after_latest_book_has_no_bid(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            store_paper_order_result(
                db,
                "yes25",
                "BUY_YES",
                limit_price=0.25,
                size_usd=50,
                fill_price=0.25,
                fill_size_usd=50,
                status="filled",
                reason="entry",
            )
            store_orderbook(
                db,
                "yes25",
                OrderBook(
                    "yes25",
                    bids=[(0.50, 10)],
                    asks=[(0.52, 10)],
                    tick_size=0.01,
                    min_order_size=5,
                ),
            )
            store_orderbook(
                db,
                "yes25",
                OrderBook(
                    "yes25",
                    bids=[],
                    asks=[(0.99, 10)],
                    tick_size=0.01,
                    min_order_size=5,
                ),
            )
            db.execute(
                """
                insert into paper_positions
                (outcome_id, net_shares, avg_price, realized_pnl, updated_at_utc)
                values ('yes25', 200, 0.25, 0, '2026-05-06T10:00:00+00:00')
                """
            )
            db.commit()

            payload = paper_trade_rows(db, "unrealized")
            stats = dashboard_stats(db)

            self.assertIsNone(payload["rows"][0]["latest_bid"])
            self.assertEqual(payload["rows"][0]["unrealized_pnl"], 0)
            self.assertAlmostEqual(stats["executable_unrealized_pnl"], -50)

    def test_paper_trade_rows_unrealized_pnl_ignores_closed_float_dust(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            store_paper_order_result(
                db,
                "yes25",
                "BUY_YES",
                limit_price=0.25,
                size_usd=50,
                fill_price=0.25,
                fill_size_usd=50,
                status="filled",
                reason="entry",
            )
            store_paper_order_result(
                db,
                "yes25",
                "SELL",
                limit_price=0.10,
                size_usd=19.999999999999996,
                fill_price=0.10,
                fill_size_usd=19.999999999999996,
                status="filled",
                reason="exit",
            )
            store_orderbook(
                db,
                "yes25",
                OrderBook(
                    "yes25",
                    bids=[(0.90, 10)],
                    asks=[(0.92, 10)],
                    tick_size=0.01,
                    min_order_size=5,
                ),
            )
            db.execute(
                """
                insert into paper_positions
                (outcome_id, net_shares, avg_price, realized_pnl, updated_at_utc)
                values ('yes25', 0, 0, 0, '2026-05-06T10:00:00+00:00')
                """
            )
            db.commit()

            payload = paper_trade_rows(db, "open")

            self.assertEqual(payload["rows"][0]["unrealized_pnl"], 0)

    def test_paper_trade_rows_returns_realized_sell_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            store_paper_order_result(
                db,
                "no25",
                "BUY_NO",
                limit_price=0.40,
                size_usd=50,
                fill_price=0.40,
                fill_size_usd=50,
                status="filled",
                reason="entry",
            )
            store_paper_order_result(
                db,
                "no25",
                "SELL",
                limit_price=0.60,
                size_usd=75,
                fill_price=0.60,
                fill_size_usd=75,
                status="filled",
                reason="exit",
            )
            db.execute(
                """
                insert into paper_positions
                (outcome_id, net_shares, avg_price, realized_pnl, updated_at_utc)
                values ('no25', 0, 0, 25, '2026-05-06T11:00:00+00:00')
                """
            )
            db.commit()

            payload = paper_trade_rows(db, "realized")

            self.assertEqual(payload["title"], "Realized PnL Trades")
            self.assertEqual([row["action"] for row in payload["rows"]], ["SELL"])
            self.assertEqual(payload["rows"][0]["token_side"], "NO")
            self.assertAlmostEqual(payload["rows"][0]["realized_pnl"], 25)

    def test_paper_trade_rows_realized_pnl_is_per_sell_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            store_paper_order_result(
                db,
                "no25",
                "BUY_NO",
                limit_price=0.40,
                size_usd=80,
                fill_price=0.40,
                fill_size_usd=80,
                status="filled",
                reason="entry",
            )
            store_paper_order_result(
                db,
                "no25",
                "SELL",
                limit_price=0.50,
                size_usd=50,
                fill_price=0.50,
                fill_size_usd=50,
                status="filled",
                reason="first exit",
            )
            store_paper_order_result(
                db,
                "no25",
                "SELL",
                limit_price=0.60,
                size_usd=60,
                fill_price=0.60,
                fill_size_usd=60,
                status="filled",
                reason="second exit",
            )
            db.execute(
                """
                insert into paper_positions
                (outcome_id, net_shares, avg_price, realized_pnl, updated_at_utc)
                values ('no25', 0, 0, 30, '2026-05-06T11:00:00+00:00')
                """
            )
            db.commit()

            payload = paper_trade_rows(db, "realized")

            self.assertEqual([row["action"] for row in payload["rows"]], ["SELL", "SELL"])
            self.assertAlmostEqual(payload["rows"][0]["realized_pnl"], 20)
            self.assertAlmostEqual(payload["rows"][1]["realized_pnl"], 10)

    def test_dashboard_filters_excluded_paper_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            store_paper_order_result(
                db,
                "yes25",
                "BUY_YES",
                limit_price=0.30,
                size_usd=100,
                fill_price=0.30,
                fill_size_usd=100,
                status="filled",
                reason="valid entry",
            )
            valid_id = db.execute(
                "select max(id) from paper_orders where outcome_id = 'yes25'"
            ).fetchone()[0]
            store_paper_order_result(
                db,
                "yes25",
                "BUY_YES",
                limit_price=0.70,
                size_usd=100,
                fill_price=0.70,
                fill_size_usd=100,
                status="filled",
                reason="bug entry",
            )
            excluded_id = db.execute(
                "select max(id) from paper_orders where outcome_id = 'yes25'"
            ).fetchone()[0]
            db.execute(
                """
                insert into paper_order_exclusions (order_id, tag, reason, created_at_utc)
                values (?, 'bug_order', 'test exclusion', '2026-05-06T00:00:00+00:00')
                """,
                (excluded_id,),
            )
            db.commit()
            store_orderbook(
                db,
                "yes25",
                OrderBook(
                    "yes25",
                    bids=[(0.40, 1000)],
                    asks=[(0.42, 1000)],
                    tick_size=0.01,
                    min_order_size=5,
                ),
            )

            stats = dashboard_stats(db)
            trades = paper_trade_rows(db, "open")
            pnl = pnl_series(db)

            self.assertEqual(stats["counts"]["buy_filled"], 1)
            self.assertEqual(stats["open_positions"], 1)
            self.assertAlmostEqual(stats["worst_case_open_loss"], 100)
            self.assertEqual([row["id"] for row in trades["rows"]], [valid_id])
            self.assertAlmostEqual(pnl["unrealized"][-1]["value"], 100 / 0.30 * 0.10)

    def test_live_dashboard_uses_live_orders_and_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            store_orderbook(
                db,
                "yes25",
                OrderBook(
                    "yes25",
                    bids=[(0.45, 10)],
                    asks=[(0.47, 10)],
                    tick_size=0.01,
                    min_order_size=5,
                ),
            )
            db.execute(
                """
                insert into live_orders (
                    created_at_utc, outcome_id, label, side, action, clob_order_id,
                    order_type, status, requested_size_usd, limit_price, fill_price,
                    fill_size_usd, fill_shares, reason
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-05-06T04:00:00+00:00",
                    "yes25",
                    "25°C",
                    "BUY_YES",
                    "BUY",
                    "0xorder",
                    "FAK",
                    "filled",
                    5.0,
                    0.47,
                    0.47,
                    5.0,
                    10.6383,
                    "test live buy",
                ),
            )
            db.commit()
            upsert_live_position(db, "yes25", 10.6383, 0.47, 0.0)

            stats = live_dashboard_payload(db)
            pnl = live_pnl_series(db)
            trades = live_trade_rows(db, "open")
            panel = forecast_panels(db, today=date(2026, 5, 6), marker_source="live")[
                "panels"
            ][0]

            self.assertEqual(stats["mode"], "live")
            self.assertEqual(stats["open_positions"], 1)
            self.assertEqual(stats["counts"]["buy_filled"], 1)
            self.assertEqual(trades["rows"][0]["action"], "BUY_YES")
            self.assertTrue(pnl["realized"])
            self.assertEqual(panel["top_tokens"][0]["markers"][0]["text"], "B")

    def test_live_dashboard_reconcile_makes_submitted_fill_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            store_orderbook(
                db,
                "yes25",
                OrderBook(
                    "yes25",
                    bids=[(0.31, 100)],
                    asks=[(0.33, 100)],
                    tick_size=0.01,
                    min_order_size=5,
                ),
            )
            db.execute(
                """
                insert into live_orders (
                    created_at_utc, outcome_id, label, side, action, clob_order_id,
                    order_type, status, requested_size_usd, limit_price, reason
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-05-06T04:00:00+00:00",
                    "yes25",
                    "25°C",
                    "BUY_YES",
                    "BUY",
                    "0xorder",
                    "FAK",
                    "submitted",
                    20.0,
                    0.30,
                    "test submitted live buy",
                ),
            )
            db.commit()

            summary = reconcile_live_dashboard_orders(
                db, client_factory=lambda: _FakeLiveReconcileClient()
            )

            rows = live_trade_rows(db, "open")["rows"]
            self.assertEqual(summary["attempted"], 1)
            self.assertEqual(summary["filled"], 1)
            self.assertEqual(rows[0]["id"], 1)
            self.assertEqual(rows[0]["action"], "BUY_YES")
            self.assertAlmostEqual(rows[0]["fill_price"], 0.29)
            self.assertAlmostEqual(rows[0]["shares"], 68.503)
            self.assertAlmostEqual(rows[0]["fill_size_usd"], 19.86587)

    def test_live_forecast_panel_keeps_trade_markers_without_orderbook(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            db.execute(
                """
                insert into live_orders (
                    created_at_utc, outcome_id, label, side, action, order_type,
                    status, requested_size_usd, limit_price, fill_price,
                    fill_size_usd, fill_shares, reason
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-05-06T04:00:00+00:00",
                    "yes25",
                    "25°C",
                    "BUY_YES",
                    "BUY",
                    "FAK",
                    "filled",
                    20.0,
                    0.29,
                    0.29,
                    20.0,
                    68.9655,
                    "test live buy",
                ),
            )
            db.execute(
                """
                insert into live_orders (
                    created_at_utc, outcome_id, label, side, action, order_type,
                    status, requested_size_usd, limit_price, fill_price,
                    fill_size_usd, fill_shares, reason
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-05-06T05:00:00+00:00",
                    "yes25",
                    "25°C",
                    "SELL",
                    "SELL",
                    "FAK",
                    "filled",
                    20.0,
                    0.31,
                    0.31,
                    20.0,
                    64.5161,
                    "test live sell",
                ),
            )
            db.commit()

            panel = forecast_panels(
                db,
                today=date(2026, 5, 6),
                token_side="YES",
                marker_source="live",
            )["panels"][0]

            traded = [
                item for item in panel["top_tokens"] if item["token_id"] == "yes25"
            ]
            self.assertEqual(len(traded), 1)
            self.assertEqual(
                [marker["text"] for marker in traded[0]["markers"]], ["B", "S"]
            )
            self.assertEqual(traded[0]["points"], [])
            self.assertAlmostEqual(traded[0]["latest_price"], 0.31)

    def test_live_trade_rows_backfill_zero_fill_size_from_price_and_shares(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            store_orderbook(
                db,
                "yes25",
                OrderBook(
                    "yes25",
                    bids=[(0.11, 400)],
                    asks=[(0.12, 400)],
                    tick_size=0.01,
                    min_order_size=5,
                ),
            )
            db.execute(
                """
                insert into live_orders (
                    created_at_utc, outcome_id, label, side, action, order_type,
                    status, requested_size_usd, limit_price, fill_price,
                    fill_size_usd, fill_shares, reason
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-05-06T15:50:07+00:00",
                    "yes25",
                    "25°C",
                    "BUY_YES",
                    "BUY",
                    "FAK",
                    "filled",
                    20.0,
                    0.19,
                    0.19,
                    0.0,
                    157.5714,
                    "forecast bucket priced unrealistically low vs HKO forecast",
                ),
            )
            upsert_live_position(db, "yes25", 157.5714, 0.19, 0.0)

            rows = live_trade_rows(db, "open")["rows"]

            self.assertAlmostEqual(rows[0]["fill_size_usd"], 29.938566)
            self.assertAlmostEqual(rows[0]["unrealized_pnl"], -12.605712)

    def test_live_dashboard_replays_orders_when_position_avg_is_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            store_orderbook(
                db,
                "yes25",
                OrderBook(
                    "yes25",
                    bids=[(0.24, 400)],
                    asks=[(0.25, 400)],
                    tick_size=0.01,
                    min_order_size=5,
                ),
            )
            db.execute(
                """
                insert into live_orders (
                    created_at_utc, outcome_id, label, side, action, order_type,
                    status, requested_size_usd, limit_price, fill_price,
                    fill_size_usd, fill_shares, reason
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-05-06T15:50:07+00:00",
                    "yes25",
                    "25°C",
                    "BUY_YES",
                    "BUY",
                    "FAK",
                    "filled",
                    20.0,
                    0.19,
                    0.19,
                    0.0,
                    100.0,
                    "forecast bucket priced unrealistically low vs HKO forecast",
                ),
            )
            upsert_live_position(db, "yes25", 100.0, 0.0, 0.0)

            stats = live_dashboard_payload(db)
            rows = live_trade_rows(db, "open")["rows"]

            self.assertAlmostEqual(stats["open_exposure_usd"], 19.0)
            self.assertAlmostEqual(stats["executable_unrealized_pnl"], 5.0)
            self.assertAlmostEqual(rows[0]["avg_price"], 0.19)
            self.assertAlmostEqual(rows[0]["net_shares"], 100.0)

    def test_live_open_rows_omit_buy_lots_consumed_by_sells(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            store_orderbook(
                db,
                "yes25",
                OrderBook(
                    "yes25",
                    bids=[(0.24, 400)],
                    asks=[(0.25, 400)],
                    tick_size=0.01,
                    min_order_size=5,
                ),
            )
            for created_at, side, price, shares in (
                ("2026-05-06T14:00:00+00:00", "BUY_YES", 0.36, 50.0),
                ("2026-05-06T15:00:00+00:00", "SELL", 0.27, 50.0),
                ("2026-05-06T16:00:00+00:00", "BUY_YES", 0.19, 100.0),
            ):
                db.execute(
                    """
                    insert into live_orders (
                        created_at_utc, outcome_id, label, side, action, order_type,
                        status, requested_size_usd, limit_price, fill_price,
                        fill_size_usd, fill_shares, reason
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        created_at,
                        "yes25",
                        "25°C",
                        side,
                        "SELL" if side == "SELL" else "BUY",
                        "FAK",
                        "filled",
                        20.0,
                        price,
                        price,
                        0.0,
                        shares,
                        "forecast bucket priced unrealistically low vs HKO forecast",
                    ),
                )
            upsert_live_position(db, "yes25", 100.0, 0.0, -4.5)

            stats = live_dashboard_payload(db)
            rows = live_trade_rows(db, "open")["rows"]

            self.assertEqual([row["action"] for row in rows], ["BUY_YES"])
            self.assertAlmostEqual(rows[0]["shares"], 100.0)
            self.assertAlmostEqual(rows[0]["fill_size_usd"], 19.0)
            self.assertAlmostEqual(rows[0]["unrealized_pnl"], 5.0)
            self.assertAlmostEqual(
                sum(row["unrealized_pnl"] for row in rows),
                stats["executable_unrealized_pnl"],
            )

    def test_live_realized_rows_backfill_zero_sell_proceeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            for created_at, side, price, shares in (
                ("2026-05-06T14:00:00+00:00", "BUY_YES", 0.20, 100.0),
                ("2026-05-06T15:00:00+00:00", "SELL", 0.26, 25.0),
            ):
                db.execute(
                    """
                    insert into live_orders (
                        created_at_utc, outcome_id, label, side, action, order_type,
                        status, requested_size_usd, limit_price, fill_price,
                        fill_size_usd, fill_shares, reason
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        created_at,
                        "yes25",
                        "25°C",
                        side,
                        "SELL" if side == "SELL" else "BUY",
                        "FAK",
                        "filled",
                        20.0,
                        price,
                        price,
                        0.0,
                        shares,
                        "position invalidated by hourly forecast",
                    ),
                )
            upsert_live_position(db, "yes25", 75.0, 0.20, 1.5)

            rows = live_trade_rows(db, "realized")["rows"]

            self.assertAlmostEqual(rows[0]["fill_size_usd"], 6.5)
            self.assertAlmostEqual(rows[0]["realized_pnl"], 1.5)

    def test_dashboard_html_has_delayed_crosshair_tooltip(self):
        self.assertIn('id="chart-tooltip"', INDEX_HTML)
        self.assertIn("Bot signal high", INDEX_HTML)
        self.assertIn("Bot signal low", INDEX_HTML)
        self.assertIn('Since-midnight min', INDEX_HTML)
        self.assertIn("setTimeout(() => showTooltip(tooltipState), 1000)", INDEX_HTML)
        self.assertIn("chart.subscribeCrosshairMove", INDEX_HTML)
        self.assertIn("function chartValueAt(points, time)", INDEX_HTML)
        self.assertIn("value: chartValueAt(d.data, param.time)", INDEX_HTML)
        self.assertIn("horzLine: { visible: false, labelVisible: false }", INDEX_HTML)
        self.assertIn("priceLineVisible: false", INDEX_HTML)
        self.assertIn('id="token-side"', INDEX_HTML)
        self.assertIn("function markerOnlySeries(chart, markers)", INDEX_HTML)
        self.assertIn("s.setData(markers.map(m => ({ time: m.time, value: m.price })))", INDEX_HTML)
        self.assertIn("lineVisible: false", INDEX_HTML)
        self.assertNotIn("s.setMarkers(markers)", INDEX_HTML)
        self.assertIn("const markerSeries = markerOnlySeries", INDEX_HTML)
        self.assertIn(".trade-bubble.buy", INDEX_HTML)
        self.assertIn("function renderTradeBubbles(lead)", INDEX_HTML)
        self.assertNotIn("function renderSignalBubblesForChart", INDEX_HTML)
        self.assertNotIn("function signalDescriptorsForChart", INDEX_HTML)
        self.assertIn("Bot signal high", INDEX_HTML)
        self.assertIn("Bot signal low", INDEX_HTML)
        self.assertIn("subscribeVisibleTimeRangeChange(renderAllTradeBubbles)", INDEX_HTML)
        self.assertIn("nearestTrade(d.markers, param.time)", INDEX_HTML)
        self.assertIn('/api/forecast-panels?side=${encodeURIComponent(tokenSide)}', INDEX_HTML)
        self.assertIn('"#ffb74d", "#4dd0e1"', INDEX_HTML)
        self.assertIn("Latest hourly forecast", INDEX_HTML)
        self.assertNotIn("OCF forecast high", INDEX_HTML)
        self.assertNotIn("OCF forecast low", INDEX_HTML)
        self.assertIn("marker.signal_time_hkt", INDEX_HTML)
        self.assertIn("t.signal_time_hkt", INDEX_HTML)
        self.assertIn('name: "Actual - forecast"', INDEX_HTML)
        self.assertNotIn('data-series-key="hourlyError"', INDEX_HTML)
        self.assertIn("pointMarkersVisible: true", INDEX_HTML)
        self.assertIn("pointMarkersRadius: 2", INDEX_HTML)
        self.assertIn("lowHourlyActual: false", INDEX_HTML)
        self.assertIn("hourlyActual: false", INDEX_HTML)
        self.assertNotIn('bubble.textContent = descriptor.kind === "low" ? "L" : "H"', INDEX_HTML)
        self.assertNotIn(".signal-bubble", INDEX_HTML)
        self.assertIn("function lineDataForDisplay(points, spanSeconds = 600)", INDEX_HTML)
        self.assertIn("d0HourlyActualData = lineDataForDisplay(panel.hourly_actual || [], 600)", INDEX_HTML)
        self.assertIn("d0HourlyErrorData = panel.hourly_error || []", INDEX_HTML)
        self.assertIn("d0CurrentTempData = lineDataForDisplay(panel.current_temp || [], 600)", INDEX_HTML)
        self.assertIn("d0HourlyForecastSeries.setData", INDEX_HTML)
        self.assertNotIn("d0HourlyErrorSeries.setData", INDEX_HTML)
        self.assertIn("function hktWallClockUnix", INDEX_HTML)
        self.assertIn("function initialDataWindowRange", INDEX_HTML)
        self.assertIn("function currentHktWallClockUnix", INDEX_HTML)
        self.assertIn("function makeTimeScaffoldSeries", INDEX_HTML)
        self.assertIn("function hktMinuteWhitespace", INDEX_HTML)
        self.assertIn("function setTimeScaleScaffold", INDEX_HTML)
        self.assertIn("function activeHktDataDate", INDEX_HTML)
        self.assertIn("function filterSeriesToHktDate", INDEX_HTML)
        self.assertIn("function projectSeriesToHktDate", INDEX_HTML)
        self.assertIn("projectSeriesToHktDate(filterSeriesToHktDate(panel.forecast, dateText), panel.target_date)", INDEX_HTML)
        self.assertIn("const displayPanel = panelForActiveHktDataDate(panel)", INDEX_HTML)
        self.assertIn("function setInitialDataWindowRangeOnce", INDEX_HTML)
        self.assertIn('const rangeKey = `${key}:${dateText}`', INDEX_HTML)
        self.assertIn("const latest = latestSeriesTime(seriesGroups)", INDEX_HTML)
        self.assertIn("const to = currentHktWallClockUnix(latest)", INDEX_HTML)
        self.assertIn("for (let minute = 0; minute <= 24 * 60; minute += 1)", INDEX_HTML)
        self.assertIn("chartState.timeScaffold.setData(hktMinuteWhitespace(dateText))", INDEX_HTML)
        self.assertIn('setInitialDataWindowRangeOnce("d0", charts[0], panel.target_date, [', INDEX_HTML)
        self.assertIn('setInitialDataWindowRangeOnce("l0", lowCharts[0], panel.target_date, [', INDEX_HTML)
        self.assertIn("setInitialDataWindowRangeOnce(`d${lead}`, charts[lead], panel.target_date, [", INDEX_HTML)
        self.assertIn("setInitialDataWindowRangeOnce(`l${lead}`, lowCharts[lead], panel.target_date, [", INDEX_HTML)
        self.assertIn('data-series-key="hourlyActual"', INDEX_HTML)
        self.assertIn("legendButton(key, color", INDEX_HTML)
        self.assertIn("d0-token-${item.token_id}", INDEX_HTML)
        self.assertIn("applySeriesVisibility", INDEX_HTML)
        self.assertIn("seriesVisibility[key] = !isSeriesVisible(key)", INDEX_HTML)
        self.assertIn("mouseWheel: false", INDEX_HTML)
        self.assertIn("function installModifierWheelZoom", INDEX_HTML)
        self.assertIn("if (!event.metaKey && !event.ctrlKey) return", INDEX_HTML)
        self.assertIn("const chartTimeToUnixSeconds = (time) =>", INDEX_HTML)
        self.assertIn("const fmtHKTUpdate = (value) =>", INDEX_HTML)
        self.assertIn("tickMarkFormatter: fmtHKTTime", INDEX_HTML)
        self.assertIn("const cursorX = event.clientX - rect.left", INDEX_HTML)
        self.assertIn("const cursorLogical = chart.timeScale().coordinateToLogical(cursorX)", INDEX_HTML)
        self.assertIn("cursorLogical - nextSpan * cursorRatio", INDEX_HTML)
        self.assertIn('installModifierWheelZoom("pnl-chart", pnlChart)', INDEX_HTML)
        self.assertIn("function fitChartOnce", INDEX_HTML)
        self.assertIn("if (fittedCharts.has(key)) return", INDEX_HTML)
        self.assertIn("fittedCharts.clear()", INDEX_HTML)
        self.assertIn("pressedMouseMove: true", INDEX_HTML)
        self.assertIn("charts[lead].chart.removeSeries(s.series)", INDEX_HTML)
        self.assertIn("if (s.markerSeries) charts[lead].chart.removeSeries(s.markerSeries)", INDEX_HTML)
        self.assertIn('data-drilldown="${c.drilldown}"', INDEX_HTML)
        self.assertIn('id="trade-drilldown"', INDEX_HTML)
        self.assertIn("function showTradeDrilldown(view)", INDEX_HTML)
        self.assertIn('/api/paper-trades?view=${encodeURIComponent(view)}', INDEX_HTML)
        self.assertIn('document.querySelectorAll(".chart-section")', INDEX_HTML)
        self.assertIn("Back to charts", INDEX_HTML)
        self.assertIn("<th>Time HKT</th>", INDEX_HTML)

    def test_live_html_reuses_paper_dashboard_with_live_endpoints(self):
        self.assertIn('class="live-banner"', LIVE_HTML)
        self.assertIn("LIVE ORDERS", LIVE_HTML)
        self.assertIn("Polymarket execution enabled", LIVE_HTML)
        self.assertIn("live-banner-dot", LIVE_HTML)
        self.assertNotIn("Paper Trading Mode", LIVE_HTML)
        self.assertIn("whenitrains · HK temperature live desk", LIVE_HTML)
        self.assertIn('/api/live/trades?view=${encodeURIComponent(view)}', LIVE_HTML)
        self.assertIn('fetchJSON("/api/live/stats")', LIVE_HTML)

    def test_historicals_html_contains_route_specific_api_and_charts(self):
        self.assertIn("HKO historical accuracy", HISTORICALS_HTML)
        self.assertIn('fetchJSON("/api/historicals")', HISTORICALS_HTML)
        self.assertIn("max-forecast-accuracy-chart", HISTORICALS_HTML)
        self.assertIn("max-price-lead-chart", HISTORICALS_HTML)
        self.assertIn("max-pnl-histograms", HISTORICALS_HTML)
        self.assertIn("min-forecast-accuracy-chart", HISTORICALS_HTML)
        self.assertIn("min-price-lead-chart", HISTORICALS_HTML)
        self.assertIn("min-pnl-histograms", HISTORICALS_HTML)


def _seed_dashboard_db(path: Path):
    db = connect(path)
    migrate(db)
    store_polymarket_event(
        db,
        TemperatureMarket(
            event_id="event",
            event_slug="highest-temperature-in-hong-kong-on-2026-05-06",
            title="Highest temperature in Hong Kong on 2026-05-06?",
            target_date=date(2026, 5, 6),
            outcomes=[
                Outcome(
                    market_id=f"m{temp}",
                    label=f"{temp}°C",
                    predicate=parse_outcome_label(f"{temp}°C"),
                    yes_token_id=f"yes{temp}",
                    no_token_id=f"no{temp}",
                )
                for temp in [24, 25, 26, 27, 28, 29, 30]
            ],
        ),
    )
    store_polymarket_event(
        db,
        TemperatureMarket(
            event_id="low-event",
            event_slug="lowest-temperature-in-hong-kong-on-2026-05-06",
            title="Lowest temperature in Hong Kong on 2026-05-06?",
            target_date=date(2026, 5, 6),
            outcomes=[
                Outcome(
                    market_id=f"lowm{temp}",
                    label=f"{temp}°C",
                    predicate=parse_outcome_label(f"{temp}°C"),
                    yes_token_id=f"lowyes{temp}",
                    no_token_id=f"lowno{temp}",
                )
                for temp in [22, 23, 24, 25, 26]
            ],
        ),
    )
    return db


def _seed_historical_accuracy_day(db):
    snapshot = store_raw_snapshot(db, "hko", "ocf", "{}")
    for modified, max_temp, min_temp in [
        (20260506100000, 27.0, 24.0),
        (20260506120000, 28.0, 23.0),
    ]:
        store_ocf_forecast_samples(
            db,
            snapshot.id,
            [
                OcfForecastSample(
                    forecast_date_hkt=date(2026, 5, 6),
                    forecast_min_c=round(min_temp),
                    forecast_max_c=round(max_temp),
                    raw_min_c=min_temp,
                    raw_max_c=max_temp,
                    hourly_temperatures=[
                        {
                            "forecast_hour_hkt": "2026-05-06T09:00:00+08:00",
                            "temperature_c": min_temp,
                        },
                        {
                            "forecast_hour_hkt": "2026-05-06T13:00:00+08:00",
                            "temperature_c": max_temp,
                        }
                    ],
                    raw={"LastModified": modified},
                )
            ],
        )
    actual_snapshot = store_raw_snapshot(db, "hko", "rhrread", "{}")
    for observed_at, temp in [
        (datetime(2026, 5, 6, 9, 0, tzinfo=HKT), 25.0),
        (datetime(2026, 5, 6, 12, 0, tzinfo=HKT), 26.8),
        (datetime(2026, 5, 6, 13, 0, tzinfo=HKT), 27.5),
        (datetime(2026, 5, 6, 14, 0, tzinfo=HKT), 23.5),
    ]:
        store_hko_current_temperature(
            db,
            actual_snapshot.id,
            HkoCurrentTemperature(
                observed_at_hkt=observed_at,
                station="Hong Kong Observatory",
                temperature_c=temp,
                raw={},
            ),
        )


def _set_latest_orderbook_time(db, token_id: str, fetched_at: str, price: float):
    store_orderbook(
        db,
        token_id,
        OrderBook(
            token_id,
            bids=[(price - 0.02, 10)],
            asks=[(price, 10)],
            tick_size=0.01,
            min_order_size=5,
        ),
    )
    db.execute(
        """
        update orderbook_snapshots
        set fetched_at_utc = ?
        where id = (select max(id) from orderbook_snapshots)
        """,
        (fetched_at,),
    )
    db.commit()


def _insert_historical_order(
    db,
    created_at: str,
    token_id: str,
    side: str,
    fill_price: float,
    fill_size: float,
    reason: str,
    decision_reason: str | None = None,
):
    db.execute(
        """
        insert into paper_orders
        (created_at_utc, outcome_id, side, limit_price, size_usd,
         simulated_fill_price, simulated_fill_size_usd, status, reason)
        values (?, ?, ?, ?, ?, ?, ?, 'filled', ?)
        """,
        (created_at, token_id, side, fill_price, fill_size, fill_price, fill_size, reason),
    )
    if decision_reason is not None:
        token_side = "NO" if side == "BUY_NO" else "YES"
        db.execute(
            """
            insert into paper_decisions
            (created_at_utc, event_type, outcome_id, label, side, action,
             status, reason, details_json)
            values (?, 'test_signal', ?, ?, ?, 'BUY', 'filled', ?, '{}')
            """,
            (created_at, token_id, token_id, token_side, decision_reason),
        )
    db.commit()


class _FakeLiveReconcileClient:
    def reconcile_order(self, order_id: str | None, token_id: str) -> dict:
        return {"order_id": order_id, "token_id": token_id, "status": "unknown"}

    def trades_for_order(self, order_id: str | None, token_id: str) -> list[dict]:
        return [
            {
                "id": "trade-1",
                "taker_order_id": order_id,
                "asset_id": token_id,
                "side": "BUY",
                "size": "68.503",
                "price": "0.29",
                "status": "CONFIRMED",
            }
        ]


if __name__ == "__main__":
    unittest.main()
