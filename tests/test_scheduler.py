import unittest
import tempfile
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from contextlib import redirect_stdout

from whenitrains.hko import HKT
from whenitrains.runner import RunnerResult
from whenitrains.scheduler import (
    SchedulerState,
    due_hko_sources,
    mark_source_fetch,
    scheduler_actions,
    run_scheduled_paper_loop,
    should_print_scheduled_tick,
)
from whenitrains.storage import connect, migrate


class SchedulerTests(unittest.TestCase):
    def test_bulletin_window_is_thirty_seconds_before_to_two_minutes_after(self):
        before = datetime(2026, 5, 4, 0, 44, 29, tzinfo=HKT)
        start = datetime(2026, 5, 4, 0, 44, 30, tzinfo=HKT)
        end = datetime(2026, 5, 4, 0, 47, 0, tzinfo=HKT)
        after = datetime(2026, 5, 4, 0, 47, 1, tzinfo=HKT)

        self.assertNotIn("bulletin", _due_sources(before))
        self.assertIn("bulletin", _due_sources(start))
        self.assertIn("bulletin", _due_sources(end))
        self.assertNotIn("bulletin", _due_sources(after))

    def test_since_midnight_window_is_one_minute_before_to_two_minutes_after(self):
        before = datetime(2026, 5, 4, 10, 7, 59, tzinfo=HKT)
        start = datetime(2026, 5, 4, 10, 8, 0, tzinfo=HKT)
        end = datetime(2026, 5, 4, 10, 11, 0, tzinfo=HKT)
        after = datetime(2026, 5, 4, 10, 11, 1, tzinfo=HKT)

        self.assertNotIn("since_midnight", _due_sources(before))
        self.assertIn("since_midnight", _due_sources(start))
        self.assertIn("since_midnight", _due_sources(end))
        self.assertNotIn("since_midnight", _due_sources(after))

    def test_since_midnight_is_not_due_outside_10_to_20_hkt(self):
        self.assertNotIn(
            "since_midnight",
            _due_sources(datetime(2026, 5, 4, 9, 59, 30, tzinfo=HKT)),
        )
        self.assertNotIn(
            "since_midnight",
            _due_sources(datetime(2026, 5, 4, 20, 8, 0, tzinfo=HKT)),
        )

    def test_content_change_marks_window_complete(self):
        now = datetime(2026, 5, 4, 0, 44, 30, tzinfo=HKT)
        state = SchedulerState(last_hashes={"bulletin": "old"})
        plan = [item for item in due_hko_sources(now, state) if item.source == "bulletin"][0]

        changed = mark_source_fetch(state, plan, "new payload", now, changed=True)

        self.assertTrue(changed)
        self.assertEqual(due_hko_sources(now + timedelta(seconds=10), state), [])

    def test_unchanged_source_respects_ten_second_window_cadence(self):
        now = datetime(2026, 5, 4, 0, 44, 30, tzinfo=HKT)
        state = SchedulerState(last_hashes={"bulletin": "same"})
        plan = [item for item in due_hko_sources(now, state) if item.source == "bulletin"][0]

        changed = mark_source_fetch(state, plan, "same", now, changed=False)

        self.assertFalse(changed)
        self.assertNotIn(
            "bulletin",
            {item.source for item in due_hko_sources(now + timedelta(seconds=9), state)},
        )
        self.assertIn(
            "bulletin",
            {item.source for item in due_hko_sources(now + timedelta(seconds=10), state)},
        )

    def test_orderbooks_and_market_discovery_have_separate_cadence(self):
        now = datetime(2026, 5, 4, 12, 0, tzinfo=HKT)
        state = SchedulerState()
        actions = scheduler_actions(now, state)
        self.assertTrue(actions.discover_market)
        self.assertTrue(actions.fetch_orderbooks)

        state.last_market_discovery_at = now
        state.last_orderbook_fetch_at = now
        actions = scheduler_actions(now + timedelta(seconds=10), state)
        self.assertFalse(actions.discover_market)
        self.assertFalse(actions.fetch_orderbooks)

    def test_quiet_scheduler_suppresses_orderbook_only_noop_tick(self):
        result = RunnerResult(notes=("forecast high unchanged", "observed max unchanged"))
        self.assertFalse(
            should_print_scheduled_tick(["discovered market", "fetched orderbooks"], result, quiet=True)
        )

    def test_quiet_scheduler_prints_hko_fetches_and_trades(self):
        noop = RunnerResult()
        trade = RunnerResult(buys_filled=1)
        self.assertTrue(
            should_print_scheduled_tick(["bulletin changed"], noop, quiet=True)
        )
        self.assertTrue(
            should_print_scheduled_tick(["fetched orderbooks"], trade, quiet=True)
        )

    def test_scheduler_prints_startup_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            output = StringIO()
            with redirect_stdout(output):
                run_scheduled_paper_loop(
                    db,
                    fetch_since_midnight=lambda: "",
                    fetch_bulletin=lambda: "",
                    discover_market=lambda target: None,
                    fetch_orderbooks=lambda target: None,
                    max_ticks=0,
                )
            self.assertIn("paper-scheduler started", output.getvalue())


def _due_sources(now):
    return {item.source for item in due_hko_sources(now, SchedulerState())}


if __name__ == "__main__":
    unittest.main()
