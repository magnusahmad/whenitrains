from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from .orderbook_cache import MarketWebSocketSubscription, OrderBookCache


POLYMARKET_MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class MarketWebSocketError(RuntimeError):
    pass


class StopSignal(Protocol):
    def is_set(self) -> bool:
        ...


@dataclass
class MarketWebSocketClient:
    cache: OrderBookCache
    token_ids_fn: Callable[[], Iterable[str]]
    url: str = POLYMARKET_MARKET_WS_URL
    connect_factory: Callable[[str], Any] | None = None

    async def run_once(self) -> int:
        token_ids = list(dict.fromkeys(self.token_ids_fn()))
        if not token_ids:
            return 0
        connect = self.connect_factory or _default_connect_factory()
        applied = 0
        async with connect(self.url) as websocket:
            await websocket.send(json.dumps(MarketWebSocketSubscription(token_ids).payload()))
            async for raw_message in websocket:
                for message in _decode_messages(raw_message):
                    if self.cache.apply_message(message) is not None:
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


def _default_connect_factory():
    try:
        import websockets
    except ImportError as exc:
        raise MarketWebSocketError(
            "websockets is not installed; install project dependencies before live market streaming"
        ) from exc
    return websockets.connect


def _decode_messages(raw_message: str | bytes) -> list[dict[str, Any]]:
    if isinstance(raw_message, bytes):
        raw_message = raw_message.decode("utf-8")
    payload = json.loads(raw_message)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []
