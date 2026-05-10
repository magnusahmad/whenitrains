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
    store_orderbook,
    store_trading_decision,
    store_risk_event,
)
from whenitrains.cli import main
from whenitrains.live_user_stream import apply_user_channel_event
from whenitrains.polymarket import OrderBook


def _websocket_orderbook() -> OrderBook:
    return OrderBook(
        "yes25",
        bids=[(0.2, 10)],
        asks=[(0.3, 10)],
        tick_size=0.01,
        min_order_size=5,
    )


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
            output_dir.mkdir()
            for name in [
                "latency_db_committed_to_decision_started.txt",
                "latency_decision_started_to_order_submitted.txt",
                "latency_order_submitted_to_fill_confirmed.txt",
                "latency_order_submitted_to_order_rejected.txt",
                "latency_db_committed_to_decision_completed.txt",
                "hko_source_timing_report.txt",
                "readiness_report.txt",
            ]:
                (output_dir / name).write_text(f"{name}\n")
            (output_dir / "manifest.txt").write_text(
                "\n".join(
                    [
                        "low latency evidence archive",
                        "all_gates_passed=True",
                        "files:",
                        "- latency_db_committed_to_decision_started.txt",
                        "- latency_decision_started_to_order_submitted.txt",
                        "- latency_order_submitted_to_fill_confirmed.txt",
                        "- latency_order_submitted_to_order_rejected.txt",
                        "- latency_db_committed_to_decision_completed.txt",
                        "- hko_source_timing_report.txt",
                        "- readiness_report.txt",
                    ]
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

            self.assertEqual(exit_code, 0)
            self.assertIn("verified low latency evidence archive", stdout.getvalue())

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
            store_live_order(
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
                    "live_order_id": 1,
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
                {"signer_address": "0xsigner", "reason": "ok"},
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
