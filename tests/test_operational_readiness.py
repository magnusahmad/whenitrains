import tempfile
import unittest
from pathlib import Path

from whenitrains.alerting import MemoryAlertSink
from whenitrains.operational import (
    LiveSchedulerLock,
    LiveSchedulerLockError,
    evaluate_live_startup_health,
    freeze_new_entries_for_health_failures,
)
from whenitrains.storage import connect, live_setting_enabled, migrate


class OperationalReadinessTests(unittest.TestCase):
    def connect_db(self, path: Path):
        db = connect(path)
        self.addCleanup(db.close)
        return db

    def test_live_scheduler_lock_rejects_second_process_for_same_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "whenitrains.sqlite3"
            first = LiveSchedulerLock(db_path)
            second = LiveSchedulerLock(db_path)

            with first:
                with self.assertRaises(LiveSchedulerLockError):
                    second.acquire()

            with second:
                self.assertTrue(second.locked)

    def test_live_scheduler_lock_uses_db_specific_lock_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "whenitrains.sqlite3"
            lock = LiveSchedulerLock(db_path)

            self.assertEqual(lock.path, Path(tmp) / "whenitrains.sqlite3.live.lock")

    def test_live_startup_health_requires_websockets_and_clean_state(self):
        health = evaluate_live_startup_health(
            market_websocket_connected=True,
            user_websocket_connected=False,
            rest_fallback_available=True,
            credentials_valid=True,
            balance_allowance_ok=True,
            stale_submitted_orders=0,
            local_clob_drift_count=0,
        )

        self.assertFalse(health.ok)
        self.assertEqual(health.reasons, ("user websocket disconnected",))

    def test_live_startup_health_reports_all_fail_closed_reasons(self):
        health = evaluate_live_startup_health(
            market_websocket_connected=False,
            user_websocket_connected=False,
            rest_fallback_available=False,
            credentials_valid=False,
            balance_allowance_ok=False,
            stale_submitted_orders=2,
            local_clob_drift_count=1,
        )

        self.assertFalse(health.ok)
        self.assertEqual(
            health.reasons,
            (
                "market websocket disconnected",
                "user websocket disconnected",
                "REST fallback unavailable",
                "CLOB credentials invalid",
                "balance or allowance insufficient",
                "2 stale submitted live orders",
                "1 local/CLOB drift items",
            ),
        )

    def test_health_failure_freezes_entries_and_records_risk_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self.connect_db(Path(tmp) / "test.db")
            migrate(db)
            health = evaluate_live_startup_health(
                market_websocket_connected=False,
                user_websocket_connected=True,
                rest_fallback_available=True,
                credentials_valid=True,
                balance_allowance_ok=True,
                stale_submitted_orders=0,
                local_clob_drift_count=0,
            )

            alert_sink = MemoryAlertSink()

            frozen = freeze_new_entries_for_health_failures(
                db, health, alert_sink=alert_sink
            )

            self.assertTrue(frozen)
            self.assertTrue(live_setting_enabled(db, "block_new_entries"))
            risk = db.execute(
                "select event_type, severity, details_json from risk_events"
            ).fetchone()
            self.assertEqual(risk["event_type"], "live_startup_health_failed")
            self.assertEqual(risk["severity"], "critical")
            self.assertIn("market websocket disconnected", risk["details_json"])
            self.assertEqual(len(alert_sink.messages), 1)
            self.assertEqual(alert_sink.messages[0].title, "live_startup_health_failed")
            self.assertEqual(alert_sink.messages[0].severity, "critical")

    def test_healthy_startup_health_does_not_freeze_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self.connect_db(Path(tmp) / "test.db")
            migrate(db)
            health = evaluate_live_startup_health(
                market_websocket_connected=True,
                user_websocket_connected=True,
                rest_fallback_available=True,
                credentials_valid=True,
                balance_allowance_ok=True,
                stale_submitted_orders=0,
                local_clob_drift_count=0,
            )

            frozen = freeze_new_entries_for_health_failures(db, health)

            self.assertFalse(frozen)
            self.assertFalse(live_setting_enabled(db, "block_new_entries"))
