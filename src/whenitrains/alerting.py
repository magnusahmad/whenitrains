from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class AlertMessage:
    title: str
    severity: str
    details: dict


class AlertSink(Protocol):
    def send(self, message: AlertMessage) -> None:
        ...


@dataclass
class MemoryAlertSink:
    messages: list[AlertMessage] = field(default_factory=list)

    def send(self, message: AlertMessage) -> None:
        self.messages.append(message)


@dataclass(frozen=True)
class WebhookAlertSink:
    url: str
    timeout_seconds: float = 5.0

    def send(self, message: AlertMessage) -> None:
        payload = json.dumps(
            {
                "title": message.title,
                "severity": message.severity,
                "details": message.details,
                "text": format_alert_text(message),
            },
            sort_keys=True,
        ).encode("utf-8")
        request = Request(
            self.url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urlopen(request, timeout=self.timeout_seconds)


def format_alert_text(message: AlertMessage) -> str:
    return (
        f"[{message.severity}] {message.title}\n"
        f"{json.dumps(message.details, sort_keys=True)}"
    )


def alert_sink_from_env(environ: dict[str, str]) -> AlertSink | None:
    webhook_url = environ.get("WHENITRAINS_ALERT_WEBHOOK_URL", "").strip()
    if webhook_url:
        return WebhookAlertSink(webhook_url)
    return None
