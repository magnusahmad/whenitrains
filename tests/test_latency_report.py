import hashlib
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from whenitrains.storage import (
    connect,
    latency_duration_summary,
    live_setting_enabled,
    migrate,
    record_latency_stage,
    set_live_setting,
    store_raw_snapshot,
    store_live_order,
    store_orderbook,
    store_trading_decision,
    store_risk_event,
)
from whenitrains.cli import main
from whenitrains.live_user_stream import apply_user_channel_event
from whenitrains.polymarket import OrderBook


ARCHIVE_REPORT_FILES = [
    "latency_db_committed_to_decision_started.txt",
    "latency_decision_started_to_order_submitted.txt",
    "latency_order_submitted_to_fill_confirmed.txt",
    "latency_order_submitted_to_order_rejected.txt",
    "latency_db_committed_to_decision_completed.txt",
    "hko_source_timing_report.txt",
    "readiness_report.txt",
]


def _websocket_orderbook() -> OrderBook:
    return OrderBook(
        "yes25",
        bids=[(0.2, 10)],
        asks=[(0.3, 10)],
        tick_size=0.01,
        min_order_size=5,
    )


def _write_complete_evidence_archive(output_dir: Path, *, checksums: bool = True) -> None:
    output_dir.mkdir()
    for name in ARCHIVE_REPORT_FILES:
        (output_dir / name).write_text(_archive_report_fixture_content(name))
    manifest_lines = [
        "low latency evidence archive",
        "created_at_utc=2026-05-11T00:00:00+00:00",
        "db_path=/private/tmp/test.sqlite3",
        "hko_endpoint_contains=latestReadings",
        "hko_limit=200",
        "all_gates_passed=True",
        "files:",
        *[f"- {name}" for name in ARCHIVE_REPORT_FILES],
    ]
    if checksums:
        manifest_lines.append("checksums:")
        for name in ARCHIVE_REPORT_FILES:
            digest = hashlib.sha256((output_dir / name).read_bytes()).hexdigest()
            manifest_lines.append(f"sha256 {name}={digest}")
    (output_dir / "manifest.txt").write_text("\n".join(manifest_lines) + "\n")


def _archive_report_fixture_content(name: str) -> str:
    if name == "hko_source_timing_report.txt":
        return (
            "hko source timing rows=1\n"
            "response_ms p50=10.000ms p95=10.000ms p99=10.000ms\n"
            "public_availability_fetch_offsets_seconds=0.0:1\n"
        )
    if name == "readiness_report.txt":
        return (
            "low latency readiness report\n"
            "latency:\n"
            "evidence gates:\n"
            "gate hko_commit_to_decision_under_1s=pass count=1 p95=0.100s threshold=1.000s\n"
            "live:\n"
        )
    if name.startswith("latency_") and name.endswith(".txt"):
        stage_pair = name[len("latency_") : -len(".txt")].replace("_to_", " -> ")
        return f"{stage_pair} count=1 p50=0.100s p95=0.100s p99=0.100s\n"
    raise AssertionError(f"unknown archive fixture report {name}")


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
            db.close()

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
            db.close()

    def test_latency_report_cli_prints_stage_percentiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            record_latency_stage(db, "event-1", "db_committed", 10.0, "actual")
            record_latency_stage(db, "event-1", "decision_started", 10.5, "actual")
            db.close()
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

    def test_latency_report_cli_closes_database_connection_on_return(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            record_latency_stage(db, "event-1", "db_committed", 10.0, "actual")
            record_latency_stage(db, "event-1", "decision_started", 10.5, "actual")
            db.close()
            opened = []

            class TrackingConnection:
                def __init__(self, wrapped):
                    self.wrapped = wrapped
                    self.closed = False

                def close(self):
                    self.closed = True
                    self.wrapped.close()

                def __getattr__(self, name):
                    return getattr(self.wrapped, name)

            def tracked_connect(path):
                connection = TrackingConnection(connect(path))
                opened.append(connection)
                return connection

            with (
                patch("whenitrains.cli.connect", tracked_connect),
                redirect_stdout(StringIO()),
            ):
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
            self.assertEqual(len(opened), 1)
            self.assertTrue(opened[0].closed)

    def test_low_latency_archive_evidence_writes_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            output_dir = Path(tmp) / "evidence"
            db = connect(db_path)
            migrate(db)
            record_latency_stage(db, "event-1", "db_committed", 10.0, "actual")
            record_latency_stage(db, "event-1", "decision_started", 10.5, "actual")
            record_latency_stage(db, "event-1", "order_submitted", 10.7, "actual")
            record_latency_stage(db, "event-1", "fill_confirmed", 11.0, "actual")
            db.close()
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "low-latency-archive-evidence",
                        "--output-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn(f"archived low latency evidence to {output_dir}", stdout.getvalue())
            manifest = (output_dir / "manifest.txt").read_text()
            self.assertIn("db_path=", manifest)
            self.assertIn("readiness_report.txt", manifest)
            self.assertIn("latency_db_committed_to_decision_started.txt", manifest)
            self.assertIn("sha256 latency_db_committed_to_decision_started.txt=", manifest)
            readiness = (output_dir / "readiness_report.txt").read_text()
            self.assertIn("low latency readiness report", readiness)
            latency = (output_dir / "latency_db_committed_to_decision_started.txt").read_text()
            self.assertIn("db_committed -> decision_started count=1 p50=0.500s", latency)

    def test_low_latency_archive_evidence_require_evidence_returns_missing_status_after_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            output_dir = Path(tmp) / "evidence"
            db = connect(db_path)
            migrate(db)
            db.close()
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "low-latency-archive-evidence",
                        "--output-dir",
                        str(output_dir),
                        "--require-evidence",
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertTrue((output_dir / "manifest.txt").exists())
            self.assertTrue((output_dir / "readiness_report.txt").exists())
            self.assertIn("readiness evidence missing:", stdout.getvalue())

    def test_low_latency_verify_evidence_archive_passes_complete_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn("verified low latency evidence archive", stdout.getvalue())

    def test_low_latency_verify_evidence_archive_does_not_touch_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            db_path = Path(tmp) / "should-not-exist.sqlite3"

            with redirect_stdout(StringIO()):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertFalse(db_path.exists())

    def test_low_latency_verify_evidence_archive_fails_missing_checksums(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir, checksums=False)
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn(
                "evidence archive checksum entries missing: "
                "latency_db_committed_to_decision_started.txt",
                stdout.getvalue(),
            )

    def test_low_latency_verify_evidence_archive_fails_missing_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            output_dir = Path(tmp) / "evidence"
            db = connect(db_path)
            migrate(db)
            db.close()
            with redirect_stdout(StringIO()):
                archive_exit = main(
                    [
                        "--db",
                        str(db_path),
                        "low-latency-archive-evidence",
                        "--output-dir",
                        str(output_dir),
                        "--require-evidence",
                    ]
                )
            self.assertEqual(archive_exit, 2)
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn("evidence archive gates missing:", stdout.getvalue())
            self.assertIn("hko_commit_to_decision_under_1s", stdout.getvalue())

    def test_low_latency_verify_evidence_archive_fails_missing_manifest_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            manifest = (output_dir / "manifest.txt").read_text()
            (output_dir / "manifest.txt").write_text(
                manifest.replace("low latency evidence archive\n", "", 1)
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn("evidence archive manifest header missing", stdout.getvalue())

    def test_low_latency_verify_evidence_archive_fails_missing_manifest_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            manifest_lines = [
                line
                for line in (output_dir / "manifest.txt").read_text().splitlines()
                if not line.startswith("created_at_utc=")
            ]
            (output_dir / "manifest.txt").write_text("\n".join(manifest_lines) + "\n")
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn(
                "evidence archive manifest metadata missing: created_at_utc",
                stdout.getvalue(),
            )

    def test_low_latency_verify_evidence_archive_fails_invalid_manifest_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            manifest = (output_dir / "manifest.txt").read_text()
            (output_dir / "manifest.txt").write_text(
                manifest.replace("hko_limit=200", "hko_limit=not-an-int")
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn(
                "evidence archive manifest metadata invalid: hko_limit",
                stdout.getvalue(),
            )

    def test_low_latency_verify_evidence_archive_fails_duplicate_manifest_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            manifest = (output_dir / "manifest.txt").read_text()
            (output_dir / "manifest.txt").write_text(
                manifest.replace(
                    "created_at_utc=2026-05-11T00:00:00+00:00",
                    "created_at_utc=2026-05-11T00:00:00+00:00\n"
                    "created_at_utc=2026-05-12T00:00:00+00:00",
                )
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn(
                "evidence archive duplicate manifest key: created_at_utc",
                stdout.getvalue(),
            )

    def test_low_latency_verify_evidence_archive_fails_missing_manifest_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            manifest_lines = [
                line
                for line in (output_dir / "manifest.txt").read_text().splitlines()
                if line not in {"files:", "checksums:"}
            ]
            (output_dir / "manifest.txt").write_text("\n".join(manifest_lines) + "\n")
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn(
                "evidence archive manifest sections missing: files, checksums",
                stdout.getvalue(),
            )

    def test_low_latency_verify_evidence_archive_fails_duplicate_manifest_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            manifest = (output_dir / "manifest.txt").read_text()
            (output_dir / "manifest.txt").write_text(
                manifest.replace("files:", "files:\nfiles:", 1)
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn(
                "evidence archive duplicate manifest section: files",
                stdout.getvalue(),
            )

    def test_low_latency_verify_evidence_archive_fails_reversed_manifest_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            lines = (output_dir / "manifest.txt").read_text().splitlines()
            files_index = lines.index("files:")
            checksums_index = lines.index("checksums:")
            lines[files_index], lines[checksums_index] = lines[checksums_index], lines[files_index]
            (output_dir / "manifest.txt").write_text("\n".join(lines) + "\n")
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn(
                "evidence archive manifest sections out of order",
                stdout.getvalue(),
            )

    def test_low_latency_verify_evidence_archive_ignores_file_entries_outside_files_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            manifest_lines = []
            delayed_file_entries = []
            for line in (output_dir / "manifest.txt").read_text().splitlines():
                if line.startswith("- "):
                    delayed_file_entries.append(line)
                else:
                    manifest_lines.append(line)
            manifest_lines.extend(delayed_file_entries)
            (output_dir / "manifest.txt").write_text("\n".join(manifest_lines) + "\n")
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn(
                "evidence archive manifest entries missing: "
                "latency_db_committed_to_decision_started.txt",
                stdout.getvalue(),
            )

    def test_low_latency_verify_evidence_archive_ignores_checksum_entries_outside_checksums_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            manifest_lines = []
            early_checksum_entries = []
            for line in (output_dir / "manifest.txt").read_text().splitlines():
                if line.startswith("sha256 "):
                    early_checksum_entries.append(line)
                else:
                    manifest_lines.append(line)
            manifest_lines[1:1] = early_checksum_entries
            (output_dir / "manifest.txt").write_text("\n".join(manifest_lines) + "\n")
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn(
                "evidence archive checksum entries missing: "
                "latency_db_committed_to_decision_started.txt",
                stdout.getvalue(),
            )

    def test_low_latency_verify_evidence_archive_requires_exact_passed_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            manifest = (output_dir / "manifest.txt").read_text()
            (output_dir / "manifest.txt").write_text(
                manifest.replace("all_gates_passed=True", "all_gates_passed=True-ish")
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn(
                "evidence archive gates missing: all_gates_passed is not True",
                stdout.getvalue(),
            )

    def test_low_latency_verify_evidence_archive_fails_duplicate_gate_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            manifest = (output_dir / "manifest.txt").read_text()
            (output_dir / "manifest.txt").write_text(
                manifest.replace(
                    "all_gates_passed=True",
                    "all_gates_passed=True\nall_gates_passed=False",
                )
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn(
                "evidence archive duplicate manifest key: all_gates_passed",
                stdout.getvalue(),
            )

    def test_low_latency_verify_evidence_archive_fails_conflicting_gate_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            manifest = (output_dir / "manifest.txt").read_text()
            (output_dir / "manifest.txt").write_text(
                manifest.replace(
                    "all_gates_passed=True",
                    "all_gates_passed=True\nmissing_gates=live_network_smoke_ok",
                )
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn(
                "evidence archive gates inconsistent: missing_gates present while all_gates_passed is True",
                stdout.getvalue(),
            )

    def test_low_latency_verify_evidence_archive_fails_malformed_missing_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            manifest = (output_dir / "manifest.txt").read_text()
            (output_dir / "manifest.txt").write_text(
                manifest.replace(
                    "all_gates_passed=True",
                    "all_gates_passed=False\nmissing_gates=live_network_smoke_ok,,",
                )
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn(
                "evidence archive missing_gates malformed",
                stdout.getvalue(),
            )

    def test_low_latency_verify_evidence_archive_fails_duplicate_manifest_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            name = "readiness_report.txt"
            manifest = (output_dir / "manifest.txt").read_text()
            (output_dir / "manifest.txt").write_text(
                manifest.replace(f"- {name}", f"- {name}\n- {name}", 1)
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn("evidence archive duplicate manifest entry: readiness_report.txt", stdout.getvalue())

    def test_low_latency_verify_evidence_archive_fails_unexpected_manifest_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            manifest = (output_dir / "manifest.txt").read_text()
            (output_dir / "manifest.txt").write_text(
                manifest.replace("files:\n", "files:\n- extra_report.txt\n", 1)
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn(
                "evidence archive unexpected manifest entry: extra_report.txt",
                stdout.getvalue(),
            )

    def test_low_latency_verify_evidence_archive_fails_checksum_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            output_dir = Path(tmp) / "evidence"
            db = connect(db_path)
            migrate(db)
            db.close()
            with redirect_stdout(StringIO()):
                main(
                    [
                        "--db",
                        str(db_path),
                        "low-latency-archive-evidence",
                        "--output-dir",
                        str(output_dir),
                    ]
                )
            (output_dir / "readiness_report.txt").write_text("tampered\n")
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn("evidence archive checksum mismatch: readiness_report.txt", stdout.getvalue())

    def test_low_latency_verify_evidence_archive_fails_duplicate_checksum_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            name = "readiness_report.txt"
            manifest = (output_dir / "manifest.txt").read_text()
            (output_dir / "manifest.txt").write_text(
                manifest.replace(
                    f"sha256 {name}=",
                    f"sha256 {name}=deadbeef\nsha256 {name}=",
                    1,
                )
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn("evidence archive duplicate checksum entry: readiness_report.txt", stdout.getvalue())

    def test_low_latency_verify_evidence_archive_fails_malformed_checksum_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            name = "readiness_report.txt"
            manifest = (output_dir / "manifest.txt").read_text()
            digest = hashlib.sha256((output_dir / name).read_bytes()).hexdigest()
            (output_dir / "manifest.txt").write_text(
                manifest.replace(f"sha256 {name}={digest}", f"sha256 {name}=not-a-sha")
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn(
                "evidence archive checksum digest invalid: readiness_report.txt",
                stdout.getvalue(),
            )

    def test_low_latency_verify_evidence_archive_fails_unexpected_checksum_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            extra = output_dir / "extra_report.txt"
            extra.write_text("extra\n")
            digest = hashlib.sha256(extra.read_bytes()).hexdigest()
            manifest = (output_dir / "manifest.txt").read_text()
            (output_dir / "manifest.txt").write_text(
                manifest + f"sha256 {extra.name}={digest}\n"
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn(
                "evidence archive unexpected checksum entry: extra_report.txt",
                stdout.getvalue(),
            )

    def test_low_latency_verify_evidence_archive_fails_missing_checksum_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            manifest = (output_dir / "manifest.txt").read_text()
            (output_dir / "manifest.txt").write_text(
                manifest + "sha256 stale_extra_report.txt=deadbeef\n"
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn("evidence archive checksum file missing: stale_extra_report.txt", stdout.getvalue())

    def test_low_latency_verify_evidence_archive_fails_empty_report_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            empty_report = output_dir / "latency_db_committed_to_decision_started.txt"
            empty_report.write_text(" \n\t\n")
            digest = hashlib.sha256(empty_report.read_bytes()).hexdigest()
            manifest = (output_dir / "manifest.txt").read_text()
            (output_dir / "manifest.txt").write_text(
                "\n".join(
                    f"sha256 {empty_report.name}={digest}"
                    if line.startswith(f"sha256 {empty_report.name}=")
                    else line
                    for line in manifest.splitlines()
                )
                + "\n"
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn(
                "evidence archive file empty: latency_db_committed_to_decision_started.txt",
                stdout.getvalue(),
            )

    def test_low_latency_verify_evidence_archive_fails_malformed_report_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            name = "latency_db_committed_to_decision_started.txt"
            report = output_dir / name
            report.write_text("unrelated report content\n")
            digest = hashlib.sha256(report.read_bytes()).hexdigest()
            manifest = (output_dir / "manifest.txt").read_text()
            (output_dir / "manifest.txt").write_text(
                "\n".join(
                    f"sha256 {name}={digest}"
                    if line.startswith(f"sha256 {name}=")
                    else line
                    for line in manifest.splitlines()
                )
                + "\n"
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn(
                "evidence archive file malformed: latency_db_committed_to_decision_started.txt",
                stdout.getvalue(),
            )

    def test_low_latency_verify_evidence_archive_fails_latency_report_without_p99(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            name = "latency_db_committed_to_decision_started.txt"
            report = output_dir / name
            report.write_text("db_committed -> decision_started count=1 p50=0.100s p95=0.100s\n")
            digest = hashlib.sha256(report.read_bytes()).hexdigest()
            manifest = (output_dir / "manifest.txt").read_text()
            (output_dir / "manifest.txt").write_text(
                "\n".join(
                    f"sha256 {name}={digest}"
                    if line.startswith(f"sha256 {name}=")
                    else line
                    for line in manifest.splitlines()
                )
                + "\n"
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn(
                "evidence archive file malformed: latency_db_committed_to_decision_started.txt",
                stdout.getvalue(),
            )

    def test_low_latency_verify_evidence_archive_fails_latency_report_without_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            name = "latency_db_committed_to_decision_started.txt"
            report = output_dir / name
            report.write_text(
                "db_committed -> decision_started count=0 p50=n/a p95=n/a p99=n/a\n"
            )
            digest = hashlib.sha256(report.read_bytes()).hexdigest()
            manifest = (output_dir / "manifest.txt").read_text()
            (output_dir / "manifest.txt").write_text(
                "\n".join(
                    f"sha256 {name}={digest}"
                    if line.startswith(f"sha256 {name}=")
                    else line
                    for line in manifest.splitlines()
                )
                + "\n"
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn(
                "evidence archive file malformed: latency_db_committed_to_decision_started.txt",
                stdout.getvalue(),
            )

    def test_low_latency_verify_evidence_archive_fails_hko_report_without_public_offsets(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            name = "hko_source_timing_report.txt"
            report = output_dir / name
            report.write_text(
                "hko source timing rows=1\n"
                "response_ms p50=10.000ms p95=10.000ms p99=10.000ms\n"
            )
            digest = hashlib.sha256(report.read_bytes()).hexdigest()
            manifest = (output_dir / "manifest.txt").read_text()
            (output_dir / "manifest.txt").write_text(
                "\n".join(
                    f"sha256 {name}={digest}"
                    if line.startswith(f"sha256 {name}=")
                    else line
                    for line in manifest.splitlines()
                )
                + "\n"
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn("evidence archive file malformed: hko_source_timing_report.txt", stdout.getvalue())

    def test_low_latency_verify_evidence_archive_fails_hko_report_without_observed_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            name = "hko_source_timing_report.txt"
            report = output_dir / name
            report.write_text(
                "hko source timing rows=0\n"
                "response_ms p50=n/a p95=n/a p99=n/a\n"
                "public_availability_fetch_offsets_seconds=none\n"
            )
            digest = hashlib.sha256(report.read_bytes()).hexdigest()
            manifest = (output_dir / "manifest.txt").read_text()
            (output_dir / "manifest.txt").write_text(
                "\n".join(
                    f"sha256 {name}={digest}"
                    if line.startswith(f"sha256 {name}=")
                    else line
                    for line in manifest.splitlines()
                )
                + "\n"
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn("evidence archive file malformed: hko_source_timing_report.txt", stdout.getvalue())

    def test_low_latency_verify_evidence_archive_fails_malformed_hko_public_offsets(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            name = "hko_source_timing_report.txt"
            report = output_dir / name
            report.write_text(
                "hko source timing rows=1\n"
                "response_ms p50=10.000ms p95=10.000ms p99=10.000ms\n"
                "public_availability_fetch_offsets_seconds=not-a-bucket\n"
            )
            digest = hashlib.sha256(report.read_bytes()).hexdigest()
            manifest = (output_dir / "manifest.txt").read_text()
            (output_dir / "manifest.txt").write_text(
                "\n".join(
                    f"sha256 {name}={digest}"
                    if line.startswith(f"sha256 {name}=")
                    else line
                    for line in manifest.splitlines()
                )
                + "\n"
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn("evidence archive file malformed: hko_source_timing_report.txt", stdout.getvalue())

    def test_low_latency_verify_evidence_archive_fails_malformed_hko_response_percentiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            name = "hko_source_timing_report.txt"
            report = output_dir / name
            report.write_text(
                "hko source timing rows=1\n"
                "response_ms p50=n/a p95=n/a p99=n/a\n"
                "public_availability_fetch_offsets_seconds=0.0:1\n"
            )
            digest = hashlib.sha256(report.read_bytes()).hexdigest()
            manifest = (output_dir / "manifest.txt").read_text()
            (output_dir / "manifest.txt").write_text(
                "\n".join(
                    f"sha256 {name}={digest}"
                    if line.startswith(f"sha256 {name}=")
                    else line
                    for line in manifest.splitlines()
                )
                + "\n"
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn("evidence archive file malformed: hko_source_timing_report.txt", stdout.getvalue())

    def test_low_latency_verify_evidence_archive_fails_readiness_report_without_gate_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            name = "readiness_report.txt"
            report = output_dir / name
            report.write_text("low latency readiness report\nlatency:\nevidence gates:\nlive:\n")
            digest = hashlib.sha256(report.read_bytes()).hexdigest()
            manifest = (output_dir / "manifest.txt").read_text()
            (output_dir / "manifest.txt").write_text(
                "\n".join(
                    f"sha256 {name}={digest}"
                    if line.startswith(f"sha256 {name}=")
                    else line
                    for line in manifest.splitlines()
                )
                + "\n"
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn("evidence archive file malformed: readiness_report.txt", stdout.getvalue())

    def test_low_latency_verify_evidence_archive_fails_readiness_report_missing_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "evidence"
            _write_complete_evidence_archive(output_dir)
            name = "readiness_report.txt"
            report = output_dir / name
            report.write_text(
                "low latency readiness report\n"
                "latency:\n"
                "evidence gates:\n"
                "gate live_network_smoke_ok=missing count=0 latest=none\n"
                "live:\n"
            )
            digest = hashlib.sha256(report.read_bytes()).hexdigest()
            manifest = (output_dir / "manifest.txt").read_text()
            (output_dir / "manifest.txt").write_text(
                "\n".join(
                    f"sha256 {name}={digest}"
                    if line.startswith(f"sha256 {name}=")
                    else line
                    for line in manifest.splitlines()
                )
                + "\n"
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "low-latency-verify-evidence-archive",
                        "--input-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn(
                "evidence archive readiness gate not passing: live_network_smoke_ok",
                stdout.getvalue(),
            )

    def test_low_latency_readiness_report_prints_latency_and_live_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            record_latency_stage(db, "event-1", "db_committed", 10.0, "actual")
            record_latency_stage(db, "event-1", "decision_started", 10.4, "actual")
            record_latency_stage(db, "event-1", "order_submitted", 10.45, "actual")
            record_latency_stage(db, "event-1", "fill_confirmed", 10.8, "actual")
            record_latency_stage(db, "event-2", "order_submitted", 20.0, "actual")
            record_latency_stage(db, "event-2", "order_rejected", 20.2, "actual")
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
            self.assertIn("order_submitted -> order_rejected count=1 p50=0.200s", text)
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
            record_latency_stage(db, "event-1", "decision_completed", 10.9, "actual")
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
                "gate hko_commit_to_decision_completed_under_1s=pass "
                "count=1 p95=0.900s threshold=1.000s",
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
            self.assertIn(
                "gate submit_to_reject_observed=pass evidence=not_observed "
                "count=0 p95=n/a",
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
            self.assertIn("gate websocket_orderbook_snapshots_observed=missing count=0", text)
            self.assertIn("gate user_channel_events_observed=missing count=0", text)
            self.assertIn("gate live_clob_drift_scan_clear=missing count=0", text)

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
            record_latency_stage(db, "event-1", "decision_completed", 10.9, "actual")
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
                response_headers={"Last-Modified": "Mon, 11 May 2026 00:00:02 GMT"},
                fetch_started_at_utc="2026-05-11T00:00:01+00:00",
                response_elapsed_ms=123.0,
            )
            store_raw_snapshot(
                db,
                source="hko",
                endpoint="https://example.test/latestReadings",
                payload="{}",
                response_headers={"Last-Modified": "Mon, 11 May 2026 00:00:02 GMT"},
                fetch_started_at_utc="2026-05-11T00:00:03+00:00",
                response_elapsed_ms=123.0,
            )
            store_orderbook(
                db,
                "yes25",
                _websocket_orderbook(),
                metadata={"source": "polymarket_market_websocket"},
            )
            store_live_order(
                db,
                outcome_id="yes25",
                side="BUY_YES",
                action="BUY",
                status="submitted",
                clob_order_id="order-1",
                requested_size_usd=5.0,
                limit_price=0.2,
            )
            apply_user_channel_event(
                db,
                {
                    "id": "trade-event-1",
                    "event_type": "trade",
                    "order_id": "order-1",
                    "asset_id": "yes25",
                    "side": "BUY",
                    "status": "MATCHED",
                    "size": "25",
                    "price": "0.2",
                },
            )
            settlement_order_id = store_live_order(
                db,
                outcome_id="yes25",
                side="SETTLEMENT",
                action="SELL",
                status="filled",
                event_type="market_resolution",
                event_key="market_resolution:2026-05-11:yes25",
                fill_price=1.0,
                fill_size_usd=25.0,
                fill_shares=25.0,
                reason="resolved market settlement",
            )
            store_live_order(
                db,
                outcome_id="yes25",
                side="BUY_YES",
                action="BUY",
                status="filled",
                event_type="manual_live",
                event_key="manual_live_buy:yes25",
                fill_price=0.2,
                fill_size_usd=5.0,
                fill_shares=25.0,
                reason="manual live buy 25C YES",
            )
            store_live_order(
                db,
                outcome_id="yes25",
                side="SELL",
                action="SELL",
                status="filled",
                event_type="manual_live",
                event_key="manual_live_sell:yes25",
                fill_price=0.2,
                fill_size_usd=5.0,
                fill_shares=25.0,
                reason="manual live sell 25C YES",
            )
            store_risk_event(
                db,
                "live_clob_drift_scan_clear",
                "info",
                {"phase": "readiness-fixture", "drift_count": 0},
            )
            store_risk_event(
                db,
                "live_auth_smoke_ok",
                "info",
                {
                    "signer_address": "0xsigner",
                    "funder_address": "0xfunder",
                    "required_balance_usd": 5.0,
                    "balance_usd": 42.0,
                    "allowance_ok": True,
                    "reason": "ok",
                },
            )
            store_risk_event(
                db,
                "live_network_smoke_ok",
                "info",
                {
                    "all_running": True,
                    "connected_once_all": True,
                    "client_count": 2,
                    "required_clients": 2,
                },
            )
            store_risk_event(
                db,
                "live_scheduler_smoke_ok",
                "info",
                {"ticks": 3, "websockets_enabled": True},
            )
            store_risk_event(
                db,
                "live_kill_switch_allowed",
                "info",
                {"block_new_entries": False, "exit_on_kill_switch": False},
            )
            store_risk_event(
                db,
                "live_settlement_validation_ok",
                "info",
                {
                    "live_order_id": settlement_order_id,
                    "outcome_id": "yes25",
                    "reference": "clob-trade-123/onchain-456",
                },
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
            self.assertIn("gate hko_source_timing_observed=pass count=2", text)
            self.assertIn(
                "gate hko_public_availability_cluster_observed=pass "
                "count=2 threshold=20.000s",
                text,
            )
            self.assertIn("gate websocket_orderbook_snapshots_observed=pass count=1", text)
            self.assertIn("gate user_channel_events_observed=pass count=1", text)
            self.assertIn("gate user_channel_trade_applied=pass count=1", text)
            self.assertIn("gate live_reconcile_observed=pass count=1", text)
            self.assertIn("gate live_settlement_observed=pass count=1", text)
            self.assertIn("gate live_clob_drift_scan_clear=pass count=1", text)
            self.assertIn("gate live_auth_smoke_ok=pass count=1 latest=ok", text)
            self.assertIn("gate live_network_smoke_ok=pass count=1 latest=ok", text)
            self.assertIn("gate manual_live_buy_observed=pass count=1", text)
            self.assertIn("gate manual_live_sell_observed=pass count=1", text)
            self.assertIn("gate live_scheduler_smoke_ok=pass count=1 latest=ok", text)
            self.assertIn(
                "gate live_kill_switch_verification=pass count=1 latest=allowed",
                text,
            )
            self.assertIn("gate live_settlement_validated=pass count=1", text)
            self.assertNotIn("readiness evidence missing", text)

    def test_low_latency_readiness_report_fails_without_settlement_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            store_live_order(
                db,
                outcome_id="yes25",
                side="SETTLEMENT",
                action="SELL",
                status="filled",
                event_type="market_resolution",
                fill_price=1.0,
                fill_size_usd=25.0,
                fill_shares=25.0,
                reason="resolved market settlement",
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
            self.assertIn("gate live_settlement_observed=pass count=1", text)
            self.assertIn("gate live_settlement_validated=missing count=0", text)

    def test_low_latency_readiness_report_fails_when_settlement_validation_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            store_live_order(
                db,
                outcome_id="yes25",
                side="SETTLEMENT",
                action="SELL",
                status="filled",
                event_type="market_resolution",
                fill_price=1.0,
                fill_size_usd=25.0,
                fill_shares=25.0,
                reason="resolved market settlement",
            )
            store_risk_event(
                db,
                "live_settlement_validation_ok",
                "info",
                {
                    "live_order_id": 999,
                    "outcome_id": "yes25",
                    "reference": "stale-validation",
                },
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
            self.assertIn("gate live_settlement_observed=pass count=1", text)
            self.assertIn("gate live_settlement_validated=missing count=0", text)

    def test_low_latency_readiness_report_fails_when_settlement_validation_lacks_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            settlement_order_id = store_live_order(
                db,
                outcome_id="yes25",
                side="SETTLEMENT",
                action="SELL",
                status="filled",
                event_type="market_resolution",
                fill_price=1.0,
                fill_size_usd=25.0,
                fill_shares=25.0,
                reason="resolved market settlement",
            )
            store_risk_event(
                db,
                "live_settlement_validation_ok",
                "info",
                {
                    "live_order_id": settlement_order_id,
                    "outcome_id": "yes25",
                    "reference": "",
                },
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
            self.assertIn("gate live_settlement_observed=pass count=1", text)
            self.assertIn("gate live_settlement_validated=missing count=0", text)

    def test_low_latency_readiness_report_fails_when_latest_kill_switch_verification_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            store_risk_event(
                db,
                "live_kill_switch_allowed",
                "info",
                {"block_new_entries": False, "exit_on_kill_switch": False},
            )
            store_risk_event(
                db,
                "live_kill_switch_blocked",
                "critical",
                {"block_new_entries": True, "exit_on_kill_switch": False},
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
                "gate live_kill_switch_verification=missing count=1 latest=blocked",
                text,
            )

    def test_low_latency_readiness_report_fails_when_latest_scheduler_smoke_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            store_risk_event(
                db,
                "live_scheduler_smoke_ok",
                "info",
                {"ticks": 3, "websockets_enabled": True},
            )
            store_risk_event(
                db,
                "live_scheduler_smoke_failed",
                "critical",
                {"ticks": 3, "websockets_enabled": True, "error": "boom"},
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
                "gate live_scheduler_smoke_ok=missing count=1 latest=failed",
                text,
            )

    def test_low_latency_readiness_report_fails_without_manual_live_sell(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            store_live_order(
                db,
                outcome_id="yes25",
                side="BUY_YES",
                action="BUY",
                status="filled",
                event_type="manual_live",
                fill_price=0.2,
                fill_size_usd=5.0,
                fill_shares=25.0,
                reason="manual live buy 25C YES",
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
            self.assertIn("gate manual_live_buy_observed=pass count=1", text)
            self.assertIn("gate manual_live_sell_observed=missing count=0", text)

    def test_low_latency_readiness_report_fails_when_manual_live_fill_has_no_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            store_live_order(
                db,
                outcome_id="yes25",
                side="BUY_YES",
                action="BUY",
                status="filled",
                event_type="manual_live",
                fill_price=0.2,
                reason="manual live buy 25C YES",
            )
            store_live_order(
                db,
                outcome_id="yes25",
                side="SELL",
                action="SELL",
                status="filled",
                event_type="manual_live",
                fill_price=0.2,
                reason="manual live sell 25C YES",
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
            self.assertIn("gate manual_live_buy_observed=missing count=0", text)
            self.assertIn("gate manual_live_sell_observed=missing count=0", text)

    def test_low_latency_readiness_report_fails_when_latest_network_smoke_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            store_risk_event(
                db,
                "live_network_smoke_ok",
                "info",
                {"all_running": True, "connected_once_all": True, "client_count": 2},
            )
            store_risk_event(
                db,
                "live_network_smoke_failed",
                "critical",
                {"all_running": True, "connected_once_all": False, "client_count": 2},
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
                "gate live_network_smoke_ok=missing count=1 latest=failed",
                text,
            )

    def test_low_latency_readiness_report_fails_when_latest_auth_smoke_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            store_risk_event(
                db,
                "live_auth_smoke_ok",
                "info",
                {
                    "signer_address": "0xsigner",
                    "funder_address": "0xfunder",
                    "required_balance_usd": 5.0,
                    "balance_usd": 42.0,
                    "allowance_ok": True,
                    "reason": "ok",
                },
            )
            store_risk_event(
                db,
                "live_auth_smoke_failed",
                "critical",
                {"signer_address": "0xsigner", "reason": "insufficient allowance"},
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
                "gate live_auth_smoke_ok=missing count=1 latest=failed",
                text,
            )

    def test_low_latency_readiness_report_fails_when_auth_smoke_ok_lacks_preflight_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            store_risk_event(
                db,
                "live_auth_smoke_ok",
                "info",
                {"signer_address": "0xsigner", "reason": "ok"},
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
                "gate live_auth_smoke_ok=missing count=0 latest=ok",
                text,
            )

    def test_low_latency_readiness_report_fails_when_latest_drift_scan_has_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            store_risk_event(
                db,
                "live_clob_drift_scan_clear",
                "info",
                {"phase": "startup", "drift_count": 0},
            )
            store_risk_event(
                db,
                "live_clob_drift_scan_drift",
                "critical",
                {
                    "phase": "reconcile_watchdog",
                    "drift_count": 1,
                    "drifts": [
                        {
                            "token_id": "yes25",
                            "local_shares": 12.5,
                            "clob_sellable_shares": 7.0,
                            "drift_shares": 5.5,
                        }
                    ],
                },
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
                "gate live_clob_drift_scan_clear=missing count=1 latest=drift",
                text,
            )

    def test_low_latency_readiness_report_require_evidence_fails_without_live_settlement(self):
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
            record_latency_stage(db, "event-1", "decision_completed", 10.9, "actual")
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
                response_headers={"Last-Modified": "Mon, 11 May 2026 00:00:02 GMT"},
                fetch_started_at_utc="2026-05-11T00:00:01+00:00",
                response_elapsed_ms=123.0,
            )
            store_raw_snapshot(
                db,
                source="hko",
                endpoint="https://example.test/latestReadings",
                payload="{}",
                response_headers={"Last-Modified": "Mon, 11 May 2026 00:00:02 GMT"},
                fetch_started_at_utc="2026-05-11T00:00:03+00:00",
                response_elapsed_ms=123.0,
            )
            store_orderbook(
                db,
                "yes25",
                _websocket_orderbook(),
                metadata={"source": "polymarket_market_websocket"},
            )
            store_live_order(
                db,
                outcome_id="yes25",
                side="BUY_YES",
                action="BUY",
                status="submitted",
                clob_order_id="order-1",
                requested_size_usd=5.0,
                limit_price=0.2,
            )
            apply_user_channel_event(
                db,
                {
                    "id": "trade-event-1",
                    "event_type": "trade",
                    "order_id": "order-1",
                    "asset_id": "yes25",
                    "side": "BUY",
                    "status": "MATCHED",
                    "size": "25",
                    "price": "0.2",
                },
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
            self.assertIn("gate live_settlement_observed=missing count=0", text)

    def test_low_latency_readiness_report_require_evidence_fails_without_user_trade_event(self):
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
            record_latency_stage(db, "event-1", "decision_completed", 10.9, "actual")
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
                response_headers={"Last-Modified": "Mon, 11 May 2026 00:00:02 GMT"},
                fetch_started_at_utc="2026-05-11T00:00:01+00:00",
                response_elapsed_ms=123.0,
            )
            store_raw_snapshot(
                db,
                source="hko",
                endpoint="https://example.test/latestReadings",
                payload="{}",
                response_headers={"Last-Modified": "Mon, 11 May 2026 00:00:02 GMT"},
                fetch_started_at_utc="2026-05-11T00:00:03+00:00",
                response_elapsed_ms=123.0,
            )
            store_orderbook(
                db,
                "yes25",
                _websocket_orderbook(),
                metadata={"source": "polymarket_market_websocket"},
            )
            apply_user_channel_event(
                db,
                {
                    "id": "user-event-1",
                    "event_type": "order",
                    "order_id": "order-1",
                    "status": "PLACEMENT",
                },
            )
            store_live_order(
                db,
                outcome_id="yes25",
                side="BUY_YES",
                action="BUY",
                status="filled",
                clob_order_id="order-1",
                fill_price=0.2,
                fill_size_usd=5.0,
                fill_shares=25.0,
                raw_reconcile={"status": "matched"},
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
            self.assertIn("gate user_channel_trade_applied=missing count=0", text)

    def test_low_latency_readiness_report_require_evidence_fails_without_live_reconcile(self):
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
            record_latency_stage(db, "event-1", "decision_completed", 10.9, "actual")
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
                response_headers={"Last-Modified": "Mon, 11 May 2026 00:00:02 GMT"},
                fetch_started_at_utc="2026-05-11T00:00:01+00:00",
                response_elapsed_ms=123.0,
            )
            store_raw_snapshot(
                db,
                source="hko",
                endpoint="https://example.test/latestReadings",
                payload="{}",
                response_headers={"Last-Modified": "Mon, 11 May 2026 00:00:02 GMT"},
                fetch_started_at_utc="2026-05-11T00:00:03+00:00",
                response_elapsed_ms=123.0,
            )
            store_orderbook(
                db,
                "yes25",
                _websocket_orderbook(),
                metadata={"source": "polymarket_market_websocket"},
            )
            apply_user_channel_event(
                db,
                {
                    "id": "user-event-1",
                    "event_type": "order",
                    "order_id": "order-1",
                    "status": "PLACEMENT",
                },
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
            self.assertIn("gate live_reconcile_observed=missing count=0", text)

    def test_low_latency_readiness_report_require_evidence_fails_without_hko_burst_cluster(self):
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
            record_latency_stage(db, "event-1", "decision_completed", 10.9, "actual")
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
                response_headers={"Last-Modified": "Mon, 11 May 2026 00:00:02 GMT"},
                fetch_started_at_utc="2026-05-11T00:05:00+00:00",
                response_elapsed_ms=123.0,
            )
            store_orderbook(
                db,
                "yes25",
                _websocket_orderbook(),
                metadata={"source": "polymarket_market_websocket"},
            )
            apply_user_channel_event(
                db,
                {
                    "id": "user-event-1",
                    "event_type": "order",
                    "order_id": "order-1",
                    "status": "PLACEMENT",
                },
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
                "gate hko_public_availability_cluster_observed=missing "
                "count=0 threshold=20.000s",
                text,
            )

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
            record_latency_stage(db, "event-1", "decision_completed", 10.9, "actual")
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
            store_orderbook(
                db,
                "yes25",
                _websocket_orderbook(),
                metadata={"source": "polymarket_market_websocket"},
            )
            apply_user_channel_event(
                db,
                {
                    "id": "user-event-1",
                    "event_type": "order",
                    "order_id": "order-1",
                    "status": "PLACEMENT",
                },
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
                "gate live_money_state_clear=missing "
                "unresolved_orders=1 problem_orders=0 submitted=1 error=0 "
                "missing_bid_positions=0",
                text,
            )

    def test_low_latency_readiness_report_require_evidence_fails_on_unknown_fill_order(self):
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
            record_latency_stage(db, "event-1", "decision_completed", 10.9, "actual")
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
            store_orderbook(
                db,
                "yes25",
                _websocket_orderbook(),
                metadata={"source": "polymarket_market_websocket"},
            )
            store_live_order(
                db,
                outcome_id="yes25",
                side="BUY_YES",
                action="BUY",
                status="unknown_fill",
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
                "gate live_money_state_clear=missing "
                "unresolved_orders=1 problem_orders=0 submitted=0 error=0 "
                "missing_bid_positions=0",
                text,
            )

    def test_low_latency_readiness_report_require_evidence_fails_on_failed_live_order(self):
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
            record_latency_stage(db, "event-1", "decision_completed", 10.9, "actual")
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
            store_orderbook(
                db,
                "yes25",
                _websocket_orderbook(),
                metadata={"source": "polymarket_market_websocket"},
            )
            apply_user_channel_event(
                db,
                {
                    "id": "user-event-1",
                    "event_type": "order",
                    "order_id": "order-1",
                    "status": "PLACEMENT",
                },
            )
            store_live_order(
                db,
                outcome_id="yes25",
                side="BUY_YES",
                action="BUY",
                status="failed",
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
                "gate live_money_state_clear=missing "
                "unresolved_orders=0 problem_orders=1 submitted=0 error=0 "
                "missing_bid_positions=0",
                text,
            )

    def test_low_latency_readiness_report_require_evidence_fails_when_kill_switch_enabled(self):
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
            record_latency_stage(db, "event-1", "decision_completed", 10.9, "actual")
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
            store_orderbook(
                db,
                "yes25",
                _websocket_orderbook(),
                metadata={"source": "polymarket_market_websocket"},
            )
            apply_user_channel_event(
                db,
                {
                    "id": "user-event-1",
                    "event_type": "order",
                    "order_id": "order-1",
                    "status": "PLACEMENT",
                },
            )
            set_live_setting(db, "block_new_entries", True)
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
                "gate kill_switch_clear=missing "
                "block_new_entries=True exit_on_kill_switch=False",
                text,
            )
