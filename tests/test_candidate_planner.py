import unittest

from whenitrains.candidate_planner import (
    ActualCrossEvent,
    ActualCrossTokenSet,
    executable_candidate_actions,
    plan_actual_cross_actions,
)


class CandidatePlannerTests(unittest.TestCase):
    def test_actual_cross_surprise_fans_out_ordered_actions(self):
        event = ActualCrossEvent(
            event_key="actual_cross:2026-05-08:max:25.6:26.1",
            target_date_hkt="2026-05-08",
            kind="max",
            old_value=25.6,
            new_value=26.1,
        )
        tokens = ActualCrossTokenSet(
            crossed_bucket_yes_token_id="yes26",
            invalidated_yes_position_token_ids=("yes25",),
            invalidated_bucket_no_token_ids=("no25",),
        )

        actions = plan_actual_cross_actions(event, tokens)

        self.assertEqual(
            [(action.intent, action.token_id, action.side) for action in actions],
            [
                ("sell_invalidated_position", "yes25", "SELL"),
                ("buy_crossed_bucket_yes", "yes26", "BUY_YES"),
                ("buy_invalidated_bucket_no", "no25", "BUY_NO"),
            ],
        )
        self.assertEqual(
            [action.candidate_key for action in actions],
            [
                "actual_cross:2026-05-08:max:25.6:26.1:sell_invalidated_position:yes25",
                "actual_cross:2026-05-08:max:25.6:26.1:buy_crossed_bucket_yes:yes26",
                "actual_cross:2026-05-08:max:25.6:26.1:buy_invalidated_bucket_no:no25",
            ],
        )
        self.assertEqual(actions[0].conflict_keys, frozenset({"token:yes25", "position:yes25"}))
        self.assertIn("risk:entry_budget", actions[1].conflict_keys)
        self.assertIn("risk:entry_budget", actions[2].conflict_keys)

    def test_planned_actions_convert_to_execution_scheduler_actions(self):
        event = ActualCrossEvent(
            event_key="actual_cross:2026-05-08:max:25.6:26.1",
            target_date_hkt="2026-05-08",
            kind="max",
            old_value=25.6,
            new_value=26.1,
        )
        planned = plan_actual_cross_actions(
            event,
            ActualCrossTokenSet(
                crossed_bucket_yes_token_id="yes26",
                invalidated_yes_position_token_ids=("yes25",),
            ),
        )
        calls = []

        actions = executable_candidate_actions(
            planned,
            executor=lambda action: calls.append(action.candidate_key) or action.intent,
        )
        results = [action.run() for action in actions]

        self.assertEqual([action.key for action in actions], [item.candidate_key for item in planned])
        self.assertEqual([action.conflict_keys for action in actions], [item.conflict_keys for item in planned])
        self.assertEqual(results, ["sell_invalidated_position", "buy_crossed_bucket_yes"])
        self.assertEqual(calls, [item.candidate_key for item in planned])


if __name__ == "__main__":
    unittest.main()
