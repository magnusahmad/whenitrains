import unittest
import tempfile
import threading
from datetime import datetime, time, timedelta
from io import StringIO
from pathlib import Path
from contextlib import redirect_stdout

from whenitrains.hko import HKT
from whenitrains.runner import RunnerResult
from whenitrains.scheduler import (
    SchedulerState,
    _aws_actual_payload_observed_at,
    _is_stale_aws_actual_payload,
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

    def test_aws_actual_window_is_every_five_minutes_with_thirty_second_catchup(self):
        before = datetime(2026, 5, 4, 0, 4, 29, tzinfo=HKT)
        early = datetime(2026, 5, 4, 0, 4, 30, tzinfo=HKT)
        start = datetime(2026, 5, 4, 0, 5, 0, tzinfo=HKT)
        end = datetime(2026, 5, 4, 0, 5, 30, tzinfo=HKT)
        after = datetime(2026, 5, 4, 0, 5, 31, tzinfo=HKT)

        self.assertNotIn("aws_actual", _due_sources(before))
        self.assertIn("aws_actual", _due_sources(early))
        self.assertIn("aws_actual", _due_sources(start))
        self.assertIn("aws_actual", _due_sources(end))
        self.assertNotIn("aws_actual", _due_sources(after))

    def test_learned_aws_actual_fetchable_time_gets_two_minute_poll_window(self):
        learned = [time(14, 36)]
        state = SchedulerState()

        self.assertNotIn(
            time(14, 36),
            _due_aws_schedules(
                    datetime(2026, 5, 4, 14, 33, 59, tzinfo=HKT),
                state,
                learned,
            ),
        )
        self.assertIn(
            time(14, 36),
            _due_aws_schedules(
                    datetime(2026, 5, 4, 14, 34, 0, tzinfo=HKT),
                state,
                learned,
            ),
        )
        self.assertIn(
            time(14, 36),
            _due_aws_schedules(
                    datetime(2026, 5, 4, 14, 38, 0, tzinfo=HKT),
                state,
                learned,
            ),
        )
        self.assertNotIn(
            time(14, 36),
            _due_aws_schedules(
                    datetime(2026, 5, 4, 14, 38, 1, tzinfo=HKT),
                state,
                learned,
            ),
        )

    def test_learned_aws_actual_fetchable_minute_repeats_every_ten_minutes(self):
        learned = [time(19, 58)]
        state = SchedulerState()

        self.assertIn(
            time(20, 8),
            _due_aws_schedules(
                datetime(2026, 5, 4, 20, 6, 0, tzinfo=HKT),
                state,
                learned,
            ),
        )
        self.assertIn(
            time(20, 8),
            _due_aws_schedules(
                datetime(2026, 5, 4, 20, 10, 0, tzinfo=HKT),
                state,
                learned,
            ),
        )
        self.assertNotIn(
            time(20, 8),
            _due_aws_schedules(
                datetime(2026, 5, 4, 20, 10, 1, tzinfo=HKT),
                state,
                learned,
            ),
        )

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

    def test_aws_actual_changed_payload_does_not_complete_poll_window(self):
        now = datetime(2026, 5, 4, 12, 4, 30, tzinfo=HKT)
        state = SchedulerState()
        plan = [item for item in due_hko_sources(now, state) if item.source == "aws_actual"][0]

        changed = mark_source_fetch(state, plan, "previous reading", now, changed=True)

        self.assertTrue(changed)
        self.assertIn(
            "aws_actual",
            {item.source for item in due_hko_sources(now + timedelta(seconds=10), state)},
        )
        self.assertIn(
            "aws_actual",
            {item.source for item in due_hko_sources(now + timedelta(seconds=30), state)},
        )
        self.assertNotIn(
            "aws_actual",
            {item.source for item in due_hko_sources(now + timedelta(seconds=61), state)},
        )

    def test_orderbooks_and_market_discovery_have_separate_cadence(self):
        now = datetime(2026, 5, 4, 12, 5, tzinfo=HKT)
        state = SchedulerState()
        actions = scheduler_actions(now, state, learned_actual_times=[time(12, 9)])
        self.assertTrue(actions.discover_market)
        self.assertTrue(actions.fetch_orderbooks)
        self.assertTrue(actions.fetch_current_temperature)

        state.last_market_discovery_at = now
        state.last_orderbook_fetch_at = now
        plan = [item for item in due_hko_sources(now, state) if item.source == "aws_actual"][0]
        mark_source_fetch(state, plan, "new actual", now, changed=True)
        actions = scheduler_actions(now + timedelta(seconds=10), state)
        self.assertFalse(actions.discover_market)
        self.assertFalse(actions.fetch_orderbooks)
        self.assertTrue(actions.fetch_current_temperature)

    def test_current_temperature_collection_can_run_with_orderbook_work(self):
        now = datetime(2026, 5, 4, 12, 5, 0, tzinfo=HKT)
        state = SchedulerState(
            last_market_discovery_at=now,
            last_orderbook_fetch_at=now - timedelta(seconds=15),
            last_current_temperature_fetch_at=now - timedelta(seconds=600),
        )

        actions = scheduler_actions(now, state, learned_actual_times=[time(12, 9)])

        self.assertFalse(actions.discover_market)
        self.assertTrue(actions.fetch_orderbooks)
        self.assertTrue(actions.fetch_current_temperature)

    def test_current_temperature_collection_can_run_during_hko_source_window(self):
        now = datetime(2026, 5, 4, 12, 9, 0, tzinfo=HKT)
        state = SchedulerState(
            last_market_discovery_at=now,
            last_orderbook_fetch_at=now,
            last_current_temperature_fetch_at=now - timedelta(seconds=600),
        )

        actions = scheduler_actions(now, state, learned_actual_times=[time(12, 9)])

        self.assertTrue(actions.fetch_since_midnight)
        self.assertTrue(actions.fetch_current_temperature)

    def test_current_temperature_not_due_outside_aws_actual_windows(self):
        now = datetime(2026, 5, 4, 12, 5, 31, tzinfo=HKT)
        state = SchedulerState(
            last_market_discovery_at=now,
            last_orderbook_fetch_at=now,
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
            should_print_scheduled_tick(
                ["aws_actual changed: Latest readings recorded at 14:30 Hong Kong Time 7 May 2026"],
                noop,
                quiet=True,
            )
        )
        self.assertTrue(
            should_print_scheduled_tick(["fetched orderbooks"], trade, quiet=True)
        )

    def test_aws_actual_payload_staleness_is_monotonic(self):
        payload_1110 = """Latest readings recorded at 11:10 Hong Kong Time 8 May 2026
STN,TEMP,MAXTEMP,MINTEMP
HKO,27.1,28.4,24.0
"""
        payload_1120 = """Latest readings recorded at 11:20 Hong Kong Time 8 May 2026
STN,TEMP,MAXTEMP,MINTEMP
HKO,27.3,28.5,24.0
"""
        latest = _aws_actual_payload_observed_at(payload_1120)

        self.assertTrue(_is_stale_aws_actual_payload(payload_1110, latest))
        self.assertFalse(_is_stale_aws_actual_payload(payload_1120, latest))

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

    def test_scheduler_uses_custom_output_label(self):
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
                    max_ticks=1,
                    quiet=False,
                    output_label="live-scheduler",
                )
            text = output.getvalue()
            self.assertIn("live-scheduler started", text)
            self.assertIn("live-scheduler actions=", text)
            self.assertNotIn("paper-scheduler started", text)

    def test_scheduler_skips_trading_on_startup_warmup_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            calls = []
            output = StringIO()
            now = datetime(2026, 5, 4, 12, 0, 0, tzinfo=HKT)

            def tick_fn(_db, today_hkt):
                calls.append(today_hkt)
                return RunnerResult(buys_filled=1)

            with redirect_stdout(output):
                run_scheduled_paper_loop(
                    db,
                    fetch_since_midnight=lambda: "",
                    fetch_bulletin=lambda: "",
                    discover_market=lambda target: None,
                    fetch_orderbooks=lambda target: None,
                    run_tick_fn=tick_fn,
                    max_ticks=2,
                    now_fn=lambda: now,
                    quiet=False,
                    base_sleep_seconds=0,
                )

            self.assertEqual(calls, [now.date()])
            text = output.getvalue()
            self.assertIn("startup warmup: trading skipped", text)
            self.assertIn("buys=1/0", text)

    def test_scheduler_does_not_warm_up_until_startup_fetches_succeed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            calls = []
            output = StringIO()
            now = datetime(2026, 5, 4, 12, 0, 0, tzinfo=HKT)

            with redirect_stdout(output):
                run_scheduled_paper_loop(
                    db,
                    fetch_since_midnight=lambda: "",
                    fetch_bulletin=lambda: "",
                    discover_market=lambda target: None,
                    fetch_orderbooks=lambda target: (_ for _ in ()).throw(
                        OSError("orderbooks unavailable")
                    ),
                    run_tick_fn=lambda _db, today_hkt: calls.append(today_hkt) or RunnerResult(buys_filled=1),
                    max_ticks=2,
                    now_fn=lambda: now,
                    quiet=False,
                    base_sleep_seconds=0,
                )

            self.assertEqual(calls, [])
            text = output.getvalue()
            self.assertIn("orderbooks failed: OSError: orderbooks unavailable", text)
            self.assertIn("startup warmup blocked: data fetch failed", text)

    def test_scheduler_skips_decisions_when_due_orderbook_refresh_fails_after_warmup(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            calls = []
            output = StringIO()
            times = iter(
                [
                    datetime(2026, 5, 4, 12, 0, 0, tzinfo=HKT),
                    datetime(2026, 5, 4, 12, 0, 16, tzinfo=HKT),
                ]
            )
            orderbook_calls = 0

            def fetch_orderbooks(_target):
                nonlocal orderbook_calls
                orderbook_calls += 1
                if orderbook_calls == 2:
                    raise OSError("orderbooks unavailable")

            with redirect_stdout(output):
                run_scheduled_paper_loop(
                    db,
                    fetch_since_midnight=lambda: "",
                    fetch_bulletin=lambda: "",
                    discover_market=lambda target: None,
                    fetch_orderbooks=fetch_orderbooks,
                    run_tick_fn=lambda _db, today_hkt: calls.append(today_hkt) or RunnerResult(buys_filled=1),
                    max_ticks=2,
                    now_fn=lambda: next(times),
                    quiet=False,
                    base_sleep_seconds=0,
                )

            self.assertEqual(calls, [])
            text = output.getvalue()
            self.assertIn("startup warmup: trading skipped", text)
            self.assertIn("decisions skipped: data fetch failed", text)

    def test_scheduler_startup_warmup_fetches_actual_when_background_poller_is_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            calls = []
            actual_fetches = []
            output = StringIO()
            now = datetime(2026, 5, 4, 12, 0, 0, tzinfo=HKT)

            with redirect_stdout(output):
                run_scheduled_paper_loop(
                    db,
                    fetch_since_midnight=lambda: "",
                    fetch_bulletin=lambda: "",
                    discover_market=lambda target: None,
                    fetch_orderbooks=lambda target: None,
                    run_tick_fn=lambda _db, today_hkt: calls.append(today_hkt) or RunnerResult(buys_filled=1),
                    aws_actual_poll_fetch=lambda: actual_fetches.append("fetch") or "actual payload",
                    max_ticks=2,
                    now_fn=lambda: now,
                    quiet=False,
                    base_sleep_seconds=0,
                )

            self.assertGreaterEqual(len(actual_fetches), 1)
            self.assertEqual(calls, [now.date()])

    def test_scheduler_stops_when_stop_event_is_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            stop_event = threading.Event()
            output = StringIO()

            def tick_once(_db, today_hkt):
                stop_event.set()
                return RunnerResult()

            with redirect_stdout(output):
                run_scheduled_paper_loop(
                    db,
                    fetch_since_midnight=lambda: "",
                    fetch_bulletin=lambda: "",
                    discover_market=lambda target: None,
                    fetch_orderbooks=lambda target: None,
                    run_tick_fn=tick_once,
                    stop_event=stop_event,
                    output_label="live-scheduler",
                )
            text = output.getvalue()
            self.assertIn("live-scheduler started", text)
            self.assertIn("live-scheduler stopped", text)

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

    def test_quiet_scheduler_logs_current_temperature_fetch_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            output = StringIO()
            now = datetime(2026, 5, 4, 10, 5, 0, tzinfo=HKT)
            with redirect_stdout(output):
                run_scheduled_paper_loop(
                    db,
                    fetch_since_midnight=lambda: "",
                    fetch_bulletin=lambda: "",
                    discover_market=lambda target: None,
                    fetch_orderbooks=lambda target: None,
                    fetch_current_temperature=lambda: (_ for _ in ()).throw(
                        OSError("aws unavailable")
                    ),
                    max_ticks=1,
                    now_fn=lambda: now,
                    quiet=True,
                )

            text = output.getvalue()
            self.assertIn("paper-scheduler started", text)
            self.assertIn("aws_actual fetch failed: OSError: aws unavailable", text)


def _due_sources(now):
    return {item.source for item in due_hko_sources(now, SchedulerState())}


def _due_aws_schedules(now, state, learned_actuals):
    return {
        item.scheduled_at.time()
        for item in due_hko_sources(now, state, learned_actual_times=learned_actuals)
        if item.source == "aws_actual"
    }


if __name__ == "__main__":
    unittest.main()
