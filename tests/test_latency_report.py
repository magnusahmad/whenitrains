import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from whenitrains.storage import (
    connect,
    latency_duration_summary,
    migrate,
    record_latency_stage,
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
