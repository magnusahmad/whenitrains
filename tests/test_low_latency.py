import json
import tempfile
import threading
import time
import unittest
from datetime import date, datetime
from pathlib import Path
from sqlite3 import ProgrammingError
from unittest.mock import patch

from whenitrains.hko import HKT, HkoCurrentTemperature, HkoObservation, OcfForecastSample
from whenitrains.low_latency import (
    AlphaEvent,
    FastDecisionWorker,
    LowLatencyEventQueue,
    compact_latency_event_line,
    process_next_fast_event,
)
from whenitrains.storage import (
    connect,
    latency_stages_for_event,
    migrate,
    store_hko_current_temperature,
    store_hko_observation,
    store_ocf_forecast_samples,
    store_orderbook,
    store_polymarket_event,
    store_raw_snapshot,
    store_trading_decision,
)
from whenitrains.markets import parse_outcome_label
from whenitrains.polymarket import OrderBook, Outcome, TemperatureMarket


class LowLatencyReadinessTests(unittest.TestCase):
    def setUp(self):
        self._opened_dbs = []
        original_connect = connect

        def tracked_connect(*args, **kwargs):
            db = original_connect(*args, **kwargs)
            self._opened_dbs.append(db)
            return db

        self._connect_patcher = patch(f"{__name__}.connect", tracked_connect)
        self._connect_patcher.start()
        self.addCleanup(self._connect_patcher.stop)
        self.addCleanup(self._close_opened_dbs)

    def _close_opened_dbs(self):
        for db in reversed(self._opened_dbs):
            try:
                db.close()
            except ProgrammingError:
                pass

    def test_aws_actual_transition_enqueues_latency_stages_after_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            queue = LowLatencyEventQueue()
            clock = _FakeMonotonic([100.0, 100.2, 100.2])

            _store_aws_actual(
                db, high=25.6, minute=40, event_queue=queue, monotonic_fn=clock
            )
            _store_aws_actual(
                db, high=26.1, minute=50, event_queue=queue, monotonic_fn=clock
            )

            event = queue.get_nowait()
            stages = latency_stages_for_event(db, event.event_key)

            self.assertEqual(event.kind, "aws_actual_transition")
            self.assertEqual(event.target_date_hkt, "2026-05-04")
            self.assertEqual(
                [stage["stage"] for stage in stages],
                ["db_committed", "event_detected"],
            )
            self.assertAlmostEqual(stages[0]["monotonic_ts"], 100.2)
            self.assertAlmostEqual(stages[1]["monotonic_ts"], 100.2)

    def test_since_midnight_transition_enqueues_latency_stages_after_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            queue = LowLatencyEventQueue()
            clock = _FakeMonotonic([110.0, 110.2, 110.2])

            _store_since_midnight(
                db,
                high=25.6,
                minute=40,
                event_queue=queue,
                monotonic_fn=clock,
            )
            _store_since_midnight(
                db,
                high=26.1,
                minute=50,
                event_queue=queue,
                monotonic_fn=clock,
            )

            event = queue.get_nowait()
            stages = latency_stages_for_event(db, event.event_key)

            self.assertEqual(event.kind, "aws_actual_transition")
            self.assertEqual(event.target_date_hkt, "2026-05-04")
            self.assertEqual(
                [stage["stage"] for stage in stages],
                ["db_committed", "event_detected"],
            )
            self.assertAlmostEqual(stages[0]["monotonic_ts"], 110.2)
            self.assertAlmostEqual(stages[1]["monotonic_ts"], 110.2)

    def test_forecast_sample_change_enqueues_latency_stages_after_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            queue = LowLatencyEventQueue()
            clock = _FakeMonotonic([300.0, 300.2, 300.2])
            first_snapshot = store_raw_snapshot(db, "hko", "ocf-1", "{}")
            second_snapshot = store_raw_snapshot(db, "hko", "ocf-2", "{}")

            store_ocf_forecast_samples(
                db,
                first_snapshot.id,
                [_ocf_sample(29.1)],
                event_queue=queue,
                monotonic_fn=clock,
            )
            store_ocf_forecast_samples(
                db,
                second_snapshot.id,
                [_ocf_sample(30.2)],
                event_queue=queue,
                monotonic_fn=clock,
            )

            event = queue.get_nowait()
            stages = latency_stages_for_event(db, event.event_key)

            self.assertEqual(event.kind, "forecast_sample_changed")
            self.assertEqual(event.target_date_hkt, "2026-05-04")
            self.assertEqual(event.details["old_raw_max_c"], 29.1)
            self.assertEqual(event.details["new_raw_max_c"], 30.2)
            self.assertEqual(
                [stage["stage"] for stage in stages],
                ["db_committed", "event_detected"],
            )
            self.assertAlmostEqual(stages[0]["monotonic_ts"], 300.2)

    def test_low_latency_queue_coalesces_forecast_events_by_target_date(self):
        queue = LowLatencyEventQueue()
        first = AlphaEvent(
            kind="forecast_sample_changed",
            event_key="forecast_sample_changed:2026-05-04:1->2",
            target_date_hkt="2026-05-04",
            source_row_id=2,
            previous_row_id=1,
            committed_monotonic=100.0,
            detected_monotonic=100.0,
            details={},
        )
        latest = AlphaEvent(
            kind="forecast_sample_changed",
            event_key="forecast_sample_changed:2026-05-04:2->3",
            target_date_hkt="2026-05-04",
            source_row_id=3,
            previous_row_id=2,
            committed_monotonic=101.0,
            detected_monotonic=101.0,
            details={},
        )
        actual = AlphaEvent(
            kind="aws_actual_transition",
            event_key="aws_actual_transition:max:2026-05-04:1:25.6->2:26.1",
            target_date_hkt="2026-05-04",
            source_row_id=2,
            previous_row_id=1,
            committed_monotonic=102.0,
            detected_monotonic=102.0,
            details={},
        )

        queue.put(first)
        queue.put(latest)
        queue.put(actual)

        drained = queue.drain_coalesced()

        self.assertEqual([event.event_key for event in drained], [latest.event_key, actual.event_key])

    def test_fast_worker_starts_decision_under_one_second_after_hko_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            queue = LowLatencyEventQueue()
            commit_clock = _FakeMonotonic([200.0, 200.1, 200.1])
            worker_clock = _FakeMonotonic([200.7, 200.8])
            calls = []

            _store_aws_actual(
                db,
                high=25.6,
                minute=40,
                event_queue=queue,
                monotonic_fn=commit_clock,
            )
            _store_aws_actual(
                db,
                high=26.1,
                minute=50,
                event_queue=queue,
                monotonic_fn=commit_clock,
            )

            result = process_next_fast_event(
                db,
                queue,
                decision_handler=lambda db, target: calls.append(target),
                monotonic_fn=worker_clock,
            )
            stages = latency_stages_for_event(db, result.event_key)
            decision_started = next(
                stage for stage in stages if stage["stage"] == "decision_started"
            )
            db_committed = next(stage for stage in stages if stage["stage"] == "db_committed")

            self.assertEqual(calls, [date(2026, 5, 4)])
            self.assertLess(
                decision_started["monotonic_ts"] - db_committed["monotonic_ts"],
                1.0,
            )
            self.assertIn("decision_completed", [stage["stage"] for stage in stages])

    def test_compact_latency_event_line_includes_event_timing(self):
        event = _alpha_event(
            kind="aws_actual_transition",
            event_key="aws_actual_transition:max:2026-05-04:1:25.6->2:26.1",
        )
        event = type(event)(
            **{
                **event.__dict__,
                "committed_monotonic": 100.0,
                "detected_monotonic": 100.125,
                "details": {"transition": "max"},
            }
        )

        line = compact_latency_event_line(event)

        self.assertIn("latency_event=aws_actual_transition", line)
        self.assertIn("target=2026-05-04", line)
        self.assertIn("commit_to_detect_ms=125.0", line)
        self.assertIn("transition=max", line)

    def test_compact_latency_event_line_covers_forecast_and_resolution_events(self):
        forecast_event = _alpha_event(
            kind="forecast_sample_changed",
            event_key="forecast_sample_changed:2026-05-04:1->2",
            details={"old_raw_max_c": 29.1, "new_raw_max_c": 30.2},
        )
        resolution_event = _alpha_event(
            kind="market_resolution_changed",
            event_key="market_resolution_changed:2026-05-04:1:active->resolved",
            details={"previous_status": "active", "new_status": "resolved"},
        )

        forecast_line = compact_latency_event_line(forecast_event)
        resolution_line = compact_latency_event_line(resolution_event)

        self.assertIn("latency_event=forecast_sample_changed", forecast_line)
        self.assertIn("key=forecast_sample_changed:2026-05-04:1->2", forecast_line)
        self.assertIn("target=2026-05-04", forecast_line)
        self.assertIn("old_raw_max_c=29.1", forecast_line)
        self.assertIn("new_raw_max_c=30.2", forecast_line)
        self.assertIn("latency_event=market_resolution_changed", resolution_line)
        self.assertIn(
            "key=market_resolution_changed:2026-05-04:1:active->resolved",
            resolution_line,
        )
        self.assertIn("target=2026-05-04", resolution_line)
        self.assertIn("previous_status=active", resolution_line)
        self.assertIn("new_status=resolved", resolution_line)

    def test_low_latency_queue_wait_wakes_when_event_arrives(self):
        queue = LowLatencyEventQueue()
        stop_event = threading.Event()

        def producer():
            time.sleep(0.02)
            queue.put(
                _alpha_event(
                    kind="aws_actual_transition",
                    event_key="aws_actual_transition:max:2026-05-04:1:25.6->2:26.1",
                )
            )

        thread = threading.Thread(target=producer)
        started = time.monotonic()
        thread.start()
        self.assertTrue(queue.wait_for_event_or_stop(1.0, stop_event))
        thread.join(timeout=1.0)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(queue.empty())

    def test_fast_worker_dispatches_forecast_sample_events_to_forecast_handler(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            queue = LowLatencyEventQueue()
            queue.put(
                _alpha_event(
                    kind="forecast_sample_changed",
                    event_key="forecast_sample_changed:2026-05-04:1->2",
                )
            )

            with patch("whenitrains.low_latency.process_forecast_entries") as handler:
                handler.return_value = object()
                process_next_fast_event(db, queue, monotonic_fn=_FakeMonotonic([400.0, 400.1]))

            handler.assert_called_once_with(db, date(2026, 5, 4))

    def test_market_resolution_change_enqueues_latency_stages_after_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            queue = LowLatencyEventQueue()
            clock = _FakeMonotonic([500.0, 500.2])

            store_polymarket_event(db, _market(status="active"))
            store_polymarket_event(
                db,
                _market(status="resolved"),
                event_queue=queue,
                monotonic_fn=clock,
            )

            event = queue.get_nowait()
            stages = latency_stages_for_event(db, event.event_key)

            self.assertEqual(event.kind, "market_resolution_changed")
            self.assertEqual(event.target_date_hkt, "2026-05-04")
            self.assertEqual(event.details["previous_status"], "active")
            self.assertEqual(event.details["new_status"], "resolved")
            self.assertEqual(
                [stage["stage"] for stage in stages],
                ["db_committed", "event_detected"],
            )

    def test_fast_worker_dispatches_market_resolution_events_to_exit_handler(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            queue = LowLatencyEventQueue()
            queue.put(
                _alpha_event(
                    kind="market_resolution_changed",
                    event_key="market_resolution_changed:2026-05-04:1:active->resolved",
                )
            )

            with patch("whenitrains.low_latency.process_open_position_exits") as handler:
                handler.return_value = object()
                process_next_fast_event(db, queue, monotonic_fn=_FakeMonotonic([600.0, 600.1]))

            handler.assert_called_once_with(db, date(2026, 5, 4))

    def test_fast_decision_worker_blocks_on_queue_and_processes_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            db.close()
            queue = LowLatencyEventQueue()
            processed = threading.Event()
            calls = []

            def handler(worker_db, target):
                calls.append((worker_db is not db, target))
                return object()

            def callback(_result):
                processed.set()

            worker = FastDecisionWorker(
                db_path=db_path,
                event_queue=queue,
                decision_handler=handler,
                result_callback=callback,
                poll_timeout=0.01,
                monotonic_fn=_FakeMonotonic([700.0, 700.1]),
            )
            worker.start()
            try:
                queue.put(
                    _alpha_event(
                        kind="forecast_sample_changed",
                        event_key="forecast_sample_changed:2026-05-04:1->2",
                    )
                )
                self.assertTrue(processed.wait(timeout=1.0))
            finally:
                worker.stop(timeout=1.0)

            self.assertEqual(calls, [(True, date(2026, 5, 4))])
            verify_db = connect(db_path)
            try:
                stages = latency_stages_for_event(
                    verify_db, "forecast_sample_changed:2026-05-04:1->2"
                )
            finally:
                verify_db.close()
            self.assertEqual(
                [stage["stage"] for stage in stages],
                ["decision_started", "decision_completed"],
            )

    def test_trading_decision_records_orderbook_state_age(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            store_orderbook(
                db,
                "yes26",
                OrderBook(
                    "yes26",
                    bids=[(0.33, 100)],
                    asks=[(0.35, 100)],
                    tick_size=0.01,
                    min_order_size=5,
                ),
            )
            db.execute(
                """
                update orderbook_snapshots
                set fetched_at_utc = '2026-05-04T07:00:00+00:00'
                where outcome_id = 'yes26'
                """
            )
            db.execute(
                """
                update orderbook_latest
                set fetched_at_utc = '2026-05-04T07:00:00+00:00'
                where outcome_id = 'yes26'
                """
            )
            db.commit()

            store_trading_decision(
                db,
                "actual_cross",
                "yes26",
                "26°C",
                "YES",
                "BUY",
                "missed",
                "test",
                {"decision_now_utc": "2026-05-04T07:00:01.250000+00:00"},
            )
            decision = db.execute(
                "select details_json from paper_decisions order by id desc limit 1"
            ).fetchone()
            details = json.loads(decision["details_json"])

            self.assertAlmostEqual(details["orderbook_state_age_seconds"], 1.25)


class _FakeMonotonic:
    def __init__(self, values):
        self._values = list(values)

    def __call__(self):
        if not self._values:
            raise AssertionError("fake monotonic exhausted")
        return self._values.pop(0)


def _store_aws_actual(
    db,
    high: float,
    minute: int,
    *,
    event_queue: LowLatencyEventQueue,
    monotonic_fn,
) -> None:
    snapshot = store_raw_snapshot(db, "hko", f"aws-actual-{high}", str(high))
    store_hko_current_temperature(
        db,
        snapshot.id,
        HkoCurrentTemperature(
            observed_at_hkt=datetime(2026, 5, 4, 15, minute, tzinfo=HKT),
            station="HKO",
            temperature_c=high,
            since_midnight_max_c=high,
            since_midnight_min_c=21.0,
            raw={},
        ),
        event_queue=event_queue,
        monotonic_fn=monotonic_fn,
    )


def _store_since_midnight(
    db,
    high: float,
    minute: int,
    *,
    event_queue: LowLatencyEventQueue,
    monotonic_fn,
) -> None:
    snapshot = store_raw_snapshot(db, "hko", f"since-midnight-{high}", str(high))
    store_hko_observation(
        db,
        snapshot.id,
        HkoObservation(
            observed_at_hkt=datetime(2026, 5, 4, 15, minute, tzinfo=HKT),
            station="HKO",
            since_midnight_max_c=high,
            since_midnight_min_c=21.0,
            raw={},
        ),
        event_queue=event_queue,
        monotonic_fn=monotonic_fn,
    )


def _ocf_sample(raw_max_c: float) -> OcfForecastSample:
    return OcfForecastSample(
        forecast_date_hkt=date(2026, 5, 4),
        forecast_min_c=22,
        forecast_max_c=round(raw_max_c),
        raw_min_c=21.8,
        raw_max_c=raw_max_c,
        hourly_temperatures=[{"ForecastHour": "15", "Temperature": raw_max_c}],
        raw={"LastModified": "2026-05-04T01:00:00+08:00"},
    )


def _alpha_event(kind: str, event_key: str, details: dict | None = None):
    from whenitrains.low_latency import AlphaEvent

    return AlphaEvent(
        kind=kind,
        event_key=event_key,
        target_date_hkt="2026-05-04",
        source_row_id=2,
        previous_row_id=1,
        committed_monotonic=100.0,
        detected_monotonic=100.0,
        details={} if details is None else details,
    )


def _market(status: str) -> TemperatureMarket:
    return TemperatureMarket(
        event_id="event",
        event_slug="highest-temperature-in-hong-kong-on-may-4-2026",
        title="Highest temperature in Hong Kong on May 4, 2026?",
        target_date=date(2026, 5, 4),
        outcomes=[
            Outcome(
                market_id="m29",
                label="29°C",
                predicate=parse_outcome_label("29°C"),
                yes_token_id="yes29",
                no_token_id="no29",
            )
        ],
        status=status,
    )
