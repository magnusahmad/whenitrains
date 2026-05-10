import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from whenitrains.storage import (
    connect,
    latency_duration_summary,
    live_setting_enabled,
    migrate,
    record_latency_stage,
    set_live_setting,
    store_raw_snapshot,
    store_live_order,
    store_trading_decision,
)
from whenitrains.cli import main


class LatencyReportTests(unittest.TestCase):
    def test_latency_duration_summary_reports_percentiles_between_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            for index, delta in enumerate([0.1, 0.2, 0.3, 0.4], start=1):
                event_key = f"event-{index}"
                record_latency_stage(db, event_key, "db_committed", 100.0, "actual")
                record_latency_stage(db, event_key, "decision_started", 100.0 + delta, "actual")

            summary = latency_duration_summary(db, "db_committed", "decision_started")

            self.assertEqual(summary["count"], 4)
            self.assertAlmostEqual(summary["p50_seconds"], 0.2)
            self.assertAlmostEqual(summary["p95_seconds"], 0.4)
            self.assertAlmostEqual(summary["p99_seconds"], 0.4)

    def test_latency_duration_summary_ignores_incomplete_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            record_latency_stage(db, "complete", "db_committed", 10.0, "actual")
            record_latency_stage(db, "complete", "decision_started", 10.5, "actual")
            record_latency_stage(db, "incomplete", "db_committed", 11.0, "actual")

            summary = latency_duration_summary(db, "db_committed", "decision_started")

            self.assertEqual(summary["count"], 1)
            self.assertAlmostEqual(summary["p50_seconds"], 0.5)

    def test_latency_report_cli_prints_stage_percentiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            record_latency_stage(db, "event-1", "db_committed", 10.0, "actual")
            record_latency_stage(db, "event-1", "decision_started", 10.5, "actual")
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "latency-report",
                        "db_committed",
                        "decision_started",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn("db_committed -> decision_started count=1", stdout.getvalue())
            self.assertIn("p50=0.500s", stdout.getvalue())

    def test_low_latency_readiness_report_prints_latency_and_live_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            record_latency_stage(db, "event-1", "db_committed", 10.0, "actual")
            record_latency_stage(db, "event-1", "decision_started", 10.4, "actual")
            record_latency_stage(db, "event-1", "order_submitted", 10.45, "actual")
            record_latency_stage(db, "event-1", "fill_confirmed", 10.8, "actual")
            store_live_order(
                db,
                outcome_id="yes25",
                side="BUY_YES",
                action="BUY",
                status="submitted",
                clob_order_id="order-1",
            )
            set_live_setting(db, "block_new_entries", True)
            self.assertTrue(live_setting_enabled(db, "block_new_entries"))
            db.close()
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "low-latency-readiness-report",
                    ]
                )

            text = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("low latency readiness report", text)
            self.assertIn("db_committed -> decision_started count=1 p50=0.400s", text)
            self.assertIn("decision_started -> order_submitted count=1 p50=0.050s", text)
            self.assertIn("order_submitted -> fill_confirmed count=1 p50=0.350s", text)
            self.assertIn("live orders total=1 submitted=1 error=0", text)
            self.assertIn("live open_positions=0 open_exposure_usd=0.00", text)
            self.assertIn("kill_switch block_new_entries=True exit_on_kill_switch=False", text)

    def test_low_latency_readiness_report_prints_evidence_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            record_latency_stage(db, "event-1", "db_committed", 10.0, "actual")
            record_latency_stage(db, "event-1", "decision_started", 10.4, "actual")
            record_latency_stage(db, "event-1", "order_submitted", 10.45, "actual")
            record_latency_stage(db, "event-1", "clob_ack", 10.5, "actual")
            record_latency_stage(db, "event-1", "fill_matched", 10.7, "actual")
            record_latency_stage(db, "event-1", "fill_confirmed", 10.8, "actual")
            db.close()
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "low-latency-readiness-report",
                    ]
                )

            text = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("evidence gates:", text)
            self.assertIn(
                "gate hko_commit_to_decision_under_1s=pass "
                "count=1 p95=0.400s threshold=1.000s",
                text,
            )
            self.assertIn(
                "gate decision_to_submit_observed=pass count=1 p95=0.050s",
                text,
            )
            self.assertIn(
                "gate submit_to_fill_observed=pass count=1 p95=0.350s",
                text,
            )
            self.assertIn("gate clob_ack_observed=pass count=1 p95=0.050s", text)
            self.assertIn("gate fill_matched_observed=pass count=1 p95=0.250s", text)
            self.assertIn(
                "gate orderbook_age_under_cap=missing count=0 "
                "p95=n/a threshold=0.250s",
                text,
            )
            self.assertIn("gate hko_source_timing_observed=missing count=0", text)

    def test_low_latency_readiness_report_prints_orderbook_age_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            store_trading_decision(
                db,
                event_type="actual",
                outcome_id="yes25",
                label="25C",
                side="YES",
                action="BUY",
                status="filled",
                reason="test",
                details={"orderbook_state_age_seconds": 0.2},
            )
            db.close()
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "low-latency-readiness-report",
                    ]
                )

            text = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn(
                "gate orderbook_age_under_cap=pass count=1 "
                "p95=0.200s threshold=0.250s",
                text,
            )

    def test_low_latency_readiness_report_require_evidence_fails_when_gates_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            record_latency_stage(db, "event-1", "db_committed", 10.0, "actual")
            record_latency_stage(db, "event-1", "decision_started", 10.4, "actual")
            db.close()
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "low-latency-readiness-report",
                        "--require-evidence",
                    ]
                )

            text = stdout.getvalue()
            self.assertEqual(exit_code, 2)
            self.assertIn("gate decision_to_submit_observed=missing count=0", text)
            self.assertIn("readiness evidence missing", text)

    def test_low_latency_readiness_report_require_evidence_passes_when_gates_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            record_latency_stage(db, "event-1", "db_committed", 10.0, "actual")
            record_latency_stage(db, "event-1", "decision_started", 10.4, "actual")
            record_latency_stage(db, "event-1", "order_submitted", 10.45, "actual")
            record_latency_stage(db, "event-1", "clob_ack", 10.5, "actual")
            record_latency_stage(db, "event-1", "fill_matched", 10.7, "actual")
            record_latency_stage(db, "event-1", "fill_confirmed", 10.8, "actual")
            store_trading_decision(
                db,
                event_type="actual",
                outcome_id="yes25",
                label="25C",
                side="YES",
                action="BUY",
                status="filled",
                reason="test",
                details={"orderbook_state_age_seconds": 0.2},
            )
            store_raw_snapshot(
                db,
                source="hko",
                endpoint="https://example.test/latestReadings",
                payload="{}",
                fetch_started_at_utc="2026-05-11T00:00:01+00:00",
                response_elapsed_ms=123.0,
            )
            db.close()
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "low-latency-readiness-report",
                        "--require-evidence",
                    ]
                )

            text = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("gate hko_source_timing_observed=pass count=1", text)
            self.assertNotIn("readiness evidence missing", text)

    def test_low_latency_readiness_report_require_evidence_fails_on_ambiguous_live_money_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            record_latency_stage(db, "event-1", "db_committed", 10.0, "actual")
            record_latency_stage(db, "event-1", "decision_started", 10.4, "actual")
            record_latency_stage(db, "event-1", "order_submitted", 10.45, "actual")
            record_latency_stage(db, "event-1", "clob_ack", 10.5, "actual")
            record_latency_stage(db, "event-1", "fill_matched", 10.7, "actual")
            record_latency_stage(db, "event-1", "fill_confirmed", 10.8, "actual")
            store_trading_decision(
                db,
                event_type="actual",
                outcome_id="yes25",
                label="25C",
                side="YES",
                action="BUY",
                status="filled",
                reason="test",
                details={"orderbook_state_age_seconds": 0.2},
            )
            store_raw_snapshot(
                db,
                source="hko",
                endpoint="https://example.test/latestReadings",
                payload="{}",
                fetch_started_at_utc="2026-05-11T00:00:01+00:00",
                response_elapsed_ms=123.0,
            )
            store_live_order(
                db,
                outcome_id="yes25",
                side="BUY_YES",
                action="BUY",
                status="submitted",
                clob_order_id="order-1",
            )
            db.close()
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "low-latency-readiness-report",
                        "--require-evidence",
                    ]
                )

            text = stdout.getvalue()
            self.assertEqual(exit_code, 2)
            self.assertIn(
                "gate live_money_state_clear=missing submitted=1 error=0 missing_bid_positions=0",
                text,
            )
