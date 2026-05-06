import unittest
from datetime import date

from whenitrains.markets import PredicateType, parse_outcome_label, predicate_matches
from whenitrains.polymarket import (
    event_slug_for_date,
    event_slugs_for_date,
    parse_event_markets,
    resolution_rules_match_expected,
    temperature_market_kind,
)
from whenitrains.polymarket import is_current_day_market


class MarketSemanticsTests(unittest.TestCase):
    def test_exact_bucket_does_not_round(self):
        predicate = parse_outcome_label("29°C")
        self.assertEqual(predicate.type, PredicateType.EXACT_C)
        self.assertTrue(predicate_matches(predicate, 29.0))
        self.assertTrue(predicate_matches(predicate, 29.9))
        self.assertFalse(predicate_matches(predicate, 30.0))

    def test_top_boundary_bucket(self):
        predicate = parse_outcome_label("26°C or higher")
        self.assertEqual(predicate.type, PredicateType.GTE_C)
        self.assertTrue(predicate_matches(predicate, 26.0))
        self.assertTrue(predicate_matches(predicate, 31.2))
        self.assertFalse(predicate_matches(predicate, 25.9))

    def test_bottom_boundary_bucket(self):
        predicate = parse_outcome_label("16°C or below")
        self.assertEqual(predicate.type, PredicateType.BOTTOM_BUCKET_LTE_C)
        self.assertTrue(predicate_matches(predicate, 16.9))
        self.assertFalse(predicate_matches(predicate, 17.0))

    def test_parse_event_markets_maps_yes_no_tokens(self):
        event = {
            "id": "439958",
            "slug": "highest-temperature-in-hong-kong-on-may-4-2026",
            "title": "Highest temperature in Hong Kong on May 4?",
            "eventDate": "2026-05-04",
            "markets": [
                {
                    "id": "2137348",
                    "slug": "highest-temperature-in-hong-kong-on-may-4-2026-25c",
                    "question": "Will the highest temperature in Hong Kong be 25°C on May 4?",
                    "groupItemTitle": "25°C",
                    "clobTokenIds": '["YES_TOKEN", "NO_TOKEN"]',
                    "outcomes": '["Yes", "No"]',
                    "bestBid": 0.36,
                    "bestAsk": 0.38,
                    "active": True,
                    "closed": False,
                    "acceptingOrders": True,
                }
            ],
        }
        markets = parse_event_markets(event)
        self.assertEqual(markets[0].outcomes[0].yes_token_id, "YES_TOKEN")
        self.assertEqual(markets[0].outcomes[0].no_token_id, "NO_TOKEN")
        self.assertEqual(markets[0].outcomes[0].predicate.value_c, 25)

    def test_current_day_market_filter(self):
        self.assertTrue(is_current_day_market(date(2026, 5, 4), date(2026, 5, 4)))
        self.assertFalse(is_current_day_market(date(2026, 5, 5), date(2026, 5, 4)))

    def test_temperature_event_slugs_include_lowest_market(self):
        target = date(2026, 5, 7)
        self.assertEqual(
            event_slug_for_date(target, "lowest"),
            "lowest-temperature-in-hong-kong-on-may-7-2026",
        )
        self.assertEqual(
            event_slugs_for_date(target),
            [
                "highest-temperature-in-hong-kong-on-may-7-2026",
                "lowest-temperature-in-hong-kong-on-may-7-2026",
            ],
        )
        self.assertEqual(
            temperature_market_kind("lowest-temperature-in-hong-kong-on-may-7-2026"),
            "lowest",
        )

    def test_resolution_rules_match_allows_date_only_change(self):
        text = """
        This market will resolve to the temperature range that contains the highest temperature
        recorded by the Hong Kong Observatory in degrees Celsius on 3 May '26.

        The resolution source for this market will be information from the Hong Kong Observatory,
        specifically the "Absolute Daily Max (deg. C)" the specified date once information is
        finalized in the relevant "Daily Extract", available here:
        https://www.weather.gov.hk/en/cis/climat.htm

        This market can not resolve to "Yes" until data for this date has been finalized.

        The resolution source for this market measures temperatures in Celsius to one decimal
        place (eg, 9.1°C). Thus, this is the level of precision that will be used when resolving
        the market.

        Any revisions to temperatures recorded after data is finalized for this market's timeframe
        will not be considered for this market's resolution.
        """

        self.assertTrue(resolution_rules_match_expected(text))
        self.assertTrue(resolution_rules_match_expected(text.replace("3 May '26", "4 May '26")))

    def test_resolution_rules_mismatch_when_tail_changes(self):
        text = """
        This market will resolve to the temperature range that contains the highest temperature
        recorded by the Hong Kong Observatory in degrees Celsius on 3 May '26.

        The resolution source for this market will be information from the Hong Kong Observatory,
        specifically the "Absolute Daily Max (deg. C)" the specified date once information is
        finalized in the relevant "Daily Extract", available here:
        https://www.weather.gov.hk/en/cis/climat.htm

        This market can resolve before data for this date has been finalized.
        """

        self.assertFalse(resolution_rules_match_expected(text))

    def test_lowest_resolution_rules_match_expected_min_wording(self):
        text = """
        This market will resolve to the temperature range that contains the lowest temperature
        recorded by the Hong Kong Observatory in degrees Celsius on 7 May '26.

        The resolution source for this market will be information from the Hong Kong Observatory,
        specifically the "Absolute Daily Min (deg. C)" the specified date once information is
        finalized in the relevant "Daily Extract", available here:
        https://www.weather.gov.hk/en/cis/climat.htm

        This market can not resolve to "Yes" until data for this date has been finalized.

        The resolution source for this market measures temperatures in Celsius to one decimal
        place (eg, 9.1°C). Thus, this is the level of precision that will be used when resolving
        the market.

        Any revisions to temperatures recorded after data is finalized for this market's timeframe
        will not be considered for this market's resolution.
        """

        self.assertTrue(resolution_rules_match_expected(text, "lowest"))
        self.assertFalse(resolution_rules_match_expected(text, "highest"))


if __name__ == "__main__":
    unittest.main()
