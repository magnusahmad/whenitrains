from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from .live_user_stream import apply_user_channel_event
from .market_websocket import _default_connect_factory


POLYMARKET_USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"


class StopSignal(Protocol):
    def is_set(self) -> bool:
        ...


@dataclass(frozen=True)
class UserWebSocketAuth:
    api_key: str
    api_secret: str
    api_passphrase: str

    def payload(self) -> dict[str, str]:
        return {
            "apiKey": self.api_key,
            "secret": self.api_secret,
            "passphrase": self.api_passphrase,
        }


@dataclass
class UserWebSocketClient:
    db: sqlite3.Connection
    auth: UserWebSocketAuth
    market_ids_fn: Callable[[], Iterable[str]]
    url: str = POLYMARKET_USER_WS_URL
    connect_factory: Callable[[str], Any] | None = None

    async def run_once(self) -> int:
        connect = self.connect_factory or _default_connect_factory()
        applied = 0
        async with connect(self.url) as websocket:
            await websocket.send(json.dumps(self._subscription_payload()))
            async for raw_message in websocket:
                for message in _decode_messages(raw_message):
                    result = apply_user_channel_event(self.db, message)
                    if result.stored:
                        applied += 1
        return applied

    async def run_forever(
        self,
        stop_event: StopSignal,
        *,
        reconnect_delay_seconds: float = 1.0,
    ) -> None:
        while not stop_event.is_set():
            try:
                await self.run_once()
            except Exception:
                if stop_event.is_set():
                    return
                await asyncio.sleep(reconnect_delay_seconds)
            else:
                await asyncio.sleep(reconnect_delay_seconds)

    def _subscription_payload(self) -> dict:
        payload = {"auth": self.auth.payload(), "type": "user"}
        market_ids = list(dict.fromkeys(self.market_ids_fn()))
        if market_ids:
            payload["markets"] = market_ids
        return payload

    def close(self) -> None:
        self.db.close()


def _decode_messages(raw_message: str | bytes) -> list[dict[str, Any]]:
    if isinstance(raw_message, bytes):
        raw_message = raw_message.decode("utf-8")
    payload = json.loads(raw_message)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []
