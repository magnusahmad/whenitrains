import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from whenitrains.cli import (
    _discover_market,
    _fetch_current_temperature,
    _fetch_ocf_forecast,
    main,
)
from whenitrains.hko import (
    AWS_GIS_FORECAST_URL,
    AWS_GIS_READINGS_URL,
    FetchResponse,
    OCF_STATION_URL,
    RHRREAD_URL,
)
from whenitrains.storage import connect, migrate


class CliDiscoveryTests(unittest.TestCase):
    def test_live_env_exports_prints_shell_safe_required_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "live.env"
            env_path.write_text(
                "\n".join(
                    [
                        "WHENITRAINS_TRADING_MODE=live",
                        "POLYMARKET_SIGNATURE_TYPE=1",
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
                    "export POLYMARKET_SIGNATURE_TYPE=1",
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
                        "POLYMARKET_SIGNATURE_TYPE=1",
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
