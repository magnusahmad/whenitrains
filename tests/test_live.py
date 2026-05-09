import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from whenitrains.live import (
    LiveConfig,
    PolymarketClobClient,
    execute_live_buy,
    execute_live_sell,
    reconcile_submitted_live_order,
    rebuild_live_positions_from_filled_orders,
    _floor_decimal,
    _fill_values,
    load_live_config,
    preflight_live,
)
from whenitrains.storage import (
    connect,
    get_live_position,
    get_live_setting,
    live_setting_enabled,
    live_dashboard_stats,
    live_total_open_exposure,
    migrate,
    set_live_setting,
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

    def token_balance(self, token_id):
        return self.token_balances.get(token_id)

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
            self.assertAlmostEqual(pos["net_shares"], 117.301541)
            risk = db.execute(
                "select event_type, severity, details_json from risk_events order by id desc limit 1"
            ).fetchone()
            self.assertEqual(risk["event_type"], "live_position_balance_mismatch")
            self.assertEqual(risk["severity"], "warning")
            details = json.loads(risk["details_json"])
            self.assertAlmostEqual(details["local_shares"], 372.661541)
            self.assertAlmostEqual(details["clob_sellable_shares"], 255.361958)

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
