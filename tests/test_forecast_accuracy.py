import unittest
from datetime import date

from whenitrains.forecast_accuracy import (
    _analysis_timestamps,
    _latest_timestamp_per_day,
    match_forecasts_to_actuals,
    parse_fnd_rss_forecasts,
    parse_hko_daily_max_csv,
    summarize_accuracy,
)


class ForecastAccuracyTests(unittest.TestCase):
    def test_parse_fnd_rss_forecast_max_ranges(self):
        forecasts = parse_fnd_rss_forecasts(_RSS_SAMPLE, "20250505-0202")

        self.assertEqual(forecasts[0].issued_at_hkt.isoformat(), "2025-05-05T00:00:00+08:00")
        self.assertEqual(forecasts[0].target_date, date(2025, 5, 5))
        self.assertEqual(forecasts[0].forecast_max_c, 31.0)
        self.assertEqual(forecasts[1].target_date, date(2025, 5, 6))
        self.assertEqual(forecasts[1].forecast_max_c, 30.0)

    def test_parse_hko_daily_max_csv_skips_footnotes(self):
        actuals = parse_hko_daily_max_csv(_ACTUALS_SAMPLE)

        self.assertEqual(actuals[date(2025, 5, 5)], 32.0)
        self.assertEqual(actuals[date(2025, 5, 6)], 30.4)

    def test_match_uses_latest_snapshot_for_each_lead(self):
        forecasts = parse_fnd_rss_forecasts(_RSS_SAMPLE, "20250505-0202")
        later = parse_fnd_rss_forecasts(
            _RSS_SAMPLE.replace("Bulletin updated at 00:00", "Bulletin updated at 08:00")
            .replace("31 C", "32 C", 1),
            "20250505-0801",
        )
        actuals = {date(2025, 5, 5): 32.0, date(2025, 5, 6): 30.4}

        rows = match_forecasts_to_actuals(
            forecasts + later,
            actuals,
            start=date(2025, 5, 5),
            end=date(2025, 5, 6),
            lead_days=(0, 1),
        )

        same_day = [row for row in rows if row.target_date == date(2025, 5, 5)][0]
        self.assertEqual(same_day.forecast_max_c, 32.0)
        self.assertEqual(same_day.error_c, 0.0)

    def test_summarize_accuracy(self):
        forecasts = parse_fnd_rss_forecasts(_RSS_SAMPLE, "20250505-0202")
        actuals = {date(2025, 5, 5): 32.0, date(2025, 5, 6): 30.4}
        rows = match_forecasts_to_actuals(
            forecasts,
            actuals,
            start=date(2025, 5, 5),
            end=date(2025, 5, 6),
            lead_days=(0, 1),
        )

        summaries = summarize_accuracy(rows, (0, 1))

        self.assertEqual(summaries[0].sample_count, 1)
        self.assertEqual(summaries[0].mean_error_c, 1.0)
        self.assertEqual(summaries[1].sample_count, 1)
        self.assertAlmostEqual(summaries[1].mae_c, 0.4)

    def test_summary_bucket_hit_uses_market_floor_semantics(self):
        forecasts = parse_fnd_rss_forecasts(_RSS_SAMPLE, "20250505-0202")
        actuals = {date(2025, 5, 5): 31.9}
        rows = match_forecasts_to_actuals(
            forecasts,
            actuals,
            start=date(2025, 5, 5),
            end=date(2025, 5, 5),
            lead_days=(0,),
        )

        summaries = summarize_accuracy(rows, (0,))

        self.assertEqual(summaries[0].exact_integer_bucket_rate, 1.0)

    def test_latest_timestamp_per_day_keeps_last_revision(self):
        self.assertEqual(
            _latest_timestamp_per_day(
                ["20260504-0202", "20260504-2002", "20260505-0802"]
            ),
            ["20260504-2002", "20260505-0802"],
        )

    def test_analysis_timestamps_keep_same_day_and_latest_revision(self):
        self.assertEqual(
            _analysis_timestamps(
                [
                    "20260504-0202",
                    "20260504-1402",
                    "20260504-2002",
                    "20260505-0802",
                ]
            ),
            ["20260504-1402", "20260504-2002", "20260505-0802"],
        )


_RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0"><channel><item>
<title>Bulletin updated at 00:00 HKT 05/May/2025</title>
<description><![CDATA[
Date/Month:
05/05 (Monday)<br/>
Temp range:
26 -
31 C<br/>
Date/Month:
06/05 (Tuesday)<br/>
Temp range:
25 -
30 C<br/>
]]></description>
</item></channel></rss>
"""

_ACTUALS_SAMPLE = """﻿"﻿日最高氣溫(攝氏度) - 天文台"
"Daily Maximum Temperature (°C) at the Hong Kong Observatory"
年/Year,月/Month,日/Day,數值/Value,"數據完整性/data Completeness"
2025,5,5,32.0,C
2025,5,6,30.4,C
"*** 沒有數據/unavailable"
"""


if __name__ == "__main__":
    unittest.main()
