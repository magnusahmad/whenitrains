import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from whenitrains.hko import HKT, HkoCurrentTemperature, OcfForecastSample
from whenitrains.low_latency import LowLatencyEventQueue, process_next_fast_event
from whenitrains.storage import (
    connect,
    latency_stages_for_event,
    migrate,
    store_hko_current_temperature,
    store_ocf_forecast_samples,
    store_orderbook,
    store_raw_snapshot,
    store_trading_decision,
)
from whenitrains.polymarket import OrderBook


class LowLatencyReadinessTests(unittest.TestCase):
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


def _alpha_event(kind: str, event_key: str):
    from whenitrains.low_latency import AlphaEvent

    return AlphaEvent(
        kind=kind,
        event_key=event_key,
        target_date_hkt="2026-05-04",
        source_row_id=2,
        previous_row_id=1,
        committed_monotonic=100.0,
        detected_monotonic=100.0,
        details={},
    )
