import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from whenitrains.hko import HKT, HkoCurrentTemperature, OcfForecastSample
from whenitrains.hourly_accuracy import (
    build_hourly_accuracy_report,
    render_hourly_accuracy_report,
)
from whenitrains.storage import (
    connect,
    migrate,
    store_hko_current_temperature,
    store_ocf_forecast_samples,
    store_raw_snapshot,
)


class HourlyAccuracyTests(unittest.TestCase):
    def test_matches_ocf_hourly_forecast_to_current_temperature_by_lead_hour(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            self.addCleanup(db.close)
            migrate(db)
            ocf_snapshot = store_raw_snapshot(db, "hko", "ocf", "{}")
            store_ocf_forecast_samples(
                db,
                ocf_snapshot.id,
                [
                    OcfForecastSample(
                        forecast_date_hkt=datetime(2026, 5, 5, tzinfo=HKT).date(),
                        forecast_min_c=20,
                        forecast_max_c=24,
                        raw_min_c=20.0,
                        raw_max_c=24.0,
                        hourly_temperatures=[
                            {
                                "forecast_hour_hkt": "2026-05-05T18:00:00+08:00",
                                "temperature_c": 23,
                            }
                        ],
                        raw={"LastModified": 20260505170200},
                    )
                ],
            )
            actual_snapshot = store_raw_snapshot(db, "hko", "rhrread", "{}")
            store_hko_current_temperature(
                db,
                actual_snapshot.id,
                HkoCurrentTemperature(
                    observed_at_hkt=datetime(2026, 5, 5, 18, 2, tzinfo=HKT),
                    station="Hong Kong Observatory",
                    temperature_c=24,
                    raw={},
                ),
            )

            rows, summaries = build_hourly_accuracy_report(db)
            report = render_hourly_accuracy_report(rows, summaries)

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].lead_hours, 1)
            self.assertEqual(rows[0].error_c, 1)
            self.assertIn("lead_hours,n,mean_error_c", report)
            self.assertIn("1,1,1.000", report)


if __name__ == "__main__":
    unittest.main()
