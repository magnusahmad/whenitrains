import unittest

from whenitrains.hko import (
    parse_flw_page,
    parse_flw_page_data_json,
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


if __name__ == "__main__":
    unittest.main()
