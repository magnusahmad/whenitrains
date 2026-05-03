import unittest

from whenitrains.markets import parse_outcome_label
from whenitrains.signals import (
    DirectionalImpact,
    PriceResponse,
    classify_directional_impact,
    classify_price_response,
)


class SignalTests(unittest.TestCase):
    def test_forecast_upgrade_increases_new_bucket(self):
        impact = classify_directional_impact(
            parse_outcome_label("29°C"), old_value=28, new_value=29
        )
        self.assertEqual(impact, DirectionalImpact.INCREASES_YES_PROBABILITY)

    def test_forecast_upgrade_decreases_old_bucket(self):
        impact = classify_directional_impact(
            parse_outcome_label("28°C"), old_value=28, new_value=29
        )
        self.assertEqual(impact, DirectionalImpact.DECREASES_YES_PROBABILITY)

    def test_far_away_longshot_is_no_material_impact(self):
        impact = classify_directional_impact(
            parse_outcome_label("35°C"), old_value=28, new_value=29
        )
        self.assertEqual(impact, DirectionalImpact.NO_MATERIAL_IMPACT)

    def test_price_response_collapses_all_lag_to_not_moved(self):
        self.assertEqual(
            classify_price_response(
                DirectionalImpact.INCREASES_YES_PROBABILITY,
                prior_yes_ask=0.40,
                current_yes_ask=0.405,
                min_move=0.02,
            ),
            PriceResponse.PRICE_NOT_MOVED_WITH_EVENT,
        )
        self.assertEqual(
            classify_price_response(
                DirectionalImpact.INCREASES_YES_PROBABILITY,
                prior_yes_ask=0.40,
                current_yes_ask=0.43,
                min_move=0.02,
            ),
            PriceResponse.PRICE_MOVED_WITH_EVENT,
        )


if __name__ == "__main__":
    unittest.main()
