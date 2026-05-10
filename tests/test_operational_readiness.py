import tempfile
import unittest
from pathlib import Path

from whenitrains.operational import (
    LiveSchedulerLock,
    LiveSchedulerLockError,
    evaluate_live_startup_health,
)


class OperationalReadinessTests(unittest.TestCase):
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
