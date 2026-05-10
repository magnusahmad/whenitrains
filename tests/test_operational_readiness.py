import tempfile
import unittest
from pathlib import Path

from whenitrains.operational import LiveSchedulerLock, LiveSchedulerLockError


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
