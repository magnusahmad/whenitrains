import unittest
from unittest.mock import patch

from whenitrains.alerting import (
    AlertMessage,
    MemoryAlertSink,
    WebhookAlertSink,
    alert_sink_from_env,
    format_alert_text,
)


class AlertingTests(unittest.TestCase):
    def test_formats_critical_risk_alert(self):
        message = AlertMessage(
            title="live_startup_health_failed",
            severity="critical",
            details={"reasons": ["market websocket disconnected", "1 local/CLOB drift items"]},
        )

        text = format_alert_text(message)

        self.assertIn("[critical] live_startup_health_failed", text)
        self.assertIn("market websocket disconnected", text)
        self.assertIn("1 local/CLOB drift items", text)

    def test_memory_sink_records_alerts(self):
        sink = MemoryAlertSink()
        message = AlertMessage("trade", "info", {"order": "order-1"})

        sink.send(message)

        self.assertEqual(sink.messages, [message])

    def test_webhook_sink_posts_alert_json(self):
        sink = WebhookAlertSink("https://alerts.example.test/hook")
        message = AlertMessage("risk", "critical", {"reason": "stale"})

        with patch("whenitrains.alerting.urlopen") as urlopen:
            sink.send(message)

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://alerts.example.test/hook")
        self.assertEqual(request.headers["Content-type"], "application/json")
        self.assertIn(b'"title": "risk"', request.data)
        self.assertIn(b'"severity": "critical"', request.data)

    def test_alert_sink_from_env_uses_webhook_when_configured(self):
        sink = alert_sink_from_env(
            {"WHENITRAINS_ALERT_WEBHOOK_URL": "https://alerts.example.test/hook"}
        )

        self.assertIsInstance(sink, WebhookAlertSink)
        self.assertEqual(sink.url, "https://alerts.example.test/hook")

    def test_alert_sink_from_env_returns_none_when_unconfigured(self):
        self.assertIsNone(alert_sink_from_env({}))


if __name__ == "__main__":
    unittest.main()
