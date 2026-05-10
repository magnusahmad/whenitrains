from __future__ import annotations

import fcntl
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .storage import set_live_setting, store_risk_event


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


@dataclass(frozen=True)
class LiveStartupHealth:
    ok: bool
    reasons: tuple[str, ...]


def evaluate_live_startup_health(
    *,
    market_websocket_connected: bool,
    user_websocket_connected: bool,
    rest_fallback_available: bool,
    credentials_valid: bool,
    balance_allowance_ok: bool,
    stale_submitted_orders: int,
    local_clob_drift_count: int,
) -> LiveStartupHealth:
    reasons: list[str] = []
    if not market_websocket_connected:
        reasons.append("market websocket disconnected")
    if not user_websocket_connected:
        reasons.append("user websocket disconnected")
    if not rest_fallback_available:
        reasons.append("REST fallback unavailable")
    if not credentials_valid:
        reasons.append("CLOB credentials invalid")
    if not balance_allowance_ok:
        reasons.append("balance or allowance insufficient")
    if stale_submitted_orders:
        reasons.append(f"{stale_submitted_orders} stale submitted live orders")
    if local_clob_drift_count:
        reasons.append(f"{local_clob_drift_count} local/CLOB drift items")
    return LiveStartupHealth(ok=not reasons, reasons=tuple(reasons))


def freeze_new_entries_for_health_failures(
    db: sqlite3.Connection, health: LiveStartupHealth
) -> bool:
    if health.ok:
        return False
    set_live_setting(db, "block_new_entries", True)
    store_risk_event(
        db,
        "live_startup_health_failed",
        "critical",
        {"reasons": list(health.reasons)},
    )
    return True
