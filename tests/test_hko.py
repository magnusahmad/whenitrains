import unittest
from unittest.mock import patch

from whenitrains.hko import (
    fetch_response,
    parse_aws_gis_current_temperature,
    parse_flw_page,
    parse_flw_page_data_json,
    parse_http_datetime_hkt,
    parse_ocf_station_json,
    parse_rhrread_temperature_json,
    parse_since_midnight_csv,
)


SINCE_MIDNIGHT_CSV = """\ufeffDate time (Year),Date time (Month),Date time (Day),Date time (Hour),Date time (Minute),Date time (Second),Date time (Time Zone),Automatic Weather Station,Maximum Air Temperature Since Midnight(degree Celsius),Minimum Air Temperature Since Midnight(degree Celsius)
2026,5,3,20,30,,UTC+8,HK Observatory,29.6,23.4
"""


class HkoParserTests(unittest.TestCase):
    def test_fetch_response_records_fetch_and_payload_timing(self):
        class FakeResponse:
            headers = {"Date": "Mon, 11 May 2026 00:00:00 GMT"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b"payload"

        with patch("whenitrains.hko.urlopen", return_value=FakeResponse()) as urlopen, patch(
            "whenitrains.hko.time.perf_counter", side_effect=[10.0, 10.125]
        ):
            response = fetch_response("https://example.test/data")

        self.assertEqual(response.text, "payload")
        self.assertEqual(response.headers["Date"], "Mon, 11 May 2026 00:00:00 GMT")
        self.assertIsNotNone(response.fetch_started_at_utc)
        self.assertIsNotNone(response.headers_received_at_utc)
        self.assertIsNotNone(response.payload_received_at_utc)
        self.assertAlmostEqual(response.response_elapsed_ms, 125.0)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 15)

    def test_parse_since_midnight_hk_observatory_row(self):
        obs = parse_since_midnight_csv(SINCE_MIDNIGHT_CSV)
        self.assertEqual(obs.station, "HK Observatory")
        self.assertEqual(obs.observed_at_hkt.isoformat(), "2026-05-03T20:30:00+08:00")
        self.assertEqual(obs.since_midnight_max_c, 29.6)
        self.assertEqual(obs.since_midnight_min_c, 23.4)

    def test_parse_rhrread_hk_observatory_temperature(self):
        payload = """
        {
          "updateTime": "2026-05-05T17:02:00+08:00",
          "temperature": {
            "data": [
              {"place": "King's Park", "value": 21, "unit": "C"},
              {"place": "Hong Kong Observatory", "value": 21.4, "unit": "C"}
            ]
          }
        }
        """
        obs = parse_rhrread_temperature_json(payload)
        self.assertEqual(obs.station, "Hong Kong Observatory")
        self.assertEqual(obs.observed_at_hkt.isoformat(), "2026-05-05T17:02:00+08:00")
        self.assertEqual(obs.temperature_c, 21.4)

    def test_parse_aws_gis_hko_decimal_temperature(self):
        payload = """Latest readings recorded at 14:30 Hong Kong Time 7 May 2026
STN,WINDDIRECTION,WINDSPEED,GUST,TEMP,RH,MAXTEMP,MINTEMP,GRASSTEMP,GRASSMINTEMP,VISIBILITY,PRESSURE,TEMPDIFFERENCE,HEATINDEX,
HKO,,,,28.9,69,29.3,24.0,,,,1011.0,4.8,27.3,
SHA,9999,5,12,29.0,68,30.1,23.2,,,,1010.6,5.1,26.1,
"""
        obs = parse_aws_gis_current_temperature(payload)

        self.assertEqual(obs.station, "HKO")
        self.assertEqual(obs.observed_at_hkt.isoformat(), "2026-05-07T14:30:00+08:00")
        self.assertEqual(obs.temperature_c, 28.9)
        self.assertEqual(obs.since_midnight_max_c, 29.3)
        self.assertEqual(obs.since_midnight_min_c, 24.0)

    def test_parse_aws_gis_midnight_extremes_are_previous_day(self):
        payload = """Latest readings recorded at 00:00 Hong Kong Time 10 May 2026
STN,WINDDIRECTION,WINDSPEED,GUST,TEMP,RH,MAXTEMP,MINTEMP,GRASSTEMP,GRASSMINTEMP,VISIBILITY,PRESSURE,TEMPDIFFERENCE,HEATINDEX,
HKO,,,,23.8,82,26.1,23.0,,,,1015.3,-2.3,21.7,
"""
        obs = parse_aws_gis_current_temperature(payload)

        self.assertEqual(obs.station, "HKO")
        self.assertEqual(obs.observed_at_hkt.isoformat(), "2026-05-10T00:00:00+08:00")
        self.assertEqual(obs.temperature_c, 23.8)
        self.assertIsNone(obs.since_midnight_max_c)
        self.assertIsNone(obs.since_midnight_min_c)

    def test_parse_flw_webpage_bulletin_time_and_range(self):
        html = """
        <html><body>
        <p><em>Bulletin updated at 00:45 HKT 04/May/2026</em></p>
        <p>Mainly cloudy with temperatures ranging between 21 and 25 degrees.</p>
        </body></html>
        """
        row = parse_flw_page(html)
        self.assertEqual(row.source_type, "flw_page")
        self.assertEqual(row.update_time, "2026-05-04T00:45:00+08:00")
        self.assertIsNone(row.forecast_min_c)
        self.assertEqual(row.forecast_max_c, 25)
        self.assertFalse(row.parse_warning)

    def test_parse_flw_page_warns_when_range_missing(self):
        row = parse_flw_page("<p>Bulletin updated at 00:45 HKT 04/May/2026</p>")
        self.assertIsNone(row.forecast_max_c)
        self.assertTrue(row.parse_warning)

    def test_parse_flw_page_data_json_builds_rendered_bulletin(self):
        payload = """
        {
          "DYN_DAT_MINDS_FLW": {
            "BulletinTime": {"Val_Eng": "0045"},
            "BulletinDate": {"Val_Eng": "20260504"},
            "FLW_WxForecastGeneralSituation": {"Val_Eng": "General situation."},
            "FLW_WxForecastPeriod": {"Val_Eng": "Weather forecast for Hong Kong"},
            "FLW_WxForecastWxDesc": {"Val_Eng": "Temperatures ranging between 21 and 25 degrees."},
            "FLW_WxOutlookTitle": {"Val_Eng": "Outlook"},
            "FLW_WxOutlookContent": {"Val_Eng": "Brighter later."}
          }
        }
        """
        row = parse_flw_page_data_json(payload)
        self.assertEqual(row.source_type, "flw_page")
        self.assertEqual(row.update_time, "2026-05-04T00:45:00+08:00")
        self.assertIsNone(row.forecast_min_c)
        self.assertEqual(row.forecast_max_c, 25)
        self.assertFalse(row.parse_warning)

    def test_parse_ocf_station_json_daily_and_hourly_forecasts(self):
        payload = """
        {
          "LastModified": 20260504131147,
          "StationCode": "HKO",
          "ModelTime": 2026050312,
          "DailyForecast": [
            {
              "ForecastDate": "20260504",
              "ForecastChanceOfRain": "80%",
              "ForecastDailyWeather": 60,
              "ForecastMaximumTemperature": 27.1,
              "ForecastMinimumTemperature": 21.9
            },
            {
              "ForecastDate": "20260505",
              "ForecastChanceOfRain": "80%",
              "ForecastDailyWeather": 62,
              "ForecastMaximumTemperature": 24.0,
              "ForecastMinimumTemperature": 21.0
            }
          ],
          "HourlyWeatherForecast": [
            {"ForecastHour": "2026050413", "ForecastTemperature": 27.1},
            {"ForecastHour": "2026050414", "ForecastTemperature": 26.9},
            {"ForecastHour": "2026050500", "ForecastTemperature": 23.5}
          ]
        }
        """
        forecasts, samples = parse_ocf_station_json(payload)

        self.assertEqual(len(forecasts), 2)
        self.assertEqual(forecasts[0].source_type, "ocf_station")
        self.assertEqual(forecasts[0].forecast_date_hkt.isoformat(), "2026-05-04")
        self.assertEqual(forecasts[0].forecast_min_c, 22)
        self.assertEqual(forecasts[0].forecast_max_c, 27)
        self.assertEqual(forecasts[0].update_time, "2026-05-04T13:11:47+08:00")
        self.assertEqual(forecasts[0].psr, "80%")
        self.assertFalse(forecasts[0].parse_warning)
        self.assertEqual(samples[0].raw_max_c, 27.1)
        self.assertEqual(len(samples[0].hourly_temperatures), 2)
        self.assertEqual(samples[1].forecast_max_c, 24)

    def test_parse_http_datetime_header_to_hkt(self):
        parsed = parse_http_datetime_hkt("Mon, 04 May 2026 05:12:19 GMT")

        self.assertEqual(parsed.isoformat(), "2026-05-04T13:12:19+08:00")


if __name__ == "__main__":
    unittest.main()
