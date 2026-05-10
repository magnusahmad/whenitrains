from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .live import LiveConfig
from .market_websocket import MarketWebSocketClient
from .orderbook_cache import OrderBookCache
from .storage import (
    connect,
    list_active_market_condition_ids,
    list_active_market_token_ids,
)
from .user_websocket import UserWebSocketAuth, UserWebSocketClient


class AsyncRuntimeClient(Protocol):
    async def run_forever(
        self,
        stop_event: threading.Event,
        *,
        reconnect_delay_seconds: float = 1.0,
    ) -> None:
        ...


@dataclass
class LiveWebSocketRuntime:
    market_client_factory: Callable[[OrderBookCache], AsyncRuntimeClient]
    user_client_factory: Callable[[], AsyncRuntimeClient]
    book_cache: OrderBookCache = field(default_factory=OrderBookCache)
    reconnect_delay_seconds: float = 1.0
    _threads: list[threading.Thread] = field(default_factory=list, init=False)
    _stop_events: list[threading.Event] = field(default_factory=list, init=False)
    _clients: list[AsyncRuntimeClient] = field(default_factory=list, init=False)

    @classmethod
    def for_live_scheduler(
        cls,
        *,
        db_path: Path,
        config: LiveConfig,
        min_date_hkt: str,
    ) -> LiveWebSocketRuntime:
        book_cache = OrderBookCache(persist_snapshots=False)

        def token_ids() -> list[str]:
            db = connect(db_path)
            try:
                return list_active_market_token_ids(db, min_date_hkt)
            finally:
                db.close()

        def condition_ids() -> list[str]:
            db = connect(db_path)
            try:
                return list_active_market_condition_ids(db, min_date_hkt)
            finally:
                db.close()

        def market_client(cache: OrderBookCache) -> MarketWebSocketClient:
            return MarketWebSocketClient(cache=cache, token_ids_fn=token_ids)

        def user_client() -> UserWebSocketClient:
            return UserWebSocketClient(
                db=connect(db_path),
                auth=UserWebSocketAuth(
                    api_key=config.api_key,
                    api_secret=config.api_secret,
                    api_passphrase=config.api_passphrase,
                ),
                market_ids_fn=condition_ids,
            )

        return cls(
            market_client_factory=market_client,
            user_client_factory=user_client,
            book_cache=book_cache,
        )

    @property
    def running(self) -> bool:
        return any(thread.is_alive() for thread in self._threads)

    @property
    def all_running(self) -> bool:
        return len(self._threads) == 2 and all(thread.is_alive() for thread in self._threads)

    @property
    def client_statuses(self) -> list[object]:
        return [
            status
            for client in self._clients
            if (status := getattr(client, "status", None)) is not None
        ]

    def start(self) -> None:
        if self.running:
            return
        self._clients = []
        self._threads = [
            self._start_thread(lambda: self.market_client_factory(self.book_cache)),
            self._start_thread(self.user_client_factory),
        ]

    def stop(self, *, timeout: float | None = None) -> None:
        for event in self._stop_events:
            event.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads = [thread for thread in self._threads if thread.is_alive()]
        if not self._threads:
            self._stop_events = []

    def _start_thread(self, client_factory: Callable[[], AsyncRuntimeClient]) -> threading.Thread:
        ready = threading.Event()

        def run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            stop_event = threading.Event()
            self._stop_events.append(stop_event)
            client = client_factory()
            self._clients.append(client)
            ready.set()
            try:
                loop.run_until_complete(
                    client.run_forever(
                        stop_event,
                        reconnect_delay_seconds=self.reconnect_delay_seconds,
                    )
                )
            finally:
                close = getattr(client, "close", None)
                if close is not None:
                    close()
                loop.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        ready.wait(timeout=5)
        return thread
