import unittest

from whenitrains.engine import build_trade_candidates
from whenitrains.markets import parse_outcome_label
from whenitrains.polymarket import Outcome


class EngineTests(unittest.TestCase):
    def test_builds_buy_yes_candidate_when_forecast_upgrade_not_priced(self):
        outcomes = [
            Outcome(
                market_id="m29",
                label="29°C",
                predicate=parse_outcome_label("29°C"),
                yes_token_id="yes29",
                no_token_id="no29",
            ),
            Outcome(
                market_id="m35",
                label="35°C",
                predicate=parse_outcome_label("35°C"),
                yes_token_id="yes35",
                no_token_id="no35",
            ),
        ]
        candidates = build_trade_candidates(
            outcomes=outcomes,
            old_forecast_max_c=28,
            new_forecast_max_c=29,
            prior_yes_asks={"m29": 0.40, "m35": 0.01},
            current_yes_asks={"m29": 0.405, "m35": 0.01},
            min_move=0.02,
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].outcome.label, "29°C")
        self.assertEqual(candidates[0].side, "BUY_YES")


if __name__ == "__main__":
    unittest.main()
