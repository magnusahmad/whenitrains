import unittest
import tempfile
from datetime import datetime, time, timedelta
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
    def test_regular_forecast_window_is_every_ten_minutes_for_ten_seconds(self):
        before = datetime(2026, 5, 4, 0, 9, 59, tzinfo=HKT)
        start = datetime(2026, 5, 4, 0, 10, 0, tzinfo=HKT)
        end = datetime(2026, 5, 4, 0, 10, 10, tzinfo=HKT)
        after = datetime(2026, 5, 4, 0, 10, 11, tzinfo=HKT)

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

    def test_since_midnight_is_due_across_full_hkt_day(self):
        self.assertIn(
            "since_midnight",
            _due_sources(datetime(2026, 5, 4, 0, 0, 30, tzinfo=HKT)),
        )
        self.assertIn(
            "since_midnight",
            _due_sources(datetime(2026, 5, 4, 20, 8, 0, tzinfo=HKT)),
        )
        self.assertIn(
            "since_midnight",
            _due_sources(datetime(2026, 5, 4, 23, 58, 30, tzinfo=HKT)),
        )

    def test_learned_forecast_minute_is_reused_every_hour_with_catchup(self):
        learned = [time(13, 12)]
        before = datetime(2026, 5, 4, 15, 11, 59, tzinfo=HKT)
        start = datetime(2026, 5, 4, 15, 12, 0, tzinfo=HKT)
        catchup = datetime(2026, 5, 4, 15, 42, 0, tzinfo=HKT)
        after = datetime(2026, 5, 4, 16, 2, 1, tzinfo=HKT)

        state = SchedulerState()
        self.assertNotIn("bulletin", {item.source for item in due_hko_sources(before, state, learned)})
        self.assertIn("bulletin", {item.source for item in due_hko_sources(start, state, learned)})
        self.assertIn("bulletin", {item.source for item in due_hko_sources(catchup, state, learned)})
        self.assertNotIn("bulletin", {item.source for item in due_hko_sources(after, state, learned)})

    def test_content_change_marks_window_complete(self):
        now = datetime(2026, 5, 4, 0, 10, 0, tzinfo=HKT)
        state = SchedulerState(last_hashes={"bulletin": "old"})
        plan = [item for item in due_hko_sources(now, state) if item.source == "bulletin"][0]

        changed = mark_source_fetch(state, plan, "new payload", now, changed=True)

        self.assertTrue(changed)
        self.assertNotIn(
            "bulletin",
            {item.source for item in due_hko_sources(now + timedelta(seconds=10), state)},
        )

    def test_unchanged_source_respects_ten_second_window_cadence(self):
        now = datetime(2026, 5, 4, 0, 10, 0, tzinfo=HKT)
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
        now = datetime(2026, 5, 4, 12, 5, tzinfo=HKT)
        state = SchedulerState()
        actions = scheduler_actions(now, state)
        self.assertTrue(actions.discover_market)
        self.assertTrue(actions.fetch_orderbooks)
        self.assertTrue(actions.fetch_current_temperature)

        state.last_market_discovery_at = now
        state.last_orderbook_fetch_at = now
        state.last_current_temperature_fetch_at = now
        actions = scheduler_actions(now + timedelta(seconds=10), state)
        self.assertFalse(actions.discover_market)
        self.assertFalse(actions.fetch_orderbooks)
        self.assertFalse(actions.fetch_current_temperature)

    def test_current_temperature_collection_can_run_with_orderbook_work(self):
        now = datetime(2026, 5, 4, 12, 5, 0, tzinfo=HKT)
        state = SchedulerState(
            last_market_discovery_at=now,
            last_orderbook_fetch_at=now - timedelta(seconds=15),
            last_current_temperature_fetch_at=now - timedelta(seconds=600),
        )

        actions = scheduler_actions(now, state)

        self.assertFalse(actions.discover_market)
        self.assertTrue(actions.fetch_orderbooks)
        self.assertTrue(actions.fetch_current_temperature)

    def test_current_temperature_waits_during_hko_source_window(self):
        now = datetime(2026, 5, 4, 12, 9, 0, tzinfo=HKT)
        state = SchedulerState(
            last_market_discovery_at=now,
            last_orderbook_fetch_at=now,
            last_current_temperature_fetch_at=now - timedelta(seconds=600),
        )

        actions = scheduler_actions(now, state)

        self.assertTrue(actions.fetch_since_midnight)
        self.assertFalse(actions.fetch_current_temperature)

    def test_current_temperature_not_due_before_ten_minutes(self):
        now = datetime(2026, 5, 4, 12, 5, 0, tzinfo=HKT)
        state = SchedulerState(
            last_market_discovery_at=now,
            last_orderbook_fetch_at=now,
            last_current_temperature_fetch_at=now - timedelta(seconds=599),
        )

        actions = scheduler_actions(now, state)

        self.assertFalse(actions.fetch_current_temperature)

    def test_quiet_scheduler_suppresses_orderbook_only_noop_tick(self):
        result = RunnerResult(notes=("forecast high unchanged", "observed max unchanged"))
        self.assertFalse(
            should_print_scheduled_tick(["discovered market", "fetched orderbooks"], result, quiet=True)
        )
        self.assertTrue(
            should_print_scheduled_tick(["discovered market", "fetched orderbooks"], result, quiet=False)
        )

    def test_quiet_scheduler_prints_hko_fetches_and_trades(self):
        noop = RunnerResult()
        trade = RunnerResult(buys_filled=1)
        self.assertTrue(
            should_print_scheduled_tick(["forecast changed"], noop, quiet=True)
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

    def test_scheduler_logs_fetch_error_and_keeps_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            output = StringIO()
            now = datetime(2026, 5, 4, 10, 9, 0, tzinfo=HKT)
            with redirect_stdout(output):
                run_scheduled_paper_loop(
                    db,
                    fetch_since_midnight=lambda: (_ for _ in ()).throw(
                        OSError("dns failed")
                    ),
                    fetch_bulletin=lambda: "",
                    discover_market=lambda target: None,
                    fetch_orderbooks=lambda target: None,
                    max_ticks=1,
                    now_fn=lambda: now,
                )

            text = output.getvalue()
            self.assertIn("paper-scheduler started", text)
            self.assertIn("since_midnight fetch failed: OSError: dns failed", text)


def _due_sources(now):
    return {item.source for item in due_hko_sources(now, SchedulerState())}


if __name__ == "__main__":
    unittest.main()
