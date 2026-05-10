from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .live import _apply_live_buy_fill, _apply_live_sell_fill


@dataclass(frozen=True)
class UserEventApplyResult:
    stored: bool
    position_applied: bool = False


def apply_user_channel_event(
    db: sqlite3.Connection, event: dict[str, Any]
) -> UserEventApplyResult:
    event_id = _event_id(event)
    event_type = str(event.get("event_type") or event.get("type") or "").lower()
    order_id = _order_id(event)
    token_id = _token_id(event)
    status = _status(event)
    side = _side(event)
    price = _optional_float(event.get("price"))
    size = _optional_float(event.get("size") or event.get("matched_amount"))

    cursor = db.execute(
        """
        insert or ignore into live_user_events
        (event_id, received_at_utc, event_type, clob_order_id, outcome_id,
         status, side, price, size, raw_event_json)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            datetime.now(timezone.utc).isoformat(),
            event_type,
            order_id,
            token_id,
            status,
            side,
            price,
            size,
            json.dumps(event),
        ),
    )
    db.commit()
    if cursor.rowcount == 0:
        return UserEventApplyResult(stored=False, position_applied=False)

    if event_type == "order":
        _update_order_lifecycle(db, order_id, status, event)
        return UserEventApplyResult(stored=True, position_applied=False)
    if event_type == "trade" and status in {"MATCHED", "MINED", "CONFIRMED"}:
        applied = _apply_trade_delta(
            db,
            event_id=event_id,
            order_id=order_id,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            event=event,
        )
        return UserEventApplyResult(stored=True, position_applied=applied)
    return UserEventApplyResult(stored=True, position_applied=False)


def _update_order_lifecycle(
    db: sqlite3.Connection, order_id: str | None, status: str | None, event: dict[str, Any]
) -> None:
    if order_id is None or status is None:
        return
    local_status = {
        "PLACEMENT": "submitted",
        "UPDATE": "submitted",
        "CANCELLATION": "cancelled",
        "FAILED": "failed",
        "RETRYING": "submitted",
        "MATCHED": "filled",
        "MINED": "filled",
        "CONFIRMED": "filled",
    }.get(status, status.lower())
    db.execute(
        """
        update live_orders
        set status = ?,
            raw_reconcile_json = ?
        where clob_order_id = ?
        """,
        (local_status, json.dumps(event), order_id),
    )
    db.commit()


def _apply_trade_delta(
    db: sqlite3.Connection,
    *,
    event_id: str,
    order_id: str | None,
    token_id: str | None,
    side: str | None,
    price: float | None,
    size: float | None,
    event: dict[str, Any],
) -> bool:
    if order_id is None or token_id is None or side is None or price is None or size is None:
        return False
    order = db.execute(
        """
        select *
        from live_orders
        where clob_order_id = ?
        order by id desc
        limit 1
        """,
        (order_id,),
    ).fetchone()
    if order is None:
        return False
    fill_size_usd = price * size
    action = _trade_action(side, order)
    db.execute(
        """
        update live_orders
        set status = 'filled',
            reconciled_at_utc = ?,
            fill_price = ?,
            fill_size_usd = ?,
            fill_shares = ?,
            raw_reconcile_json = ?
        where id = ?
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            price,
            fill_size_usd,
            size,
            json.dumps(event),
            int(order["id"]),
        ),
    )
    if action == "SELL":
        _apply_live_sell_fill(db, token_id, size, fill_size_usd)
    else:
        _apply_live_buy_fill(db, token_id, size, fill_size_usd)
    db.execute(
        """
        update live_user_events
        set applied_position_delta = 1
        where event_id = ?
        """,
        (event_id,),
    )
    db.commit()
    return True


def _event_id(event: dict[str, Any]) -> str:
    for key in ("id", "event_id", "trade_id", "transaction_hash"):
        value = event.get(key)
        if value:
            return str(value)
    return json.dumps(event, sort_keys=True)


def _order_id(event: dict[str, Any]) -> str | None:
    for key in ("order_id", "orderID", "orderId", "maker_order_id", "taker_order_id"):
        value = event.get(key)
        if value:
            return str(value)
    return None


def _token_id(event: dict[str, Any]) -> str | None:
    for key in ("asset_id", "token_id", "asset"):
        value = event.get(key)
        if value:
            return str(value)
    return None


def _status(event: dict[str, Any]) -> str | None:
    value = event.get("status") or event.get("type")
    return None if value is None else str(value).upper()


def _side(event: dict[str, Any]) -> str | None:
    value = event.get("side") or event.get("taker_side")
    return None if value is None else str(value).upper()


def _trade_action(side: str, order: sqlite3.Row) -> str:
    if str(order["action"] or "").upper() in {"BUY", "SELL"}:
        return str(order["action"]).upper()
    return "SELL" if side == "SELL" else "BUY"


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)
