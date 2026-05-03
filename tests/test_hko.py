import unittest

from whenitrains.hko import (
    parse_flw_forecast,
    parse_fnd_forecasts,
    parse_since_midnight_csv,
)


SINCE_MIDNIGHT_CSV = """\ufeffDate time (Year),Date time (Month),Date time (Day),Date time (Hour),Date time (Minute),Date time (Second),Date time (Time Zone),Automatic Weather Station,Maximum Air Temperature Since Midnight(degree Celsius),Minimum Air Temperature Since Midnight(degree Celsius)
2026,5,3,20,30,,UTC+8,HK Observatory,29.6,23.4
"""


class HkoParserTests(unittest.TestCase):
    def test_parse_since_midnight_hk_observatory_row(self):
        obs = parse_since_midnight_csv(SINCE_MIDNIGHT_CSV)
        self.assertEqual(obs.station, "HK Observatory")
        self.assertEqual(obs.observed_at_hkt.isoformat(), "2026-05-03T20:30:00+08:00")
        self.assertEqual(obs.since_midnight_max_c, 29.6)
        self.assertEqual(obs.since_midnight_min_c, 23.4)

    def test_parse_fnd_forecast_maxtemp(self):
        payload = {
            "updateTime": "2026-05-03T20:15:00+08:00",
            "weatherForecast": [
                {
                    "forecastDate": "20260504",
                    "week": "Monday",
                    "forecastWind": "North to northeast force 4 to 5.",
                    "forecastWeather": "Mainly cloudy.",
                    "forecastMaxtemp": {"value": 25, "unit": "C"},
                    "forecastMintemp": {"value": 21, "unit": "C"},
                    "PSR": "Medium",
                }
            ],
        }
        rows = parse_fnd_forecasts(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].forecast_date_hkt.isoformat(), "2026-05-04")
        self.assertEqual(rows[0].forecast_max_c, 25)
        self.assertEqual(rows[0].source_type, "fnd")

    def test_parse_flw_between_pattern(self):
        payload = {
            "updateTime": "2026-05-03T20:10:00+08:00",
            "forecastDesc": "Temperatures will range between 21 and 25 degrees. Moderate winds.",
        }
        row = parse_flw_forecast(payload)
        self.assertEqual(row.forecast_min_c, 21)
        self.assertEqual(row.forecast_max_c, 25)
        self.assertFalse(row.parse_warning)

    def test_parse_flw_warns_when_range_missing(self):
        row = parse_flw_forecast({"forecastDesc": "No range here."})
        self.assertIsNone(row.forecast_max_c)
        self.assertTrue(row.parse_warning)


if __name__ == "__main__":
    unittest.main()
