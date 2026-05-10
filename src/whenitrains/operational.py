from __future__ import annotations

import fcntl
from pathlib import Path


class LiveSchedulerLockError(RuntimeError):
    pass


class LiveSchedulerLock:
    def __init__(self, db_path: Path) -> None:
        self.path = db_path.with_name(f"{db_path.name}.live.lock")
        self._handle = None

    @property
    def locked(self) -> bool:
        return self._handle is not None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise LiveSchedulerLockError(
                f"live scheduler already holds DB lock: {self.path}"
            ) from exc
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None

    def __enter__(self):
        if not self.locked:
            self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
