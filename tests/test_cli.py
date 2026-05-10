import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import date
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from whenitrains.cli import (
    _discover_market,
    _fetch_current_temperature,
    _fetch_ocf_forecast,
    _fetch_orderbooks,
    main,
)
from whenitrains.hko import (
    AWS_GIS_FORECAST_URL,
    AWS_GIS_READINGS_URL,
    FetchResponse,
    OCF_STATION_URL,
    RHRREAD_URL,
)
from whenitrains.markets import parse_outcome_label
from whenitrains.polymarket import OrderBook, Outcome, TemperatureMarket
from whenitrains.storage import connect, live_setting_enabled, migrate, store_polymarket_event


class CliDiscoveryTests(unittest.TestCase):
    def test_fetch_orderbooks_fetches_tokens_concurrently_and_stores_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            store_polymarket_event(
                db,
                TemperatureMarket(
                    event_id="event",
                    event_slug="highest-temperature-in-hong-kong-on-may-10-2026",
                    title="Highest temperature",
                    target_date=date(2026, 5, 10),
                    outcomes=[
                        Outcome(
                            market_id="m26",
                            label="26°C",
                            predicate=parse_outcome_label("26°C"),
                            yes_token_id="yes26",
                            no_token_id="no26",
                        ),
                        Outcome(
                            market_id="m27",
                            label="27°C",
                            predicate=parse_outcome_label("27°C"),
                            yes_token_id="yes27",
                            no_token_id="no27",
                        ),
                    ],
                ),
            )
            active_fetches = 0
            max_active_fetches = 0
            lock = threading.Lock()

            def fake_fetch(token_id):
                nonlocal active_fetches, max_active_fetches
                with lock:
                    active_fetches += 1
                    max_active_fetches = max(max_active_fetches, active_fetches)
                time.sleep(0.02)
                with lock:
                    active_fetches -= 1
                return OrderBook(
                    token_id,
                    bids=[(0.10, 10)],
                    asks=[(0.20, 10)],
                    tick_size=0.01,
                    min_order_size=5,
                )

            with patch("whenitrains.cli.fetch_orderbook", side_effect=fake_fetch):
                _fetch_orderbooks(db, date(2026, 5, 10), quiet=True, max_workers=4)

            self.assertGreater(max_active_fetches, 1)
            rows = db.execute(
                """
                select outcome_id, best_bid, best_ask
                from orderbook_snapshots
                order by outcome_id
                """
            ).fetchall()
            self.assertEqual(
                [(row["outcome_id"], row["best_bid"], row["best_ask"]) for row in rows],
                [
                    ("no26", 0.10, 0.20),
                    ("no27", 0.10, 0.20),
                    ("yes26", 0.10, 0.20),
                    ("yes27", 0.10, 0.20),
                ],
            )

    def test_live_env_exports_prints_shell_safe_required_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "live.env"
            env_path.write_text(
                "\n".join(
                    [
                        "WHENITRAINS_TRADING_MODE=live",
                        "POLYMARKET_SIGNATURE_TYPE=3",
                        "POLYMARKET_FUNDER_ADDRESS=0xfunder",
                        "POLYMARKET_API_KEY=api key",
                        "POLYMARKET_API_SECRET=secret'with quote",
                        "POLYMARKET_API_PASSPHRASE=passphrase",
                        "IGNORED=value",
                    ]
                )
                + "\n"
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(["live-env-exports", "--env-file", str(env_path)])

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                stdout.getvalue().splitlines(),
                [
                    "export WHENITRAINS_TRADING_MODE=live",
                    "export POLYMARKET_SIGNATURE_TYPE=3",
                    "export POLYMARKET_FUNDER_ADDRESS=0xfunder",
                    "export POLYMARKET_API_KEY='api key'",
                    """export POLYMARKET_API_SECRET='secret'"'"'with quote'""",
                    "export POLYMARKET_API_PASSPHRASE=passphrase",
                ],
            )

    def test_live_env_exports_fails_closed_when_required_secret_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "live.env"
            env_path.write_text(
                "\n".join(
                    [
                        "WHENITRAINS_TRADING_MODE=live",
                        "POLYMARKET_SIGNATURE_TYPE=3",
                        "POLYMARKET_FUNDER_ADDRESS=0xfunder",
                        "POLYMARKET_API_KEY=api",
                        "POLYMARKET_API_PASSPHRASE=passphrase",
                    ]
                )
                + "\n"
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(["live-env-exports", "--env-file", str(env_path)])

            self.assertEqual(exit_code, 2)
            self.assertEqual(
                stdout.getvalue().strip(),
                "missing live env values: POLYMARKET_API_SECRET",
            )

    def test_live_scheduler_starts_websocket_runtime_and_passes_book_cache_to_ticks(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            runtime_events = []
            book_cache = object()

            class FakeRuntime:
                all_running = True

                def __init__(self):
                    self.book_cache = book_cache

                def start(self):
                    runtime_events.append("start")

                def stop(self, timeout=None):
                    runtime_events.append(("stop", timeout))

            fake_runtime = FakeRuntime()

            def run_loop(*args, **kwargs):
                self.assertIn("alert_sink", kwargs)
                kwargs["reconcile_watchdog_fn"](args[0])
                kwargs["run_tick_fn"](args[0], date(2026, 5, 4))
                kwargs["fast_event_handler"](args[0], date(2026, 5, 4))

            stdout = StringIO()
            with (
                patch("whenitrains.cli.load_live_config", return_value=object()),
                patch("whenitrains.cli.PolymarketClobClient", return_value=object()),
                patch(
                    "whenitrains.cli.preflight_live",
                    return_value=SimpleNamespace(ok=True, reason="ok"),
                ),
                patch(
                    "whenitrains.cli.LiveWebSocketRuntime.for_live_scheduler",
                    return_value=fake_runtime,
                ) as runtime_factory,
                patch("whenitrains.cli.run_scheduled_paper_loop", side_effect=run_loop),
                patch("whenitrains.cli.run_live_tick", return_value=object()) as live_tick,
                patch("whenitrains.cli.find_live_position_drifts", return_value=[]),
                redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "live-scheduler",
                        "--live",
                        "--ticks",
                        "0",
                        "--no-startup-backup",
                    ]
                )

            self.assertEqual(exit_code, 0)
            runtime_factory.assert_called_once()
            self.assertEqual(runtime_events, ["start", ("stop", 5)])
            self.assertEqual(live_tick.call_count, 2)
            for call in live_tick.call_args_list:
                self.assertIs(call.kwargs["book_cache"], book_cache)

    def test_live_scheduler_freezes_entries_when_startup_drift_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"

            def run_loop(*args, **kwargs):
                return None

            stdout = StringIO()
            with (
                patch("whenitrains.cli.load_live_config", return_value=object()),
                patch("whenitrains.cli.PolymarketClobClient", return_value=object()),
                patch(
                    "whenitrains.cli.preflight_live",
                    return_value=SimpleNamespace(ok=True, reason="ok"),
                ),
                patch("whenitrains.cli.find_live_position_drifts", return_value=[object()]),
                patch("whenitrains.cli.run_scheduled_paper_loop", side_effect=run_loop),
                redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "live-scheduler",
                        "--live",
                        "--ticks",
                        "0",
                        "--no-startup-backup",
                        "--no-websockets",
                    ]
                )

            self.assertEqual(exit_code, 0)
            db = connect(db_path)
            try:
                self.assertTrue(live_setting_enabled(db, "block_new_entries"))
                risk = db.execute(
                    "select event_type, severity from risk_events order by id desc limit 1"
                ).fetchone()
                self.assertEqual(risk["event_type"], "live_startup_health_failed")
                self.assertEqual(risk["severity"], "critical")
            finally:
                db.close()
            self.assertIn("1 local/CLOB drift items", stdout.getvalue())

    def test_live_scheduler_reconcile_watchdog_freezes_entries_when_drift_appears(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            drift_calls = []

            def run_loop(*args, **kwargs):
                result = kwargs["reconcile_watchdog_fn"](args[0])
                self.assertIn("live reconcile watchdog froze entries", result.notes[0])

            def drifts(_db, _client):
                drift_calls.append("scan")
                return [] if len(drift_calls) == 1 else [
                    SimpleNamespace(
                        token_id="yes25",
                        local_shares=12.5,
                        clob_sellable_shares=None,
                        drift_shares=None,
                    )
                ]

            stdout = StringIO()
            with (
                patch("whenitrains.cli.load_live_config", return_value=object()),
                patch("whenitrains.cli.PolymarketClobClient", return_value=object()),
                patch(
                    "whenitrains.cli.preflight_live",
                    return_value=SimpleNamespace(ok=True, reason="ok"),
                ),
                patch("whenitrains.cli.find_live_position_drifts", side_effect=drifts),
                patch("whenitrains.cli.repair_live_position_drifts", return_value=0),
                patch("whenitrains.cli.run_scheduled_paper_loop", side_effect=run_loop),
                redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "live-scheduler",
                        "--live",
                        "--ticks",
                        "0",
                        "--no-startup-backup",
                        "--no-websockets",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(drift_calls, ["scan", "scan"])
            db = connect(db_path)
            try:
                self.assertTrue(live_setting_enabled(db, "block_new_entries"))
                risk = db.execute(
                    "select event_type, severity from risk_events order by id desc limit 1"
                ).fetchone()
                self.assertEqual(risk["event_type"], "live_startup_health_failed")
                self.assertEqual(risk["severity"], "critical")
            finally:
                db.close()

    def test_live_scheduler_reconcile_watchdog_repairs_lower_clob_drift_before_freezing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            drift = SimpleNamespace(
                token_id="yes25",
                local_shares=12.5,
                clob_sellable_shares=7.0,
                drift_shares=5.5,
            )

            class FakeRuntime:
                book_cache = object()
                all_running = True

                def start(self):
                    return None

                def stop(self, timeout=None):
                    return None

            def run_loop(*args, **kwargs):
                result = kwargs["reconcile_watchdog_fn"](args[0])
                self.assertEqual(result.notes, ("live reconcile watchdog repaired 1 local/CLOB drift items",))

            with (
                patch("whenitrains.cli.load_live_config", return_value=object()),
                patch("whenitrains.cli.PolymarketClobClient", return_value=object()),
                patch(
                    "whenitrains.cli.preflight_live",
                    return_value=SimpleNamespace(ok=True, reason="ok"),
                ),
                patch(
                    "whenitrains.cli.find_live_position_drifts",
                    side_effect=[[], [drift], []],
                ) as find_drifts,
                patch(
                    "whenitrains.cli.LiveWebSocketRuntime.for_live_scheduler",
                    return_value=FakeRuntime(),
                ),
                patch("whenitrains.cli.repair_live_position_drifts", return_value=1) as repair,
                patch("whenitrains.cli.run_scheduled_paper_loop", side_effect=run_loop),
                redirect_stdout(StringIO()),
            ):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "live-scheduler",
                        "--live",
                        "--ticks",
                        "0",
                        "--no-startup-backup",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(find_drifts.call_count, 3)
            repair.assert_called_once()
            db = connect(db_path)
            try:
                self.assertFalse(live_setting_enabled(db, "block_new_entries"))
            finally:
                db.close()

    def test_live_scheduler_reconcile_watchdog_freezes_entries_when_websocket_runtime_stalls(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"

            class FakeRuntime:
                book_cache = object()
                all_running = False

                def start(self):
                    return None

                def stop(self, timeout=None):
                    return None

            def run_loop(*args, **kwargs):
                result = kwargs["reconcile_watchdog_fn"](args[0])
                self.assertIn("live reconcile watchdog froze entries", result.notes[0])

            with (
                patch("whenitrains.cli.load_live_config", return_value=object()),
                patch("whenitrains.cli.PolymarketClobClient", return_value=object()),
                patch(
                    "whenitrains.cli.preflight_live",
                    return_value=SimpleNamespace(ok=True, reason="ok"),
                ),
                patch("whenitrains.cli.find_live_position_drifts", return_value=[]),
                patch(
                    "whenitrains.cli.LiveWebSocketRuntime.for_live_scheduler",
                    return_value=FakeRuntime(),
                ),
                patch("whenitrains.cli.run_scheduled_paper_loop", side_effect=run_loop),
                redirect_stdout(StringIO()),
            ):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "live-scheduler",
                        "--live",
                        "--ticks",
                        "0",
                        "--no-startup-backup",
                    ]
                )

            self.assertEqual(exit_code, 0)
            db = connect(db_path)
            try:
                self.assertTrue(live_setting_enabled(db, "block_new_entries"))
                risk = db.execute(
                    "select details_json from risk_events order by id desc limit 1"
                ).fetchone()
                self.assertIn("market websocket disconnected", risk["details_json"])
                self.assertIn("user websocket disconnected", risk["details_json"])
            finally:
                db.close()

    def test_discover_market_fetches_highest_and_lowest_temperature_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            requested_slugs = []

            def fake_fetch(slug):
                requested_slugs.append(slug)
                if slug not in {
                    "highest-temperature-in-hong-kong-on-may-7-2026",
                    "lowest-temperature-in-hong-kong-on-may-7-2026",
                }:
                    return None
                return {
                    "id": f"event-{slug}",
                    "slug": slug,
                    "title": slug,
                    "eventDate": "2026-05-07",
                    "markets": [
                        {
                            "id": f"market-{slug}",
                            "question": slug,
                            "groupItemTitle": "25°C",
                            "clobTokenIds": '["YES_TOKEN", "NO_TOKEN"]',
                        }
                    ],
                }

            with (
                patch("whenitrains.cli.fetch_hk_temperature_event", side_effect=fake_fetch),
                patch("whenitrains.cli.resolution_rules_warning", return_value=None),
            ):
                discovered = _discover_market(db, date(2026, 5, 7))

            self.assertTrue(discovered)
            self.assertEqual(
                requested_slugs,
                [
                    "highest-temperature-in-hong-kong-on-may-7-2026",
                    "lowest-temperature-in-hong-kong-on-may-7-2026",
                ],
            )
            slugs = [
                row["slug"]
                for row in db.execute("select slug from markets order by slug")
            ]
            self.assertEqual(
                slugs,
                [
                    "highest-temperature-in-hong-kong-on-may-7-2026",
                    "lowest-temperature-in-hong-kong-on-may-7-2026",
                ],
            )

    def test_fetch_current_temperature_records_aws_gis_actual_on_success(self):
        aws_payload = """Latest readings recorded at 14:30 Hong Kong Time 7 May 2026
STN,WINDDIRECTION,WINDSPEED,GUST,TEMP,RH,MAXTEMP,MINTEMP,GRASSTEMP,GRASSMINTEMP,VISIBILITY,PRESSURE,TEMPDIFFERENCE,HEATINDEX,
HKO,,,,28.9,69,29.3,24.0,,,,1011.0,4.8,27.3,
"""
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)

            with patch(
                "whenitrains.cli.fetch_response",
                return_value=FetchResponse(AWS_GIS_READINGS_URL, aws_payload, {}),
            ):
                returned = _fetch_current_temperature(db)

            self.assertEqual(returned, aws_payload)
            obs = db.execute(
                """
                select station, temperature_c, since_midnight_max_c, since_midnight_min_c
                from hko_current_observations
                """
            ).fetchone()
            self.assertEqual(obs["station"], "HKO")
            self.assertEqual(obs["temperature_c"], 28.9)
            self.assertEqual(obs["since_midnight_max_c"], 29.3)
            sources = [
                row["source"]
                for row in db.execute("select source from hko_source_update_minutes")
            ]
            self.assertEqual(sources, ["aws_gis_actual"])

    def test_fetch_current_temperature_learns_aws_gis_publish_minute(self):
        aws_payload = """Latest readings recorded at 14:30 Hong Kong Time 7 May 2026
STN,WINDDIRECTION,WINDSPEED,GUST,TEMP,RH,MAXTEMP,MINTEMP,GRASSTEMP,GRASSMINTEMP,VISIBILITY,PRESSURE,TEMPDIFFERENCE,HEATINDEX,
HKO,,,,28.9,69,29.3,24.0,,,,1011.0,4.8,27.3,
"""
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)

            with patch(
                "whenitrains.cli.fetch_response",
                return_value=FetchResponse(
                    AWS_GIS_READINGS_URL,
                    aws_payload,
                    {"Last-Modified": "Thu, 07 May 2026 06:38:01 GMT"},
                ),
            ):
                _fetch_current_temperature(db)

            rows = db.execute(
                """
                select update_minute_hkt, json_extract(evidence_json, '$.kind') as kind
                from hko_source_update_minutes
                order by update_minute_hkt
                """
            ).fetchall()
            self.assertEqual(
                [(row["update_minute_hkt"], row["kind"]) for row in rows],
                [("14:30", "payload_header"), ("14:38", "http_Last-Modified")],
            )

    def test_fetch_current_temperature_persists_http_timing(self):
        aws_payload = """Latest readings recorded at 14:30 Hong Kong Time 7 May 2026
STN,WINDDIRECTION,WINDSPEED,GUST,TEMP,RH,MAXTEMP,MINTEMP,GRASSTEMP,GRASSMINTEMP,VISIBILITY,PRESSURE,TEMPDIFFERENCE,HEATINDEX,
HKO,,,,28.9,69,29.3,24.0,,,,1011.0,4.8,27.3,
"""
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)

            with patch(
                "whenitrains.cli.fetch_response",
                return_value=FetchResponse(
                    AWS_GIS_READINGS_URL,
                    aws_payload,
                    {"Last-Modified": "Thu, 07 May 2026 06:38:01 GMT"},
                    fetch_started_at_utc="2026-05-11T00:00:00+00:00",
                    headers_received_at_utc="2026-05-11T00:00:00.040000+00:00",
                    payload_received_at_utc="2026-05-11T00:00:00.090000+00:00",
                    response_elapsed_ms=90.2,
                ),
            ):
                _fetch_current_temperature(db)

            row = db.execute(
                """
                select fetch_started_at_utc, headers_received_at_utc,
                       payload_received_at_utc, response_elapsed_ms
                from raw_snapshots
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(row["fetch_started_at_utc"], "2026-05-11T00:00:00+00:00")
            self.assertEqual(row["headers_received_at_utc"], "2026-05-11T00:00:00.040000+00:00")
            self.assertEqual(row["payload_received_at_utc"], "2026-05-11T00:00:00.090000+00:00")
            self.assertAlmostEqual(row["response_elapsed_ms"], 90.2)

    def test_fetch_current_temperature_labels_rhrread_fallback_and_keeps_aws_failed(self):
        rhrread_payload = """
        {
          "updateTime": "2026-05-07T14:02:00+08:00",
          "temperature": {
            "data": [
              {"place": "Hong Kong Observatory", "value": 29, "unit": "C"}
            ]
          }
        }
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)

            def fake_fetch(url):
                if url == AWS_GIS_READINGS_URL:
                    raise OSError("aws unavailable")
                self.assertEqual(url, RHRREAD_URL)
                return FetchResponse(RHRREAD_URL, rhrread_payload, {})

            with patch("whenitrains.cli.fetch_response", side_effect=fake_fetch):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "AWS GIS actual fetch failed; stored rhrread observation fallback only",
                ):
                    _fetch_current_temperature(db)

            obs = db.execute(
                """
                select station, temperature_c, since_midnight_max_c, since_midnight_min_c
                from hko_current_observations
                """
            ).fetchone()
            self.assertEqual(obs["station"], "Hong Kong Observatory")
            self.assertEqual(obs["temperature_c"], 29.0)
            self.assertIsNone(obs["since_midnight_max_c"])
            sources = [
                row["source"]
                for row in db.execute("select source from hko_source_update_minutes")
            ]
            self.assertEqual(sources, ["rhrread_actual"])

    def test_fetch_forecast_prefers_aws_gis_station_forecast_payload(self):
        payload = """
        {
          "LastModified": 20260507181146,
          "StationCode": "HKO",
          "DailyForecast": [
            {
              "ForecastDate": "20260508",
              "ForecastChanceOfRain": "60%",
              "ForecastMaximumTemperature": 29,
              "ForecastMinimumTemperature": 24
            }
          ],
          "HourlyWeatherForecast": [
            {"ForecastHour": "2026050812", "ForecastTemperature": 28.4},
            {"ForecastHour": "2026050813", "ForecastTemperature": 29.0}
          ]
        }
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)

            with patch(
                "whenitrains.cli.fetch_response",
                return_value=FetchResponse(AWS_GIS_FORECAST_URL, payload, {}),
            ) as fetch:
                _hash, forecasts = _fetch_ocf_forecast(db)

            fetch.assert_called_once_with(AWS_GIS_FORECAST_URL)
            self.assertEqual(len(forecasts), 1)
            snapshot = db.execute(
                "select endpoint from raw_snapshots order by id desc limit 1"
            ).fetchone()
            self.assertEqual(snapshot["endpoint"], AWS_GIS_FORECAST_URL)
            sample = db.execute(
                """
                select raw_max_c, hourly_temperatures_json
                from ocf_forecast_samples
                """
            ).fetchone()
            self.assertEqual(sample["raw_max_c"], 29.0)
            self.assertIn("2026-05-08T13:00:00+08:00", sample["hourly_temperatures_json"])

    def test_fetch_forecast_falls_back_to_ocf_station_url(self):
        payload = """
        {
          "LastModified": 20260507181146,
          "StationCode": "HKO",
          "DailyForecast": [
            {
              "ForecastDate": "20260508",
              "ForecastMaximumTemperature": 29,
              "ForecastMinimumTemperature": 24
            }
          ],
          "HourlyWeatherForecast": []
        }
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)

            def fake_fetch(url):
                if url == AWS_GIS_FORECAST_URL:
                    raise OSError("aws forecast unavailable")
                self.assertEqual(url, OCF_STATION_URL)
                return FetchResponse(OCF_STATION_URL, payload, {})

            with patch("whenitrains.cli.fetch_response", side_effect=fake_fetch):
                _fetch_ocf_forecast(db)

            snapshot = db.execute(
                "select endpoint from raw_snapshots order by id desc limit 1"
            ).fetchone()
            self.assertEqual(snapshot["endpoint"], OCF_STATION_URL)


if __name__ == "__main__":
    unittest.main()
