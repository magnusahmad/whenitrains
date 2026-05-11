import tempfile
import threading
import time
import sqlite3
import unittest
from contextlib import redirect_stdout
from datetime import date
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from whenitrains.cli import (
    _discover_market,
    _fetch_current_temperature,
    _fetch_ocf_forecast,
    _fetch_orderbooks,
    main,
)
from whenitrains.hko import (
    AWS_GIS_FORECAST_URL,
    AWS_GIS_READINGS_URL,
    FetchResponse,
    OCF_STATION_URL,
    RHRREAD_URL,
)
from whenitrains.markets import parse_outcome_label
from whenitrains.polymarket import OrderBook, Outcome, TemperatureMarket
from whenitrains.storage import (
    connect,
    live_setting_enabled,
    migrate,
    record_latency_stage,
    store_live_order,
    store_orderbook,
    store_polymarket_event,
    store_raw_snapshot,
    store_risk_event,
)


class CliDiscoveryTests(unittest.TestCase):
    def test_paper_scheduler_starts_blocking_fast_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            worker_events = []

            class FakeWorker:
                def __init__(self, **kwargs):
                    worker_events.append(("init", kwargs["db_path"], kwargs["event_queue"]))

                def start(self):
                    worker_events.append("start")

                def stop(self, timeout=None):
                    worker_events.append(("stop", timeout))

            with (
                patch("whenitrains.cli.FastDecisionWorker", FakeWorker),
                patch("whenitrains.cli.run_scheduled_paper_loop") as run_loop,
                redirect_stdout(StringIO()),
            ):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "paper-scheduler",
                        "--ticks",
                        "0",
                        "--no-startup-backup",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(worker_events[0][0], "init")
            self.assertEqual(worker_events[0][1], db_path)
            self.assertEqual(worker_events[1:], ["start", ("stop", 5)])
            run_loop.assert_called_once()

    def test_fetch_orderbooks_fetches_tokens_concurrently_and_stores_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            store_polymarket_event(
                db,
                TemperatureMarket(
                    event_id="event",
                    event_slug="highest-temperature-in-hong-kong-on-may-10-2026",
                    title="Highest temperature",
                    target_date=date(2026, 5, 10),
                    outcomes=[
                        Outcome(
                            market_id="m26",
                            label="26°C",
                            predicate=parse_outcome_label("26°C"),
                            yes_token_id="yes26",
                            no_token_id="no26",
                        ),
                        Outcome(
                            market_id="m27",
                            label="27°C",
                            predicate=parse_outcome_label("27°C"),
                            yes_token_id="yes27",
                            no_token_id="no27",
                        ),
                    ],
                ),
            )
            active_fetches = 0
            max_active_fetches = 0
            lock = threading.Lock()

            def fake_fetch(token_id):
                nonlocal active_fetches, max_active_fetches
                with lock:
                    active_fetches += 1
                    max_active_fetches = max(max_active_fetches, active_fetches)
                time.sleep(0.02)
                with lock:
                    active_fetches -= 1
                return OrderBook(
                    token_id,
                    bids=[(0.10, 10)],
                    asks=[(0.20, 10)],
                    tick_size=0.01,
                    min_order_size=5,
                )

            with patch("whenitrains.cli.fetch_orderbook", side_effect=fake_fetch):
                _fetch_orderbooks(db, date(2026, 5, 10), quiet=True, max_workers=4)

            self.assertGreater(max_active_fetches, 1)
            rows = db.execute(
                """
                select outcome_id, best_bid, best_ask
                from orderbook_snapshots
                order by outcome_id
                """
            ).fetchall()
            self.assertEqual(
                [(row["outcome_id"], row["best_bid"], row["best_ask"]) for row in rows],
                [
                    ("no26", 0.10, 0.20),
                    ("no27", 0.10, 0.20),
                    ("yes26", 0.10, 0.20),
                    ("yes27", 0.10, 0.20),
                ],
            )
            db.close()

    def test_live_env_exports_prints_shell_safe_required_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "live.env"
            env_path.write_text(
                "\n".join(
                    [
                        "WHENITRAINS_TRADING_MODE=live",
                        "POLYMARKET_SIGNATURE_TYPE=3",
                        "POLYMARKET_FUNDER_ADDRESS=0xfunder",
                        "POLYMARKET_API_KEY=api key",
                        "POLYMARKET_API_SECRET=secret'with quote",
                        "POLYMARKET_API_PASSPHRASE=passphrase",
                        "IGNORED=value",
                    ]
                )
                + "\n"
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(["live-env-exports", "--env-file", str(env_path)])

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                stdout.getvalue().splitlines(),
                [
                    "export WHENITRAINS_TRADING_MODE=live",
                    "export POLYMARKET_SIGNATURE_TYPE=3",
                    "export POLYMARKET_FUNDER_ADDRESS=0xfunder",
                    "export POLYMARKET_API_KEY='api key'",
                    """export POLYMARKET_API_SECRET='secret'"'"'with quote'""",
                    "export POLYMARKET_API_PASSPHRASE=passphrase",
                ],
            )

    def test_live_env_exports_fails_closed_when_required_secret_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "live.env"
            env_path.write_text(
                "\n".join(
                    [
                        "WHENITRAINS_TRADING_MODE=live",
                        "POLYMARKET_SIGNATURE_TYPE=3",
                        "POLYMARKET_FUNDER_ADDRESS=0xfunder",
                        "POLYMARKET_API_KEY=api",
                        "POLYMARKET_API_PASSPHRASE=passphrase",
                    ]
                )
                + "\n"
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(["live-env-exports", "--env-file", str(env_path)])

            self.assertEqual(exit_code, 2)
            self.assertEqual(
                stdout.getvalue().strip(),
                "missing live env values: POLYMARKET_API_SECRET",
            )

    def test_live_scheduler_starts_websocket_runtime_and_passes_book_cache_to_ticks(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            runtime_events = []
            book_cache = object()

            class FakeRuntime:
                all_running = True

                def __init__(self):
                    self.book_cache = book_cache

                def start(self):
                    runtime_events.append("start")

                def stop(self, timeout=None):
                    runtime_events.append(("stop", timeout))

            fake_runtime = FakeRuntime()

            def run_loop(*args, **kwargs):
                self.assertIn("alert_sink", kwargs)
                kwargs["reconcile_watchdog_fn"](args[0])
                kwargs["run_tick_fn"](args[0], date(2026, 5, 4))
                kwargs["fast_event_handler"](args[0], date(2026, 5, 4))

            stdout = StringIO()
            with (
                patch("whenitrains.cli.load_live_config", return_value=object()),
                patch("whenitrains.cli.PolymarketClobClient", return_value=object()),
                patch(
                    "whenitrains.cli.preflight_live",
                    return_value=SimpleNamespace(ok=True, reason="ok"),
                ),
                patch(
                    "whenitrains.cli.LiveWebSocketRuntime.for_live_scheduler",
                    return_value=fake_runtime,
                ) as runtime_factory,
                patch("whenitrains.cli.run_scheduled_paper_loop", side_effect=run_loop),
                patch("whenitrains.cli.run_live_tick", return_value=object()) as live_tick,
                patch("whenitrains.cli.find_live_position_drifts", return_value=[]),
                redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "live-scheduler",
                        "--live",
                        "--ticks",
                        "0",
                        "--no-startup-backup",
                    ]
                )

            self.assertEqual(exit_code, 0)
            runtime_factory.assert_called_once()
            self.assertEqual(runtime_events, ["start", ("stop", 5)])
            self.assertEqual(live_tick.call_count, 2)
            for call in live_tick.call_args_list:
                self.assertIs(call.kwargs["book_cache"], book_cache)
            db = connect(db_path)
            try:
                event = db.execute(
                    "select event_type, severity, details_json from risk_events order by id desc limit 1"
                ).fetchone()
                self.assertEqual(event["event_type"], "live_scheduler_smoke_ok")
                self.assertEqual(event["severity"], "info")
                self.assertIn('"ticks": 0', event["details_json"])
                self.assertIn('"websockets_enabled": true', event["details_json"])
            finally:
                db.close()

    def test_live_network_smoke_starts_and_stops_websocket_runtime_without_trading(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            runtime_events = []

            class FakeRuntime:
                all_running = True
                client_statuses = [
                    SimpleNamespace(
                        connected_once=True,
                        connection_attempts=1,
                        messages_applied=2,
                        last_error=None,
                    )
                ]

                def start(self):
                    runtime_events.append("start")

                def stop(self, timeout=None):
                    runtime_events.append(("stop", timeout))

            fake_runtime = FakeRuntime()
            stdout = StringIO()
            with (
                patch("whenitrains.cli.load_live_config", return_value=object()),
                patch(
                    "whenitrains.cli.LiveWebSocketRuntime.for_live_scheduler",
                    return_value=fake_runtime,
                ) as runtime_factory,
                patch("whenitrains.cli.time.sleep") as sleep,
                patch("whenitrains.cli.run_live_tick") as live_tick,
                redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "live-network-smoke",
                        "--live",
                        "--seconds",
                        "0.1",
                    ]
                )

            self.assertEqual(exit_code, 0)
            runtime_factory.assert_called_once()
            sleep.assert_called_once_with(0.1)
            live_tick.assert_not_called()
            self.assertEqual(runtime_events, ["start", ("stop", 5)])
            self.assertIn(
                "live network smoke websocket_all_running=True",
                stdout.getvalue(),
            )
            self.assertIn(
                "client1_connected_once=True client1_attempts=1 client1_messages=2 client1_last_error=n/a",
                stdout.getvalue(),
            )
            db = connect(db_path)
            try:
                event = db.execute(
                    """
                    select event_type, severity, details_json
                    from risk_events
                    order by id desc
                    limit 1
                    """
                ).fetchone()
                self.assertEqual(event["event_type"], "live_network_smoke_ok")
                self.assertEqual(event["severity"], "info")
                self.assertIn('"all_running": true', event["details_json"])
                self.assertIn('"client_count": 1', event["details_json"])
            finally:
                db.close()

    def test_live_auth_smoke_requires_live_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(["--db", str(db_path), "live-auth-smoke"])

            self.assertEqual(exit_code, 2)
            self.assertIn("refusing live auth smoke without --live", stdout.getvalue())

    def test_live_auth_smoke_prints_required_balance_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            client = object()
            preflight_calls = []

            def preflight(_db, live_client, _config, *, required_balance_usd):
                preflight_calls.append((live_client, required_balance_usd))
                return SimpleNamespace(
                    ok=True,
                    signer_address="0xsigner",
                    funder_address="0xfunder",
                    balance_usd=42.0,
                    allowance_ok=True,
                    reason="ok",
                )

            stdout = StringIO()
            with (
                patch("whenitrains.cli.load_live_config", return_value=object()),
                patch("whenitrains.cli.PolymarketClobClient", return_value=client),
                patch("whenitrains.cli.preflight_live", side_effect=preflight),
                redirect_stdout(stdout),
            ):
                exit_code = main(
                    ["--db", str(db_path), "live-auth-smoke", "--live"]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(len(preflight_calls), 1)
            self.assertIs(preflight_calls[0][0], client)
            self.assertIn(
                f"required_balance_usd={preflight_calls[0][1]:.2f}",
                stdout.getvalue(),
            )
            db = connect(db_path)
            try:
                event = db.execute(
                    """
                    select event_type, severity, details_json
                    from risk_events
                    order by id desc
                    limit 1
                    """
                ).fetchone()
                self.assertEqual(event["event_type"], "live_auth_smoke_ok")
                self.assertEqual(event["severity"], "info")
                self.assertIn('"signer_address": "0xsigner"', event["details_json"])
                self.assertIn('"required_balance_usd":', event["details_json"])
            finally:
                db.close()

    def test_live_readiness_checklist_prints_ordered_evidence_commands(self):
        stdout = StringIO()

        with redirect_stdout(stdout):
            exit_code = main(
                [
                    "--db",
                    "data/whenitrains.sqlite3",
                    "live-readiness-checklist",
                    "--label",
                    "30",
                    "--side",
                    "YES",
                    "--date",
                    "2026-05-11",
                    "--market-kind",
                    "highest",
                    "--size-usd",
                    "5",
                    "--scheduler-ticks",
                    "3",
                ]
            )

        text = stdout.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("1. live-network-smoke --live --require-connected", text)
        self.assertIn("archive live-reconcile output as REST/recent-trades validation evidence", text)
        self.assertIn(
            "PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 "
            "live-buy 30 YES 5.00 --date 2026-05-11 --market-kind highest "
            "--live --yes-i-understand",
            text,
        )
        self.assertIn(
            "PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 "
            "live-scheduler --live --ticks 3 --verbose",
            text,
        )
        self.assertIn("verify persistent kill-switch against the real account", text)
        self.assertIn(
            "PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 "
            "live-kill-switch --block-new-entries",
            text,
        )
        self.assertIn(
            "PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 "
            "live-kill-switch --allow-new-entries",
            text,
        )
        self.assertIn("validate live settlement against CLOB/onchain truth", text)
        self.assertIn(
            "PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 "
            "low-latency-readiness-report --require-evidence",
            text,
        )
        self.assertIn(
            "PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 "
            "hko-source-timing-report",
            text,
        )
        self.assertIn(
            "PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 "
            "low-latency-readiness-db-audit",
            text,
        )
        self.assertIn(
            "PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 "
            "latency-report db_committed decision_completed",
            text,
        )
        self.assertIn(
            "PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 "
            "latency-report order_submitted order_rejected",
            text,
        )
        self.assertIn(
            "PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 "
            "latency-report order_submitted clob_ack",
            text,
        )
        self.assertIn(
            "PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 "
            "latency-report order_submitted fill_matched",
            text,
        )
        self.assertIn(
            "PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 "
            "low-latency-archive-evidence --output-dir "
            "'data/low-latency-evidence/<run-id>' --require-evidence",
            text,
        )
        self.assertIn(
            "PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 "
            "low-latency-verify-evidence-archive --input-dir "
            "'data/low-latency-evidence/<run-id>'",
            text,
        )

    def test_low_latency_readiness_db_audit_reports_missing_evidence_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            db.close()
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "low-latency-readiness-db-audit",
                    ]
                )

            text = stdout.getvalue()
            self.assertEqual(exit_code, 2)
            self.assertIn("low latency readiness db audit", text)
            self.assertIn(f"db_path={db_path}", text)
            self.assertIn("latency_trace_events=0", text)
            self.assertIn("latency_db_commit_to_decision_started_pairs=0", text)
            self.assertIn("latency_db_commit_to_decision_completed_pairs=0", text)
            self.assertIn("latency_decision_to_submit_pairs=0", text)
            self.assertIn("latency_submit_to_ack_pairs=0", text)
            self.assertIn("latency_submit_to_match_pairs=0", text)
            self.assertIn("latency_submit_to_fill_pairs=0", text)
            self.assertIn("hko_timed_raw_snapshots=0", text)
            self.assertIn("websocket_orderbook_snapshots=0", text)
            self.assertIn("live_orders=0", text)
            self.assertIn("manual_live_buy_orders=0", text)
            self.assertIn("manual_live_sell_orders=0", text)
            self.assertIn("live_reconciled_orders=0", text)
            self.assertIn("live_network_smoke_records=0", text)
            self.assertIn("live_auth_smoke_records=0", text)
            self.assertIn("live_scheduler_smoke_records=0", text)
            self.assertIn("live_kill_switch_verification_records=0", text)
            self.assertIn("live_clob_drift_scan_records=0", text)
            self.assertIn("live_settlement_validation_records=0", text)
            self.assertIn(
                "missing_evidence=latency_trace_events,"
                "latency_db_commit_to_decision_started_pairs,",
                text,
            )
            self.assertIn("live_settlement_validation_records", text)
            self.assertIn("readiness_db_audit=missing_evidence", text)

    def test_low_latency_readiness_db_audit_does_not_create_missing_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "missing.db"
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "low-latency-readiness-db-audit",
                    ]
                )

            text = stdout.getvalue()
            self.assertEqual(exit_code, 2)
            self.assertFalse(db_path.exists())
            self.assertIn("low latency readiness db audit", text)
            self.assertIn("read_only_open_error=", text)
            self.assertIn("readiness_db_audit=missing_evidence", text)

    def test_low_latency_readiness_db_audit_tolerates_old_schema_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "old.db"
            db = sqlite3.connect(db_path)
            db.execute("create table raw_snapshots (id integer primary key, source text)")
            db.execute(
                "create table orderbook_snapshots (id integer primary key, outcome_id text)"
            )
            db.execute("create table paper_decisions (id integer primary key)")
            db.execute("create table live_orders (id integer primary key)")
            db.execute("create table risk_events (id integer primary key)")
            db.commit()
            db.close()
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "low-latency-readiness-db-audit",
                    ]
                )

            text = stdout.getvalue()
            self.assertEqual(exit_code, 2)
            self.assertIn("hko_raw_snapshots=0", text)
            self.assertIn("hko_timed_raw_snapshots=0", text)
            self.assertIn("websocket_orderbook_snapshots=0", text)
            self.assertIn("paper_decisions_with_orderbook_age=0", text)
            self.assertIn("latency_db_commit_to_decision_started_pairs=0", text)
            self.assertIn("manual_live_buy_orders=0", text)
            self.assertIn("live_settlement_orders=0", text)
            self.assertIn("live_user_trade_applied_events=0", text)
            self.assertIn("live_network_smoke_records=0", text)
            self.assertIn("missing_evidence=latency_trace_events", text)
            self.assertIn("readiness_db_audit=missing_evidence", text)

    def test_low_latency_readiness_db_audit_passes_when_evidence_counts_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            record_latency_stage(
                db,
                "event-1",
                "db_committed",
                1.0,
                event_type="aws_actual_transition",
            )
            record_latency_stage(
                db,
                "event-1",
                "decision_started",
                1.1,
                event_type="aws_actual_transition",
            )
            record_latency_stage(
                db,
                "event-1",
                "decision_completed",
                1.2,
                event_type="aws_actual_transition",
            )
            record_latency_stage(
                db,
                "event-1",
                "order_submitted",
                1.3,
                event_type="aws_actual_transition",
            )
            record_latency_stage(
                db,
                "event-1",
                "clob_ack",
                1.4,
                event_type="aws_actual_transition",
            )
            record_latency_stage(
                db,
                "event-1",
                "fill_matched",
                1.5,
                event_type="aws_actual_transition",
            )
            record_latency_stage(
                db,
                "event-1",
                "fill_confirmed",
                1.6,
                event_type="aws_actual_transition",
            )
            store_raw_snapshot(
                db,
                "hko",
                "latestReadings",
                "{}",
                fetch_started_at_utc="2026-05-11T00:00:00+00:00",
                headers_received_at_utc="2026-05-11T00:00:00.100000+00:00",
                payload_received_at_utc="2026-05-11T00:00:00.200000+00:00",
                response_elapsed_ms=200.0,
            )
            store_orderbook(
                db,
                "yes25",
                OrderBook(
                    token_id="yes25",
                    bids=[(0.40, 10)],
                    asks=[(0.45, 10)],
                    tick_size=0.01,
                    min_order_size=5.0,
                ),
                metadata={"source": "polymarket_market_websocket"},
            )
            db.execute(
                """
                insert into paper_decisions
                (created_at_utc, event_type, event_key, outcome_id, label, side,
                 action, status, reason, details_json)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-05-11T00:00:00+00:00",
                    "event",
                    "event-1",
                    "yes25",
                    "25",
                    "YES",
                    "BUY",
                    "filled",
                    "test",
                    '{"orderbook_state_age_seconds": 0.1}',
                ),
            )
            store_live_order(
                db,
                outcome_id="yes25",
                side="YES",
                action="BUY",
                status="filled",
                event_type="manual_live",
                event_key="manual_live_buy:yes25",
                clob_order_id="clob-buy-1",
                raw_reconcile={"id": "clob-buy-1", "status": "filled"},
                fill_price=0.45,
                fill_size_usd=5.0,
                fill_shares=11.1,
            )
            store_live_order(
                db,
                outcome_id="yes25",
                side="YES",
                action="SELL",
                status="filled",
                event_type="manual_live",
                event_key="manual_live_sell:yes25",
                clob_order_id="clob-sell-1",
                raw_reconcile={"id": "clob-sell-1", "status": "filled"},
                fill_price=0.50,
                fill_size_usd=5.0,
                fill_shares=10.0,
            )
            settlement_order_id = store_live_order(
                db,
                outcome_id="yes25",
                side="SETTLEMENT",
                action="SELL",
                status="filled",
                event_type="market_resolution",
                event_key="market_resolution:yes25",
                clob_order_id="clob-settlement-1",
                raw_reconcile={"id": "clob-settlement-1", "status": "filled"},
                fill_price=1.0,
                fill_size_usd=10.0,
                fill_shares=10.0,
                reason="resolved market settlement",
            )
            db.execute(
                """
                insert into live_user_events
                (event_id, received_at_utc, event_type, clob_order_id, outcome_id,
                 status, side, price, size, applied_position_delta, raw_event_json)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "trade-1",
                    "2026-05-11T00:00:01+00:00",
                    "trade",
                    "clob-1",
                    "yes25",
                    "MATCHED",
                    "BUY",
                    0.45,
                    11.1,
                    1,
                    "{}",
                ),
            )
            db.commit()
            store_risk_event(
                db,
                "live_network_smoke_ok",
                "info",
                {"all_running": True, "connected_once_all": True},
            )
            store_risk_event(
                db,
                "live_auth_smoke_ok",
                "info",
                {"signer_address": "0xsigner", "allowance_ok": True},
            )
            store_risk_event(
                db,
                "live_scheduler_smoke_ok",
                "info",
                {"ticks": 3, "websockets_enabled": True},
            )
            store_risk_event(
                db,
                "live_kill_switch_allowed",
                "info",
                {"block_new_entries": False, "exit_on_kill_switch": False},
            )
            store_risk_event(
                db,
                "live_clob_drift_scan_clear",
                "info",
                {"drift_count": 0},
            )
            store_risk_event(
                db,
                "live_settlement_validation_ok",
                "info",
                {"live_order_id": settlement_order_id, "reference": "clob-reference"},
            )
            db.close()
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "low-latency-readiness-db-audit",
                    ]
                )

            text = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("latency_trace_events=7", text)
            self.assertIn("latency_db_commit_to_decision_started_pairs=1", text)
            self.assertIn("latency_db_commit_to_decision_completed_pairs=1", text)
            self.assertIn("latency_decision_to_submit_pairs=1", text)
            self.assertIn("latency_submit_to_ack_pairs=1", text)
            self.assertIn("latency_submit_to_match_pairs=1", text)
            self.assertIn("latency_submit_to_fill_pairs=1", text)
            self.assertIn("hko_timed_raw_snapshots=1", text)
            self.assertIn("websocket_orderbook_snapshots=1", text)
            self.assertIn("paper_decisions_with_orderbook_age=1", text)
            self.assertIn("live_orders=3", text)
            self.assertIn("manual_live_buy_orders=1", text)
            self.assertIn("manual_live_sell_orders=1", text)
            self.assertIn("live_settlement_orders=1", text)
            self.assertIn("live_reconciled_orders=3", text)
            self.assertIn("live_user_events=1", text)
            self.assertIn("live_user_trade_applied_events=1", text)
            self.assertIn("live_network_smoke_records=1", text)
            self.assertIn("live_auth_smoke_records=1", text)
            self.assertIn("live_scheduler_smoke_records=1", text)
            self.assertIn("live_kill_switch_verification_records=1", text)
            self.assertIn("live_clob_drift_scan_records=1", text)
            self.assertIn("live_settlement_validation_records=1", text)
            self.assertNotIn("missing_evidence=", text)
            self.assertIn("readiness_db_audit=evidence_present", text)

    def test_live_kill_switch_records_block_and_allow_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"

            with redirect_stdout(StringIO()):
                block_exit = main(
                    [
                        "--db",
                        str(db_path),
                        "live-kill-switch",
                        "--block-new-entries",
                    ]
                )
                allow_exit = main(
                    [
                        "--db",
                        str(db_path),
                        "live-kill-switch",
                        "--allow-new-entries",
                    ]
                )

            self.assertEqual(block_exit, 0)
            self.assertEqual(allow_exit, 0)
            db = connect(db_path)
            try:
                events = db.execute(
                    """
                    select event_type, severity, details_json
                    from risk_events
                    order by id
                    """
                ).fetchall()
                self.assertEqual(
                    [event["event_type"] for event in events],
                    ["live_kill_switch_blocked", "live_kill_switch_allowed"],
                )
                self.assertEqual(events[-1]["severity"], "info")
                self.assertIn('"block_new_entries": false', events[-1]["details_json"])
                self.assertFalse(live_setting_enabled(db, "block_new_entries"))
            finally:
                db.close()

    def test_live_settlement_validate_records_validation_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            order_id = store_live_order(
                db,
                outcome_id="yes25",
                side="SETTLEMENT",
                action="SELL",
                status="filled",
                event_type="market_resolution",
                event_key="market_resolution:2026-05-11:yes25",
                fill_price=1.0,
                fill_size_usd=25.0,
                fill_shares=25.0,
                reason="resolved market settlement",
            )
            db.close()
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "live-settlement-validate",
                        "--live",
                        "--order-id",
                        str(order_id),
                        "--reference",
                        "clob-trade-123/onchain-456",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn(f"validated live settlement order_id={order_id}", stdout.getvalue())
            db = connect(db_path)
            try:
                event = db.execute(
                    "select event_type, severity, details_json from risk_events order by id desc limit 1"
                ).fetchone()
                self.assertEqual(event["event_type"], "live_settlement_validation_ok")
                self.assertEqual(event["severity"], "info")
                self.assertIn(f'"live_order_id": {order_id}', event["details_json"])
                self.assertIn('"reference": "clob-trade-123/onchain-456"', event["details_json"])
            finally:
                db.close()

    def test_live_settlement_validate_rejects_non_settlement_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            order_id = store_live_order(
                db,
                outcome_id="yes25",
                side="BUY_YES",
                action="BUY",
                status="filled",
                event_type="manual_live",
                fill_price=0.2,
                fill_size_usd=5.0,
                fill_shares=25.0,
                reason="manual live buy",
            )
            db.close()
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "live-settlement-validate",
                        "--live",
                        "--order-id",
                        str(order_id),
                        "--reference",
                        "not-settlement",
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn("not a filled settlement", stdout.getvalue())

    def test_live_network_smoke_require_connected_fails_when_client_never_connected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"

            class FakeRuntime:
                all_running = True
                client_statuses = [
                    SimpleNamespace(
                        connected_once=False,
                        connection_attempts=1,
                        messages_applied=0,
                        last_error="connect failed",
                    )
                ]

                def start(self):
                    pass

                def stop(self, timeout=None):
                    pass

            stdout = StringIO()
            with (
                patch("whenitrains.cli.load_live_config", return_value=object()),
                patch(
                    "whenitrains.cli.LiveWebSocketRuntime.for_live_scheduler",
                    return_value=FakeRuntime(),
                ),
                patch("whenitrains.cli.time.sleep"),
                redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "live-network-smoke",
                        "--live",
                        "--seconds",
                        "0",
                        "--require-connected",
                    ]
                )

            text = stdout.getvalue()
            self.assertEqual(exit_code, 2)
            self.assertIn("client1_connected_once=False", text)
            self.assertIn("live network smoke connected_once_all=False", text)
            db = connect(db_path)
            try:
                event = db.execute(
                    "select event_type, severity from risk_events order by id desc limit 1"
                ).fetchone()
                self.assertEqual(event["event_type"], "live_network_smoke_failed")
                self.assertEqual(event["severity"], "critical")
            finally:
                db.close()

    def test_live_network_smoke_require_connected_requires_market_and_user_clients(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"

            class FakeRuntime:
                all_running = True
                client_statuses = [
                    SimpleNamespace(
                        connected_once=True,
                        connection_attempts=1,
                        messages_applied=1,
                        last_error=None,
                    )
                ]

                def start(self):
                    pass

                def stop(self, timeout=None):
                    pass

            stdout = StringIO()
            with (
                patch("whenitrains.cli.load_live_config", return_value=object()),
                patch(
                    "whenitrains.cli.LiveWebSocketRuntime.for_live_scheduler",
                    return_value=FakeRuntime(),
                ),
                patch("whenitrains.cli.time.sleep"),
                redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "live-network-smoke",
                        "--live",
                        "--seconds",
                        "0",
                        "--require-connected",
                    ]
                )

            text = stdout.getvalue()
            self.assertEqual(exit_code, 2)
            self.assertIn("live network smoke client_count=1 required_clients=2", text)

    def test_live_scheduler_freezes_entries_when_startup_drift_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"

            def run_loop(*args, **kwargs):
                return None

            stdout = StringIO()
            with (
                patch("whenitrains.cli.load_live_config", return_value=object()),
                patch("whenitrains.cli.PolymarketClobClient", return_value=object()),
                patch(
                    "whenitrains.cli.preflight_live",
                    return_value=SimpleNamespace(ok=True, reason="ok"),
                ),
                patch("whenitrains.cli.find_live_position_drifts", return_value=[object()]),
                patch("whenitrains.cli.run_scheduled_paper_loop", side_effect=run_loop),
                redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "live-scheduler",
                        "--live",
                        "--ticks",
                        "0",
                        "--no-startup-backup",
                        "--no-websockets",
                    ]
                )

            self.assertEqual(exit_code, 0)
            db = connect(db_path)
            try:
                self.assertTrue(live_setting_enabled(db, "block_new_entries"))
                risk = db.execute(
                    "select event_type, severity from risk_events order by id desc limit 1"
                ).fetchone()
                self.assertEqual(risk["event_type"], "live_startup_health_failed")
                self.assertEqual(risk["severity"], "critical")
            finally:
                db.close()
            self.assertIn("1 local/CLOB drift items", stdout.getvalue())

    def test_live_scheduler_enforces_persistent_kill_switch_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            client = object()
            calls = []

            def run_loop(*args, **kwargs):
                result = kwargs["reconcile_watchdog_fn"](args[0])
                self.assertIn("live kill-switch exits", result.notes[0])

            def enforce(_db, live_client, event_key=None):
                calls.append((live_client, event_key))
                return SimpleNamespace(
                    enabled=True,
                    cancel_all_status="ok",
                    sells_filled=1,
                    sells_attempted=1,
                    sells_missed=0,
                )

            stdout = StringIO()
            with (
                patch("whenitrains.cli.load_live_config", return_value=object()),
                patch("whenitrains.cli.PolymarketClobClient", return_value=client),
                patch(
                    "whenitrains.cli.preflight_live",
                    return_value=SimpleNamespace(ok=True, reason="ok"),
                ),
                patch("whenitrains.cli.find_live_position_drifts", return_value=[]),
                patch("whenitrains.cli.enforce_live_kill_switch_exits", side_effect=enforce),
                patch("whenitrains.cli.run_scheduled_paper_loop", side_effect=run_loop),
                redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "live-scheduler",
                        "--live",
                        "--ticks",
                        "0",
                        "--no-startup-backup",
                        "--no-websockets",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                calls,
                [
                    (client, "live_scheduler_startup"),
                    (client, "live_reconcile_watchdog"),
                ],
            )
            self.assertIn("live kill-switch exits cancel_all=ok sells=1/1 missed=0", stdout.getvalue())

    def test_live_scheduler_reconciles_pending_orders_before_watchdog_drift_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            client = object()
            calls = []

            def run_loop(*args, **kwargs):
                result = kwargs["reconcile_watchdog_fn"](args[0])
                self.assertIn("live reconcile checked=1 filled=1", result.notes[0])

            def reconcile(_db, live_client):
                calls.append(live_client)
                return SimpleNamespace(
                    orders_checked=1,
                    orders_filled=1,
                    orders_open=0,
                    orders_error=0,
                    rebuilt_positions=1,
                )

            stdout = StringIO()
            with (
                patch("whenitrains.cli.load_live_config", return_value=object()),
                patch("whenitrains.cli.PolymarketClobClient", return_value=client),
                patch(
                    "whenitrains.cli.preflight_live",
                    return_value=SimpleNamespace(ok=True, reason="ok"),
                ),
                patch("whenitrains.cli.find_live_position_drifts", return_value=[]),
                patch("whenitrains.cli.reconcile_pending_live_orders", side_effect=reconcile),
                patch("whenitrains.cli.run_scheduled_paper_loop", side_effect=run_loop),
                redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "live-scheduler",
                        "--live",
                        "--ticks",
                        "0",
                        "--no-startup-backup",
                        "--no-websockets",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(calls, [client, client])
            self.assertIn(
                "live reconcile checked=1 filled=1 open=0 errors=0 rebuilt_positions=1",
                stdout.getvalue(),
            )
            db = connect(db_path)
            try:
                clear_scan_count = db.execute(
                    """
                    select count(*) from risk_events
                    where event_type = 'live_clob_drift_scan_clear'
                    """
                ).fetchone()[0]
                self.assertEqual(clear_scan_count, 2)
            finally:
                db.close()

    def test_live_scheduler_reconcile_watchdog_freezes_entries_when_drift_appears(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            drift_calls = []

            def run_loop(*args, **kwargs):
                result = kwargs["reconcile_watchdog_fn"](args[0])
                self.assertIn("live reconcile watchdog froze entries", result.notes[0])

            def drifts(_db, _client):
                drift_calls.append("scan")
                return [] if len(drift_calls) == 1 else [
                    SimpleNamespace(
                        token_id="yes25",
                        local_shares=12.5,
                        clob_sellable_shares=None,
                        drift_shares=None,
                    )
                ]

            stdout = StringIO()
            with (
                patch("whenitrains.cli.load_live_config", return_value=object()),
                patch("whenitrains.cli.PolymarketClobClient", return_value=object()),
                patch(
                    "whenitrains.cli.preflight_live",
                    return_value=SimpleNamespace(ok=True, reason="ok"),
                ),
                patch("whenitrains.cli.find_live_position_drifts", side_effect=drifts),
                patch("whenitrains.cli.repair_live_position_drifts", return_value=0),
                patch("whenitrains.cli.run_scheduled_paper_loop", side_effect=run_loop),
                redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "live-scheduler",
                        "--live",
                        "--ticks",
                        "0",
                        "--no-startup-backup",
                        "--no-websockets",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(drift_calls, ["scan", "scan"])
            db = connect(db_path)
            try:
                self.assertTrue(live_setting_enabled(db, "block_new_entries"))
                risk = db.execute(
                    "select event_type, severity from risk_events order by id desc limit 1"
                ).fetchone()
                self.assertEqual(risk["event_type"], "live_startup_health_failed")
                self.assertEqual(risk["severity"], "critical")
            finally:
                db.close()

    def test_live_scheduler_reconcile_watchdog_repairs_lower_clob_drift_before_freezing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            drift = SimpleNamespace(
                token_id="yes25",
                local_shares=12.5,
                clob_sellable_shares=7.0,
                drift_shares=5.5,
            )

            class FakeRuntime:
                book_cache = object()
                all_running = True

                def start(self):
                    return None

                def stop(self, timeout=None):
                    return None

            def run_loop(*args, **kwargs):
                result = kwargs["reconcile_watchdog_fn"](args[0])
                self.assertEqual(result.notes, ("live reconcile watchdog repaired 1 local/CLOB drift items",))

            with (
                patch("whenitrains.cli.load_live_config", return_value=object()),
                patch("whenitrains.cli.PolymarketClobClient", return_value=object()),
                patch(
                    "whenitrains.cli.preflight_live",
                    return_value=SimpleNamespace(ok=True, reason="ok"),
                ),
                patch(
                    "whenitrains.cli.find_live_position_drifts",
                    side_effect=[[], [drift], []],
                ) as find_drifts,
                patch(
                    "whenitrains.cli.LiveWebSocketRuntime.for_live_scheduler",
                    return_value=FakeRuntime(),
                ),
                patch("whenitrains.cli.repair_live_position_drifts", return_value=1) as repair,
                patch("whenitrains.cli.run_scheduled_paper_loop", side_effect=run_loop),
                redirect_stdout(StringIO()),
            ):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "live-scheduler",
                        "--live",
                        "--ticks",
                        "0",
                        "--no-startup-backup",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(find_drifts.call_count, 3)
            repair.assert_called_once()
            db = connect(db_path)
            try:
                self.assertFalse(live_setting_enabled(db, "block_new_entries"))
            finally:
                db.close()

    def test_live_scheduler_reconcile_watchdog_freezes_entries_when_websocket_runtime_stalls(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"

            class FakeRuntime:
                book_cache = object()
                all_running = False

                def start(self):
                    return None

                def stop(self, timeout=None):
                    return None

            def run_loop(*args, **kwargs):
                result = kwargs["reconcile_watchdog_fn"](args[0])
                self.assertIn("live reconcile watchdog froze entries", result.notes[0])

            with (
                patch("whenitrains.cli.load_live_config", return_value=object()),
                patch("whenitrains.cli.PolymarketClobClient", return_value=object()),
                patch(
                    "whenitrains.cli.preflight_live",
                    return_value=SimpleNamespace(ok=True, reason="ok"),
                ),
                patch("whenitrains.cli.find_live_position_drifts", return_value=[]),
                patch(
                    "whenitrains.cli.LiveWebSocketRuntime.for_live_scheduler",
                    return_value=FakeRuntime(),
                ),
                patch("whenitrains.cli.run_scheduled_paper_loop", side_effect=run_loop),
                redirect_stdout(StringIO()),
            ):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "live-scheduler",
                        "--live",
                        "--ticks",
                        "0",
                        "--no-startup-backup",
                    ]
                )

            self.assertEqual(exit_code, 0)
            db = connect(db_path)
            try:
                self.assertTrue(live_setting_enabled(db, "block_new_entries"))
                risk = db.execute(
                    "select details_json from risk_events order by id desc limit 1"
                ).fetchone()
                self.assertIn("market websocket disconnected", risk["details_json"])
                self.assertIn("user websocket disconnected", risk["details_json"])
            finally:
                db.close()

    def test_discover_market_fetches_highest_and_lowest_temperature_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            requested_slugs = []

            def fake_fetch(slug):
                requested_slugs.append(slug)
                if slug not in {
                    "highest-temperature-in-hong-kong-on-may-7-2026",
                    "lowest-temperature-in-hong-kong-on-may-7-2026",
                }:
                    return None
                return {
                    "id": f"event-{slug}",
                    "slug": slug,
                    "title": slug,
                    "eventDate": "2026-05-07",
                    "markets": [
                        {
                            "id": f"market-{slug}",
                            "question": slug,
                            "groupItemTitle": "25°C",
                            "clobTokenIds": '["YES_TOKEN", "NO_TOKEN"]',
                        }
                    ],
                }

            with (
                patch("whenitrains.cli.fetch_hk_temperature_event", side_effect=fake_fetch),
                patch("whenitrains.cli.resolution_rules_warning", return_value=None),
            ):
                discovered = _discover_market(db, date(2026, 5, 7))

            self.assertTrue(discovered)
            self.assertEqual(
                requested_slugs,
                [
                    "highest-temperature-in-hong-kong-on-may-7-2026",
                    "lowest-temperature-in-hong-kong-on-may-7-2026",
                ],
            )
            slugs = [
                row["slug"]
                for row in db.execute("select slug from markets order by slug")
            ]
            self.assertEqual(
                slugs,
                [
                    "highest-temperature-in-hong-kong-on-may-7-2026",
                    "lowest-temperature-in-hong-kong-on-may-7-2026",
                ],
            )
            db.close()

    def test_fetch_current_temperature_records_aws_gis_actual_on_success(self):
        aws_payload = """Latest readings recorded at 14:30 Hong Kong Time 7 May 2026
STN,WINDDIRECTION,WINDSPEED,GUST,TEMP,RH,MAXTEMP,MINTEMP,GRASSTEMP,GRASSMINTEMP,VISIBILITY,PRESSURE,TEMPDIFFERENCE,HEATINDEX,
HKO,,,,28.9,69,29.3,24.0,,,,1011.0,4.8,27.3,
"""
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)

            with patch(
                "whenitrains.cli.fetch_response",
                return_value=FetchResponse(AWS_GIS_READINGS_URL, aws_payload, {}),
            ):
                returned = _fetch_current_temperature(db)

            self.assertEqual(returned, aws_payload)
            obs = db.execute(
                """
                select station, temperature_c, since_midnight_max_c, since_midnight_min_c
                from hko_current_observations
                """
            ).fetchone()
            self.assertEqual(obs["station"], "HKO")
            self.assertEqual(obs["temperature_c"], 28.9)
            self.assertEqual(obs["since_midnight_max_c"], 29.3)
            sources = [
                row["source"]
                for row in db.execute("select source from hko_source_update_minutes")
            ]
            self.assertEqual(sources, ["aws_gis_actual"])
            db.close()

    def test_fetch_current_temperature_learns_aws_gis_publish_minute(self):
        aws_payload = """Latest readings recorded at 14:30 Hong Kong Time 7 May 2026
STN,WINDDIRECTION,WINDSPEED,GUST,TEMP,RH,MAXTEMP,MINTEMP,GRASSTEMP,GRASSMINTEMP,VISIBILITY,PRESSURE,TEMPDIFFERENCE,HEATINDEX,
HKO,,,,28.9,69,29.3,24.0,,,,1011.0,4.8,27.3,
"""
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)

            with patch(
                "whenitrains.cli.fetch_response",
                return_value=FetchResponse(
                    AWS_GIS_READINGS_URL,
                    aws_payload,
                    {"Last-Modified": "Thu, 07 May 2026 06:38:01 GMT"},
                ),
            ):
                _fetch_current_temperature(db)

            rows = db.execute(
                """
                select update_minute_hkt, json_extract(evidence_json, '$.kind') as kind
                from hko_source_update_minutes
                order by update_minute_hkt
                """
            ).fetchall()
            self.assertEqual(
                [(row["update_minute_hkt"], row["kind"]) for row in rows],
                [("14:30", "payload_header"), ("14:38", "http_Last-Modified")],
            )
            db.close()

    def test_fetch_current_temperature_persists_http_timing(self):
        aws_payload = """Latest readings recorded at 14:30 Hong Kong Time 7 May 2026
STN,WINDDIRECTION,WINDSPEED,GUST,TEMP,RH,MAXTEMP,MINTEMP,GRASSTEMP,GRASSMINTEMP,VISIBILITY,PRESSURE,TEMPDIFFERENCE,HEATINDEX,
HKO,,,,28.9,69,29.3,24.0,,,,1011.0,4.8,27.3,
"""
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)

            with patch(
                "whenitrains.cli.fetch_response",
                return_value=FetchResponse(
                    AWS_GIS_READINGS_URL,
                    aws_payload,
                    {"Last-Modified": "Thu, 07 May 2026 06:38:01 GMT"},
                    fetch_started_at_utc="2026-05-11T00:00:00+00:00",
                    headers_received_at_utc="2026-05-11T00:00:00.040000+00:00",
                    payload_received_at_utc="2026-05-11T00:00:00.090000+00:00",
                    response_elapsed_ms=90.2,
                ),
            ):
                _fetch_current_temperature(db)

            row = db.execute(
                """
                select fetch_started_at_utc, headers_received_at_utc,
                       payload_received_at_utc, response_elapsed_ms
                from raw_snapshots
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(row["fetch_started_at_utc"], "2026-05-11T00:00:00+00:00")
            self.assertEqual(row["headers_received_at_utc"], "2026-05-11T00:00:00.040000+00:00")
            self.assertEqual(row["payload_received_at_utc"], "2026-05-11T00:00:00.090000+00:00")
            self.assertAlmostEqual(row["response_elapsed_ms"], 90.2)
            db.close()

    def test_hko_source_timing_report_summarizes_aws_fetch_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = connect(db_path)
            migrate(db)
            store_raw_snapshot(
                db,
                "hko",
                AWS_GIS_READINGS_URL,
                "payload",
                {"Last-Modified": "Thu, 07 May 2026 06:38:01 GMT"},
                fetch_started_at_utc="2026-05-11T06:37:59+00:00",
                headers_received_at_utc="2026-05-11T06:37:59.040000+00:00",
                payload_received_at_utc="2026-05-11T06:37:59.090000+00:00",
                response_elapsed_ms=90.2,
            )
            store_raw_snapshot(
                db,
                "hko",
                AWS_GIS_READINGS_URL,
                "payload-2",
                {"Last-Modified": "Thu, 07 May 2026 06:38:01 GMT"},
                fetch_started_at_utc="2026-05-11T06:38:00+00:00",
                headers_received_at_utc="2026-05-11T06:38:00.050000+00:00",
                payload_received_at_utc="2026-05-11T06:38:00.120000+00:00",
                response_elapsed_ms=120.4,
            )
            db.close()

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "hko-source-timing-report",
                        "--endpoint-contains",
                        "latestReadings",
                    ]
                )

            self.assertEqual(exit_code, 0)
            text = stdout.getvalue()
            self.assertIn("hko source timing rows=2", text)
            self.assertIn("response_ms p50=90.2 p95=120.4 p99=120.4", text)
            self.assertIn("fetch_second_offsets=59:1, 00:1", text)
            self.assertIn("last_modified_minute_offsets=38:2", text)
            self.assertIn(
                "public_availability_fetch_offsets_seconds=-2.0:1, -1.0:1",
                text,
            )

    def test_fetch_current_temperature_labels_rhrread_fallback_and_keeps_aws_failed(self):
        rhrread_payload = """
        {
          "updateTime": "2026-05-07T14:02:00+08:00",
          "temperature": {
            "data": [
              {"place": "Hong Kong Observatory", "value": 29, "unit": "C"}
            ]
          }
        }
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)

            def fake_fetch(url):
                if url == AWS_GIS_READINGS_URL:
                    raise OSError("aws unavailable")
                self.assertEqual(url, RHRREAD_URL)
                return FetchResponse(RHRREAD_URL, rhrread_payload, {})

            with patch("whenitrains.cli.fetch_response", side_effect=fake_fetch):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "AWS GIS actual fetch failed; stored rhrread observation fallback only",
                ):
                    _fetch_current_temperature(db)

            obs = db.execute(
                """
                select station, temperature_c, since_midnight_max_c, since_midnight_min_c
                from hko_current_observations
                """
            ).fetchone()
            self.assertEqual(obs["station"], "Hong Kong Observatory")
            self.assertEqual(obs["temperature_c"], 29.0)
            self.assertIsNone(obs["since_midnight_max_c"])
            sources = [
                row["source"]
                for row in db.execute("select source from hko_source_update_minutes")
            ]
            self.assertEqual(sources, ["rhrread_actual"])
            db.close()

    def test_fetch_forecast_prefers_aws_gis_station_forecast_payload(self):
        payload = """
        {
          "LastModified": 20260507181146,
          "StationCode": "HKO",
          "DailyForecast": [
            {
              "ForecastDate": "20260508",
              "ForecastChanceOfRain": "60%",
              "ForecastMaximumTemperature": 29,
              "ForecastMinimumTemperature": 24
            }
          ],
          "HourlyWeatherForecast": [
            {"ForecastHour": "2026050812", "ForecastTemperature": 28.4},
            {"ForecastHour": "2026050813", "ForecastTemperature": 29.0}
          ]
        }
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)

            with patch(
                "whenitrains.cli.fetch_response",
                return_value=FetchResponse(AWS_GIS_FORECAST_URL, payload, {}),
            ) as fetch:
                _hash, forecasts = _fetch_ocf_forecast(db)

            fetch.assert_called_once_with(AWS_GIS_FORECAST_URL)
            self.assertEqual(len(forecasts), 1)
            snapshot = db.execute(
                "select endpoint from raw_snapshots order by id desc limit 1"
            ).fetchone()
            self.assertEqual(snapshot["endpoint"], AWS_GIS_FORECAST_URL)
            sample = db.execute(
                """
                select raw_max_c, hourly_temperatures_json
                from ocf_forecast_samples
                """
            ).fetchone()
            self.assertEqual(sample["raw_max_c"], 29.0)
            self.assertIn("2026-05-08T13:00:00+08:00", sample["hourly_temperatures_json"])
            db.close()

    def test_fetch_forecast_falls_back_to_ocf_station_url(self):
        payload = """
        {
          "LastModified": 20260507181146,
          "StationCode": "HKO",
          "DailyForecast": [
            {
              "ForecastDate": "20260508",
              "ForecastMaximumTemperature": 29,
              "ForecastMinimumTemperature": 24
            }
          ],
          "HourlyWeatherForecast": []
        }
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)

            def fake_fetch(url):
                if url == AWS_GIS_FORECAST_URL:
                    raise OSError("aws forecast unavailable")
                self.assertEqual(url, OCF_STATION_URL)
                return FetchResponse(OCF_STATION_URL, payload, {})

            with patch("whenitrains.cli.fetch_response", side_effect=fake_fetch):
                _fetch_ocf_forecast(db)

            snapshot = db.execute(
                "select endpoint from raw_snapshots order by id desc limit 1"
            ).fetchone()
            self.assertEqual(snapshot["endpoint"], OCF_STATION_URL)
            db.close()


if __name__ == "__main__":
    unittest.main()
