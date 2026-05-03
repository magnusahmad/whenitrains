import unittest
from datetime import date

from whenitrains.markets import PredicateType, parse_outcome_label, predicate_matches
from whenitrains.polymarket import parse_event_markets
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


if __name__ == "__main__":
    unittest.main()
