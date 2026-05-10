import json
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from whenitrains.config import Settings
from whenitrains.live import (
    LiveConfig,
    PolymarketClobClient,
    enforce_live_kill_switch_exits,
    execute_live_buy,
    execute_live_sell,
    find_live_position_drifts,
    freeze_new_entries_for_stale_submitted_orders,
    reconcile_pending_live_orders,
    repair_live_position_drifts,
    reconcile_submitted_live_order,
    rebuild_live_positions_from_filled_orders,
    _floor_decimal,
    _fill_values,
    load_live_config,
    preflight_live,
)
from whenitrains.polymarket import OrderBook
from whenitrains.dashboard_server import active_live_positions
from whenitrains.storage import (
    connect,
    get_live_position,
    get_live_setting,
    live_setting_enabled,
    live_dashboard_stats,
    live_total_open_exposure,
    migrate,
    record_latency_stage,
    set_live_setting,
    store_live_order,
    store_orderbook,
    latency_duration_summary,
    latency_stages_for_event,
    upsert_live_position,
)


class FakeClobClient:
    def __init__(
        self,
        fill=True,
        reconcile_payload="default",
        buy_response=None,
        trades=None,
        token_balances=None,
    ):
        self.fill = fill
        self.reconcile_payload = reconcile_payload
        self.buy_response = buy_response
        self.trades = trades or []
        self.token_balances = token_balances or {}
        self.buys = []
        self.sells = []
        self.cancel_all_calls = 0

    def signer_address(self):
        return "0xsigner"

    def balance_usd(self):
        return 100.0

    def allowance_ok(self):
        return True

    def buy_fak(self, token_id, price, size_usd):
        self.buys.append((token_id, price, size_usd))
        return self.buy_response or {"orderID": "buy-1", "status": "matched"}

    def sell_fak(self, token_id, price, shares):
        self.sells.append((token_id, price, shares))
        return {"orderID": "sell-1", "status": "matched"}

    def cancel_all(self):
        self.cancel_all_calls += 1
        return {"cancelled": True}

    def token_balance(self, token_id):
        value = self.token_balances.get(token_id)
        if isinstance(value, list):
            if len(value) > 1:
                return value.pop(0)
            return value[0] if value else None
        return value

    def reconcile_order(self, order_id, token_id):
        if self.reconcile_payload != "default":
            return self.reconcile_payload
        if not self.fill:
            return {"order_id": order_id, "token_id": token_id, "status": "submitted"}
        return {"order_id": order_id, "token_id": token_id, "status": "filled"}

    def trades_for_order(self, order_id, token_id):
        return self.trades


class StrictBalanceClient:
    def __init__(self):
        self.params = []

    def get_balance_allowance(self, params):
        if not hasattr(params, "signature_type"):
            raise AttributeError("params missing signature_type")
        self.params.append(params)
        return {"balance": "5000000", "allowance": "1"}


class MarketMetadataClient:
    def get_tick_size(self, token_id):
        self.tick_size_token_id = token_id
        return "0.001"

    def get_neg_risk(self, token_id):
        self.neg_risk_token_id = token_id
        return True

    def get_market(self, token_id):
        self.market_token_id = token_id
        return {"minimum_tick_size": "0.01", "neg_risk": False}


class V2MarketBuyClient(MarketMetadataClient):
    def create_and_post_market_order(self, order_args, options, order_type):
        self.order_args = order_args
        self.order_options = options
        self.order_type = order_type
        return {"orderID": "order-1", "status": "matched"}


class EmptyOrderLookupClient:
    def get_order(self, order_id):
        self.order_id = order_id
        return None


class TimeoutPreflightClient:
    def signer_address(self):
        return "0xsigner"

    def balance_usd(self):
        raise RuntimeError("timeout")

    def allowance_ok(self):
        raise RuntimeError("timeout")


class RawBalanceAllowanceClient:
    def __init__(self, payload):
        self.payload = payload

    def signer_address(self):
        return "0xsigner"

    def balance_allowance(self):
        return self.payload


class LiveTests(unittest.TestCase):
    def test_live_scheduler_buy_cap_is_five_usd(self):
        self.assertEqual(Settings.live_scheduler_order_cap_usd, 5.0)

    def test_migrate_adds_live_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)

            tables = {
                row["name"]
                for row in db.execute(
                    "select name from sqlite_master where type = 'table'"
                )
            }
            self.assertIn("live_orders", tables)
            self.assertIn("live_positions", tables)
            self.assertIn("live_settings", tables)

    def test_load_live_config_requires_prederived_credentials_and_keychain_key(self):
        env = {
            "WHENITRAINS_TRADING_MODE": "live",
            "POLYMARKET_SIGNATURE_TYPE": "3",
            "POLYMARKET_FUNDER_ADDRESS": "0xfunder",
            "POLYMARKET_API_KEY": "api",
            "POLYMARKET_API_SECRET": "secret",
            "POLYMARKET_API_PASSPHRASE": "passphrase",
        }
        with patch("whenitrains.live.read_keychain_secret", return_value="0xabc"):
            config = load_live_config(env)

        self.assertEqual(config.signature_type, 3)
        self.assertEqual(config.private_key, "0xabc")
        self.assertEqual(config.api_key, "api")

    def test_execute_live_buy_uses_fak_and_updates_position_from_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            client = FakeClobClient()

            result = execute_live_buy(
                db,
                client,
                token_id="yes25",
                side="YES",
                size_usd=5,
                asks=[(0.40, 100)],
                reason="test live buy",
                max_price=0.40,
                min_fill_usd=5,
                order_cap_usd=5,
                label="25C",
            )

            self.assertEqual(result.status, "filled")
            self.assertEqual(client.buys, [("yes25", 0.40, 5.0)])
            pos = get_live_position(db, "yes25")
            self.assertIsNotNone(pos)
            self.assertAlmostEqual(pos["net_shares"], 12.5)
            self.assertAlmostEqual(live_total_open_exposure(db), 5.0)

    def test_execute_live_buy_marks_unknown_fill_when_matched_response_omits_amounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            client = FakeClobClient(reconcile_payload=None)

            result = execute_live_buy(
                db,
                client,
                token_id="yes25",
                side="YES",
                size_usd=5,
                asks=[(0.40, 100)],
                reason="test live buy",
                max_price=0.40,
                min_fill_usd=5,
                order_cap_usd=5,
                label="25C",
            )

            self.assertEqual(result.status, "unknown_fill")
            self.assertIsNone(get_live_position(db, "yes25"))
            row = db.execute("select status, raw_reconcile_json from live_orders").fetchone()
            self.assertEqual(row["status"], "unknown_fill")
            self.assertIn('"status": "matched"', row["raw_reconcile_json"])

    def test_execute_live_buy_uses_matched_order_response_amounts_when_reconcile_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            client = FakeClobClient(
                reconcile_payload=None,
                buy_response={
                    "orderID": "buy-1",
                    "status": "matched",
                    "makingAmount": "5000000",
                    "takingAmount": "12500000",
                },
            )

            result = execute_live_buy(
                db,
                client,
                token_id="yes25",
                side="YES",
                size_usd=5,
                asks=[(0.40, 100)],
                reason="test live buy",
                max_price=0.40,
                min_fill_usd=5,
                order_cap_usd=5,
                label="25C",
            )

            self.assertEqual(result.status, "filled")
            pos = get_live_position(db, "yes25")
            self.assertIsNotNone(pos)
            self.assertAlmostEqual(pos["net_shares"], 12.5)

    def test_execute_live_buy_does_not_record_phantom_fill_when_token_balance_does_not_increase(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            client = FakeClobClient(
                reconcile_payload=None,
                buy_response={
                    "orderID": "buy-1",
                    "status": "matched",
                    "makingAmount": "5000000",
                    "takingAmount": "12500000",
                },
                token_balances={"yes25": [0.0, 0.0]},
            )

            result = execute_live_buy(
                db,
                client,
                token_id="yes25",
                side="YES",
                size_usd=5,
                asks=[(0.40, 100)],
                reason="test live buy",
                max_price=0.40,
                min_fill_usd=5,
                order_cap_usd=5,
                label="25C",
            )

            self.assertEqual(result.status, "unknown_fill")
            self.assertEqual(result.shares, 0)
            self.assertIsNone(get_live_position(db, "yes25"))
            row = db.execute(
                "select status, fill_size_usd, fill_shares from live_orders"
            ).fetchone()
            self.assertEqual(row["status"], "unknown_fill")
            self.assertEqual(row["fill_size_usd"], 0)
            self.assertEqual(row["fill_shares"], 0)
            risk = db.execute(
                "select event_type, severity, details_json from risk_events order by id desc limit 1"
            ).fetchone()
            self.assertEqual(risk["event_type"], "live_buy_balance_mismatch")
            details = json.loads(risk["details_json"])
            self.assertAlmostEqual(details["reported_fill_shares"], 12.5)
            self.assertAlmostEqual(details["actual_balance_delta"], 0.0)

    def test_execute_live_buy_caps_fill_to_observed_token_balance_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            client = FakeClobClient(
                reconcile_payload=None,
                buy_response={
                    "orderID": "buy-1",
                    "status": "matched",
                    "makingAmount": "5000000",
                    "takingAmount": "12500000",
                },
                token_balances={"yes25": [10.0, 15.0]},
            )

            result = execute_live_buy(
                db,
                client,
                token_id="yes25",
                side="YES",
                size_usd=5,
                asks=[(0.40, 100)],
                reason="test live buy",
                max_price=0.40,
                min_fill_usd=5,
                order_cap_usd=5,
                label="25C",
            )

            self.assertEqual(result.status, "filled")
            self.assertAlmostEqual(result.shares, 5.0)
            self.assertAlmostEqual(result.fill_size_usd, 2.0)
            pos = get_live_position(db, "yes25")
            self.assertIsNotNone(pos)
            self.assertAlmostEqual(pos["net_shares"], 5.0)
            self.assertAlmostEqual(pos["avg_price"], 0.40)

    def test_execute_live_buy_keeps_submitted_when_reconcile_and_response_are_unfilled(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            client = FakeClobClient(
                reconcile_payload=None,
                buy_response={"orderID": "buy-1", "status": "submitted"},
            )

            result = execute_live_buy(
                db,
                client,
                token_id="yes25",
                side="YES",
                size_usd=5,
                asks=[(0.40, 100)],
                reason="test live buy",
                max_price=0.40,
                min_fill_usd=5,
                order_cap_usd=5,
                label="25C",
            )

            self.assertEqual(result.status, "submitted")
            self.assertIsNone(get_live_position(db, "yes25"))

    def test_reconcile_submitted_live_order_applies_late_order_fill_to_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            order_id = db.execute(
                """
                insert into live_orders (
                    created_at_utc, submitted_at_utc, outcome_id, label, side, action,
                    clob_order_id, order_type, status, requested_size_usd, limit_price,
                    reason, raw_request_json, raw_response_json, raw_reconcile_json
                )
                values (
                    '2026-05-08T01:00:00+00:00', '2026-05-08T01:00:00+00:00',
                    'yes25', '25C', 'BUY_YES', 'BUY', 'buy-1', 'FAK', 'submitted',
                    5.0, 0.40, 'late live buy', '{}', ?, '{}'
                )
                """,
                (json.dumps({"orderID": "buy-1", "status": "submitted"}),),
            ).lastrowid
            db.commit()
            row = db.execute("select * from live_orders where id = ?", (order_id,)).fetchone()

            result = reconcile_submitted_live_order(db, FakeClobClient(), row)

            self.assertEqual(result.status, "filled")
            pos = get_live_position(db, "yes25")
            self.assertIsNotNone(pos)
            self.assertAlmostEqual(pos["net_shares"], 12.5)

    def test_reconcile_unknown_fill_applies_trade_history_fill_to_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            order_id = db.execute(
                """
                insert into live_orders (
                    created_at_utc, submitted_at_utc, outcome_id, label, side, action,
                    clob_order_id, order_type, status, requested_size_usd, limit_price,
                    reason, raw_request_json, raw_response_json, raw_reconcile_json
                )
                values (
                    '2026-05-08T01:00:00+00:00', '2026-05-08T01:00:00+00:00',
                    'yes25', '25C', 'BUY_YES', 'BUY', 'buy-1', 'FAK', 'unknown_fill',
                    5.0, 0.40, 'late live buy', '{}', ?, '{}'
                )
                """,
                (json.dumps({"orderID": "buy-1", "status": "matched"}),),
            ).lastrowid
            db.commit()
            row = db.execute("select * from live_orders where id = ?", (order_id,)).fetchone()
            client = FakeClobClient(
                reconcile_payload={"order_id": "buy-1", "token_id": "yes25", "status": "unknown"},
                trades=[
                    {
                        "taker_order_id": "buy-1",
                        "asset_id": "yes25",
                        "size": "12500000",
                        "price": "0.4",
                    }
                ],
            )

            result = reconcile_submitted_live_order(db, client, row)

            self.assertEqual(result.status, "filled")
            pos = get_live_position(db, "yes25")
            self.assertIsNotNone(pos)
            self.assertAlmostEqual(pos["net_shares"], 12.5)

    def test_rebuild_live_positions_restores_missing_position_from_filled_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            db.execute(
                """
                insert into live_orders (
                    created_at_utc, submitted_at_utc, reconciled_at_utc,
                    outcome_id, label, side, action, clob_order_id, order_type,
                    status, requested_size_usd, limit_price, fill_price,
                    fill_size_usd, fill_shares, reason, raw_request_json,
                    raw_response_json, raw_reconcile_json
                )
                values (
                    '2026-05-08T01:00:00+00:00',
                    '2026-05-08T01:00:00+00:00',
                    '2026-05-08T01:00:01+00:00',
                    'yes25', '25C', 'BUY_YES', 'BUY', 'buy-1', 'FAK',
                    'filled', 5.0, 0.40, 0.40, 5.0, 12.5,
                    'filled live buy', '{}', '{}', '{}'
                )
                """
            )
            db.commit()

            rebuilt = rebuild_live_positions_from_filled_orders(db)

            self.assertEqual(rebuilt, 1)
            pos = get_live_position(db, "yes25")
            self.assertIsNotNone(pos)
            self.assertAlmostEqual(pos["net_shares"], 12.5)
            self.assertAlmostEqual(pos["avg_price"], 0.40)

    def test_polymarket_reconcile_order_handles_empty_lookup(self):
        client = PolymarketClobClient.__new__(PolymarketClobClient)
        client._client = EmptyOrderLookupClient()

        result = client.reconcile_order("order-1", "yes25")

        self.assertEqual(result["order_id"], "order-1")
        self.assertEqual(result["token_id"], "yes25")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(client._client.order_id, "order-1")

    def test_polymarket_balance_uses_typed_params_and_converts_wei(self):
        client = PolymarketClobClient.__new__(PolymarketClobClient)
        client._client = StrictBalanceClient()
        client._signature_type = 2

        self.assertAlmostEqual(client.balance_usd(), 5.0)
        self.assertTrue(client.allowance_ok())
        self.assertEqual(len(client._client.params), 2)

    def test_plural_allowances_are_required_when_present(self):
        client = PolymarketClobClient.__new__(PolymarketClobClient)
        client._signature_type = 2

        class ZeroAllowances:
            def get_balance_allowance(self, _params):
                return {"balance": "5000000", "allowances": {"a": "0", "b": "0"}}

        client._client = ZeroAllowances()
        self.assertFalse(client.allowance_ok())

    def test_market_order_options_use_market_metadata(self):
        client = PolymarketClobClient.__new__(PolymarketClobClient)
        client._client = MarketMetadataClient()
        client._signature_type = 2

        options = client._order_options("token")

        self.assertEqual(options.tick_size, "0.001")
        self.assertTrue(options.neg_risk)
        self.assertEqual(client._client.tick_size_token_id, "token")
        self.assertEqual(client._client.neg_risk_token_id, "token")

    def test_v2_market_buy_floors_usdc_amount_to_cents(self):
        fake_module = types.SimpleNamespace(
            MarketOrderArgs=lambda **kwargs: types.SimpleNamespace(**kwargs),
            PartialCreateOrderOptions=lambda **kwargs: types.SimpleNamespace(**kwargs),
            OrderType=types.SimpleNamespace(FAK="FAK"),
            Side=types.SimpleNamespace(BUY="BUY", SELL="SELL"),
        )
        client = PolymarketClobClient.__new__(PolymarketClobClient)
        client._client = V2MarketBuyClient()
        client._signature_type = 3

        with patch.dict("sys.modules", {"py_clob_client_v2": fake_module}):
            response = client._post_v2_market_buy("token", 0.47, 5.009)

        self.assertEqual(response["orderID"], "order-1")
        self.assertEqual(client._client.order_args.amount, 5.0)
        self.assertEqual(client._client.order_args.price, 0.47)
        self.assertEqual(client._client.order_args.order_type, "FAK")
        self.assertEqual(client._client.order_options.tick_size, "0.001")
        self.assertTrue(client._client.order_options.neg_risk)

    def test_floor_decimal_avoids_float_rounding_up(self):
        self.assertEqual(_floor_decimal(5.009, "0.01"), 5.0)
        self.assertEqual(_floor_decimal(5.999, "0.01"), 5.99)

    def test_fill_values_uses_default_cost_when_reconcile_omits_amount(self):
        price, cost, shares = _fill_values(
            {"status": "filled", "matched_size": "11.90476", "price": "0"},
            0.42,
            5.0,
            11.90476,
        )

        self.assertAlmostEqual(price, 0.42, places=6)
        self.assertAlmostEqual(cost, 5.0)
        self.assertAlmostEqual(shares, 11.90476)

    def test_preflight_returns_failure_instead_of_raising_on_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            config = LiveConfig(
                trading_mode="live",
                private_key="0xabc",
                signature_type=2,
                funder_address="0xfunder",
                api_key="api",
                api_secret="secret",
                api_passphrase="passphrase",
            )

            result = preflight_live(db, TimeoutPreflightClient(), config)

            self.assertFalse(result.ok)
            self.assertIn("balance/allowance check failed", result.reason)

    def test_preflight_decodes_sub_dollar_raw_balance_as_micro_usdc(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            config = LiveConfig(
                trading_mode="live",
                private_key="0xabc",
                signature_type=3,
                funder_address="0xfunder",
                api_key="api",
                api_secret="secret",
                api_passphrase="passphrase",
            )
            client = RawBalanceAllowanceClient(
                {
                    "balance": "7366",
                    "allowances": {
                        "0xexchange": (
                            "115792089237316195423570985008687907853269984665640564039457"
                            "584007913129639935"
                        )
                    },
                }
            )

            result = preflight_live(db, client, config, required_balance_usd=3.95)

            self.assertFalse(result.ok)
            self.assertAlmostEqual(result.balance_usd, 0.007366)
            self.assertEqual(result.reason, "insufficient balance")

    def test_preflight_uses_required_scheduler_balance_not_manual_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            config = LiveConfig(
                trading_mode="live",
                private_key="0xabc",
                signature_type=3,
                funder_address="0xfunder",
                api_key="api",
                api_secret="secret",
                api_passphrase="passphrase",
            )
            client = RawBalanceAllowanceClient(
                {
                    "balance": "10000000",
                    "allowances": {
                        "0xexchange": (
                            "115792089237316195423570985008687907853269984665640564039457"
                            "584007913129639935"
                        )
                    },
                }
            )

            result = preflight_live(db, client, config, required_balance_usd=20.0)

            self.assertFalse(result.ok)
            self.assertAlmostEqual(result.balance_usd, 10.0)
            self.assertEqual(result.reason, "insufficient balance")

    def test_preflight_can_skip_entry_capacity_for_exit_only_scheduler_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            config = LiveConfig(
                trading_mode="live",
                private_key="0xabc",
                signature_type=3,
                funder_address="0xfunder",
                api_key="api",
                api_secret="secret",
                api_passphrase="passphrase",
            )
            client = RawBalanceAllowanceClient({"balance": "4000000", "allowance": "0"})

            result = preflight_live(
                db,
                client,
                config,
                required_balance_usd=20.0,
                require_entry_capacity=False,
            )

            self.assertTrue(result.ok)
            self.assertAlmostEqual(result.balance_usd, 4.0)
            self.assertEqual(result.reason, "ok")

    def test_preflight_can_skip_entry_block_for_exit_only_scheduler_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            set_live_setting(db, "block_new_entries", True)
            config = LiveConfig(
                trading_mode="live",
                private_key="0xabc",
                signature_type=3,
                funder_address="0xfunder",
                api_key="api",
                api_secret="secret",
                api_passphrase="passphrase",
            )
            client = RawBalanceAllowanceClient({"balance": "4000000", "allowance": "0"})

            result = preflight_live(
                db,
                client,
                config,
                required_balance_usd=20.0,
                require_entry_capacity=False,
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.reason, "ok")

    def test_stale_submitted_order_watchdog_freezes_new_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            store_live_order(
                db,
                outcome_id="yes25",
                side="BUY_YES",
                action="BUY",
                status="submitted",
                clob_order_id="order-1",
                requested_size_usd=5.0,
                limit_price=0.40,
            )
            db.execute(
                """
                update live_orders
                set submitted_at_utc = '2026-05-08T01:00:00+00:00'
                where clob_order_id = 'order-1'
                """
            )
            db.commit()

            frozen = freeze_new_entries_for_stale_submitted_orders(
                db,
                now=datetime(2026, 5, 8, 1, 5, tzinfo=timezone.utc),
                max_age_seconds=60,
            )

            self.assertEqual(frozen, 1)
            self.assertTrue(live_setting_enabled(db, "block_new_entries"))
            risk = db.execute(
                "select event_type, severity from risk_events order by id desc limit 1"
            ).fetchone()
            self.assertEqual(risk["event_type"], "live_stale_submitted_orders")
            self.assertEqual(risk["severity"], "critical")

    def test_execute_live_buy_blocks_when_kill_switch_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            set_live_setting(db, "block_new_entries", True)
            client = FakeClobClient()

            result = execute_live_buy(
                db,
                client,
                token_id="yes25",
                side="YES",
                size_usd=5,
                asks=[(0.40, 100)],
                reason="test live buy",
                max_price=0.40,
                min_fill_usd=5,
                order_cap_usd=5,
                label="25C",
            )

            self.assertEqual(result.status, "blocked")
            self.assertEqual(client.buys, [])

    def test_execute_live_buy_blocks_future_entries_after_three_insufficient_balance_errors(self):
        class InsufficientBalanceClient(FakeClobClient):
            def buy_fak(self, token_id, price, size_usd):
                raise RuntimeError(
                    "not enough balance / allowance: the balance is not enough -> "
                    "balance: 7366, order amount: 3950000"
                )

        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            client = InsufficientBalanceClient()

            for attempt in range(1, 4):
                result = execute_live_buy(
                    db,
                    client,
                    token_id=f"yes{attempt}",
                    side="YES",
                    size_usd=5,
                    asks=[(0.40, 100)],
                    reason="test live buy",
                    max_price=0.40,
                    min_fill_usd=5,
                    order_cap_usd=5,
                    label=f"{attempt}C",
                )

                self.assertEqual(result.status, "error")
                self.assertEqual(
                    get_live_setting(db, "insufficient_balance_error_count"),
                    str(attempt),
                )
                self.assertEqual(live_setting_enabled(db, "block_new_entries"), attempt == 3)

    def test_execute_live_buy_resets_insufficient_balance_counter_after_success(self):
        class InsufficientBalanceClient(FakeClobClient):
            def buy_fak(self, token_id, price, size_usd):
                raise RuntimeError(
                    "not enough balance / allowance: the balance is not enough -> "
                    "balance: 7366, order amount: 3950000"
                )

        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)

            for token_id in ("yes25", "yes26"):
                execute_live_buy(
                    db,
                    InsufficientBalanceClient(),
                    token_id=token_id,
                    side="YES",
                    size_usd=5,
                    asks=[(0.40, 100)],
                    reason="test live buy",
                    max_price=0.40,
                    min_fill_usd=5,
                    order_cap_usd=5,
                    label="25C",
                )

            result = execute_live_buy(
                db,
                FakeClobClient(),
                token_id="yes27",
                side="YES",
                size_usd=5,
                asks=[(0.40, 100)],
                reason="test live buy",
                max_price=0.40,
                min_fill_usd=5,
                order_cap_usd=5,
                label="27C",
            )

            self.assertEqual(result.status, "filled")
            self.assertEqual(get_live_setting(db, "insufficient_balance_error_count"), "0")
            self.assertFalse(live_setting_enabled(db, "block_new_entries"))

    def test_execute_live_buy_records_latency_stages_for_event_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            client = FakeClobClient()

            result = execute_live_buy(
                db,
                client,
                token_id="yes25",
                side="YES",
                size_usd=5,
                asks=[(0.40, 100)],
                reason="test live buy",
                max_price=0.40,
                min_fill_usd=5,
                order_cap_usd=5,
                label="25C",
                event_type="aws_actual_transition",
                event_key="actual:2026-05-08:max",
            )

            self.assertEqual(result.status, "filled")
            stages = [
                row["stage"]
                for row in latency_stages_for_event(db, "actual:2026-05-08:max")
            ]
            self.assertEqual(
                stages,
                ["order_submitted", "clob_ack", "fill_matched", "fill_confirmed"],
            )

    def test_execute_live_buy_decision_to_submit_latency_under_100ms_with_fake_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            event_key = "actual:2026-05-08:max"
            record_latency_stage(
                db,
                event_key,
                "decision_started",
                100.0,
                "aws_actual_transition",
            )
            client = FakeClobClient()

            with patch("whenitrains.live.time.monotonic", side_effect=[100.05, 100.08, 100.09, 100.10]):
                result = execute_live_buy(
                    db,
                    client,
                    token_id="yes25",
                    side="YES",
                    size_usd=5,
                    asks=[(0.40, 100)],
                    reason="test live buy",
                    max_price=0.40,
                    min_fill_usd=5,
                    order_cap_usd=5,
                    label="25C",
                    event_type="aws_actual_transition",
                    event_key=event_key,
                )

            self.assertEqual(result.status, "filled")
            summary = latency_duration_summary(db, "decision_started", "order_submitted")
            self.assertEqual(summary["count"], 1)
            self.assertLessEqual(summary["p95_seconds"], 0.1)

    def test_find_live_position_drifts_compares_local_to_clob_sellable_balance(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            upsert_live_position(db, "yes25", 12.5, 0.40, 0.0)
            upsert_live_position(db, "yes26", 8.0, 0.50, 0.0)
            upsert_live_position(db, "yes27", 5.0, 0.60, 0.0)
            client = FakeClobClient(
                token_balances={
                    "yes25": 12.5,
                    "yes26": 7.0,
                    "yes27": None,
                }
            )

            drifts = find_live_position_drifts(db, client, tolerance_shares=0.01)

            self.assertEqual(len(drifts), 2)
            self.assertEqual(drifts[0].token_id, "yes26")
            self.assertAlmostEqual(drifts[0].local_shares, 8.0)
            self.assertAlmostEqual(drifts[0].clob_sellable_shares, 7.0)
            self.assertAlmostEqual(drifts[0].drift_shares, 1.0)
            self.assertEqual(drifts[1].token_id, "yes27")
            self.assertIsNone(drifts[1].clob_sellable_shares)

    def test_find_live_position_drifts_ignores_balances_inside_tolerance(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            upsert_live_position(db, "yes25", 12.5, 0.40, 0.0)
            client = FakeClobClient(token_balances={"yes25": 12.495})

            drifts = find_live_position_drifts(db, client, tolerance_shares=0.01)

            self.assertEqual(drifts, [])

    def test_repair_live_position_drifts_applies_lower_clob_balance_adjustment(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            upsert_live_position(db, "yes25", 12.5, 0.40, 0.0)
            client = FakeClobClient(token_balances={"yes25": 7.0})
            drifts = find_live_position_drifts(db, client, tolerance_shares=0.01)

            repaired = repair_live_position_drifts(db, drifts, event_key="watchdog")

            self.assertEqual(repaired, 1)
            position = get_live_position(db, "yes25")
            self.assertAlmostEqual(position["net_shares"], 7.0)
            self.assertAlmostEqual(position["realized_pnl"], -2.2)
            adjustment = db.execute(
                """
                select side, action, status, fill_shares, event_type, event_key
                from live_orders
                where side = 'RECONCILE_SELL'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(adjustment["action"], "SELL")
            self.assertEqual(adjustment["status"], "filled")
            self.assertAlmostEqual(adjustment["fill_shares"], 5.5)
            self.assertEqual(adjustment["event_type"], "live_position_drift_repair")
            self.assertEqual(adjustment["event_key"], "watchdog")

    def test_reconcile_pending_live_orders_applies_fills_and_rebuilds_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            store_live_order(
                db,
                outcome_id="yes25",
                side="BUY_YES",
                action="BUY",
                status="submitted",
                clob_order_id="order-1",
                requested_size_usd=5.0,
                limit_price=0.40,
                raw_response={"orderID": "order-1", "status": "submitted"},
            )
            client = FakeClobClient()

            result = reconcile_pending_live_orders(db, client)

            self.assertEqual(result.orders_checked, 1)
            self.assertEqual(result.orders_filled, 1)
            self.assertEqual(result.rebuilt_positions, 1)
            pos = get_live_position(db, "yes25")
            self.assertAlmostEqual(pos["net_shares"], 12.5)
            order = db.execute(
                "select status, fill_shares from live_orders where clob_order_id = 'order-1'"
            ).fetchone()
            self.assertEqual(order["status"], "filled")
            self.assertAlmostEqual(order["fill_shares"], 12.5)

    def test_enforce_live_kill_switch_exits_cancels_orders_and_sells_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            set_live_setting(db, "cancel_open_orders_and_exit_positions", True)
            upsert_live_position(db, "yes25", 12.5, 0.40, 0.0)
            store_orderbook(
                db,
                "yes25",
                OrderBook(
                    "yes25",
                    bids=[(0.45, 100)],
                    asks=[(0.46, 100)],
                    tick_size=0.01,
                    min_order_size=5,
                ),
            )
            client = FakeClobClient()

            result = enforce_live_kill_switch_exits(db, client, event_key="kill-switch")

            self.assertEqual(result.cancel_all_status, "ok")
            self.assertEqual(result.sells_attempted, 1)
            self.assertEqual(result.sells_filled, 1)
            self.assertEqual(client.cancel_all_calls, 1)
            self.assertEqual(client.sells, [("yes25", 0.45, 12.5)])
            pos = get_live_position(db, "yes25")
            self.assertAlmostEqual(pos["net_shares"], 0.0)
            order = db.execute(
                """
                select event_type, event_key, reason
                from live_orders
                where action = 'SELL'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(order["event_type"], "live_kill_switch")
            self.assertEqual(order["event_key"], "kill-switch")
            self.assertEqual(order["reason"], "live kill switch exit")

    def test_execute_live_sell_closes_position_from_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            client = FakeClobClient()
            execute_live_buy(
                db,
                client,
                token_id="yes25",
                side="YES",
                size_usd=5,
                asks=[(0.40, 100)],
                reason="test live buy",
                max_price=0.40,
                min_fill_usd=5,
                order_cap_usd=5,
                label="25C",
            )

            result = execute_live_sell(
                db,
                client,
                token_id="yes25",
                bids=[(0.45, 100)],
                reason="test live sell",
                label="25C",
            )

            self.assertEqual(result.status, "filled")
            self.assertEqual(client.sells, [("yes25", 0.45, 12.5)])
            pos = get_live_position(db, "yes25")
            self.assertAlmostEqual(pos["net_shares"], 0.0)
            self.assertGreater(pos["realized_pnl"], 0)

    def test_execute_live_sell_floors_submitted_shares_to_exchange_precision(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            upsert_live_position(db, "yes25", 12.345, 0.40, 0.0)
            client = FakeClobClient()

            result = execute_live_sell(
                db,
                client,
                token_id="yes25",
                bids=[(0.45, 100)],
                reason="test live sell",
                label="25C",
            )

            self.assertEqual(result.status, "filled")
            self.assertEqual(client.sells, [("yes25", 0.45, 12.34)])
            pos = get_live_position(db, "yes25")
            self.assertAlmostEqual(pos["net_shares"], 0.005)

    def test_execute_live_sell_caps_to_clob_sellable_balance(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            upsert_live_position(db, "yes27", 372.661541, 0.19, 0.0)
            client = FakeClobClient(token_balances={"yes27": 255.361958})

            result = execute_live_sell(
                db,
                client,
                token_id="yes27",
                bids=[(0.03, 1000)],
                reason="position invalidated by hourly forecast",
                label="27°C or higher",
            )

            self.assertEqual(result.status, "filled")
            self.assertEqual(client.sells, [("yes27", 0.03, 255.36)])
            pos = get_live_position(db, "yes27")
            self.assertAlmostEqual(pos["net_shares"], 0.001958)
            adjustment = db.execute(
                """
                select side, action, status, fill_shares
                from live_orders
                where side = 'RECONCILE_SELL'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(adjustment["action"], "SELL")
            self.assertEqual(adjustment["status"], "filled")
            self.assertAlmostEqual(adjustment["fill_shares"], 117.299583)
            risk = db.execute(
                "select event_type, severity, details_json from risk_events order by id desc limit 1"
            ).fetchone()
            self.assertEqual(risk["event_type"], "live_position_balance_mismatch")
            self.assertEqual(risk["severity"], "warning")
            details = json.loads(risk["details_json"])
            self.assertAlmostEqual(details["local_shares"], 372.661541)
            self.assertAlmostEqual(details["clob_sellable_shares"], 255.361958)

    def test_execute_live_sell_records_balance_adjustment_for_missing_local_shares(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            store_live_order(
                db,
                outcome_id="yes27",
                label="27°C or higher",
                side="BUY_YES",
                action="BUY",
                status="filled",
                fill_price=0.20,
                fill_size_usd=20.0,
                fill_shares=100.0,
                reason="test buy",
            )
            upsert_live_position(db, "yes27", 100.0, 0.20, 0.0)
            client = FakeClobClient(token_balances={"yes27": 0.0})

            result = execute_live_sell(
                db,
                client,
                token_id="yes27",
                bids=[(0.03, 1000)],
                reason="position invalidated by hourly forecast",
                label="27°C or higher",
            )

            self.assertEqual(result.status, "rejected")
            self.assertEqual(result.reason, "no sellable token balance")
            self.assertEqual(client.sells, [])
            pos = get_live_position(db, "yes27")
            self.assertAlmostEqual(pos["net_shares"], 0.0)
            rows = db.execute(
                """
                select side, action, status, fill_price, fill_size_usd, fill_shares, reason
                from live_orders
                order by id asc
                """
            ).fetchall()
            self.assertEqual(rows[1]["side"], "RECONCILE_SELL")
            self.assertEqual(rows[1]["action"], "SELL")
            self.assertEqual(rows[1]["status"], "filled")
            self.assertEqual(rows[1]["fill_price"], 0.0)
            self.assertEqual(rows[1]["fill_size_usd"], 0.0)
            self.assertEqual(rows[1]["fill_shares"], 100.0)
            self.assertIn("CLOB sellable balance lower than local position", rows[1]["reason"])
            self.assertEqual(active_live_positions(db)["yes27"]["net_shares"], 0.0)

    def test_rebuild_live_positions_includes_zero_proceeds_balance_adjustments(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            store_live_order(
                db,
                outcome_id="yes27",
                label="27°C or higher",
                side="BUY_YES",
                action="BUY",
                status="filled",
                fill_price=0.20,
                fill_size_usd=20.0,
                fill_shares=100.0,
                reason="test buy",
            )
            store_live_order(
                db,
                outcome_id="yes27",
                label="27°C or higher",
                side="RECONCILE_SELL",
                action="SELL",
                status="filled",
                fill_price=0.0,
                fill_size_usd=0.0,
                fill_shares=100.0,
                reason="CLOB sellable balance lower than local position",
            )

            rebuilt = rebuild_live_positions_from_filled_orders(db)

            self.assertEqual(rebuilt, 1)
            pos = get_live_position(db, "yes27")
            self.assertAlmostEqual(pos["net_shares"], 0.0)
            self.assertAlmostEqual(pos["realized_pnl"], -20.0)

    def test_live_dashboard_stats_separate_from_paper(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            stats = live_dashboard_stats(db)

            self.assertEqual(stats["mode"], "live")
            self.assertEqual(stats["open_positions"], 0)
            self.assertFalse(stats["block_new_entries"])


if __name__ == "__main__":
    unittest.main()
