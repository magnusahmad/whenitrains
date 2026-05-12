import tempfile
import unittest
from sqlite3 import ProgrammingError
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from whenitrains.hko import HKT, HkoCurrentTemperature, HkoForecast, HkoObservation, OcfForecastSample
from whenitrains.execution_scheduler import CandidateAction
from whenitrains.orderbook_cache import OrderBookCache
from whenitrains.markets import parse_outcome_label
from whenitrains.polymarket import OrderBook, Outcome, TemperatureMarket
from whenitrains.runner import (
    process_actual_entries,
    process_all_forecast_entries,
    process_forecast_entries,
    process_forecast_value_entry,
    process_open_position_exits,
    process_forecast_position_exits,
    render_dashboard,
    run_live_tick,
    run_paper_tick,
)
from whenitrains.storage import (
    connect,
    migrate,
    store_hko_forecasts,
    store_hko_current_temperature,
    store_hko_observation,
    store_ocf_forecast_samples,
    store_orderbook,
    store_polymarket_event,
    store_raw_snapshot,
    upsert_live_position,
)


class _FakeLiveClient:
    def __init__(self):
        self.buys = []
        self.sells = []
        self.token_balances = {}

    def signer_address(self):
        return "0xsigner"

    def balance_usd(self):
        return 100.0

    def allowance_ok(self):
        return True

    def buy_fak(self, token_id, price, size_usd):
        self.buys.append((token_id, price, size_usd))
        return {"orderID": "buy-1", "status": "matched"}

    def sell_fak(self, token_id, price, shares):
        self.sells.append((token_id, price, shares))
        return {"orderID": "sell-1", "status": "matched"}

    def token_balance(self, token_id):
        return self.token_balances.get(token_id)

    def reconcile_order(self, order_id, token_id):
        return {"order_id": order_id, "token_id": token_id, "status": "filled"}

    def trades_for_order(self, order_id, token_id):
        return []


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self._opened_dbs = []
        original_connect = connect

        def tracked_connect(*args, **kwargs):
            db = original_connect(*args, **kwargs)
            self._opened_dbs.append(db)
            return db

        self._connect_patcher = patch(f"{__name__}.connect", tracked_connect)
        self._connect_patcher.start()
        self.addCleanup(self._connect_patcher.stop)
        self.addCleanup(self._close_opened_dbs)

    def _close_opened_dbs(self):
        for db in reversed(self._opened_dbs):
            try:
                db.close()
            except ProgrammingError:
                pass

    def test_forecast_change_buys_stale_affected_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            _store_forecast(db, 28, "2026-05-04T00:45:00+08:00")
            _store_forecast(db, 29, "2026-05-04T01:45:00+08:00")
            _store_book_pair(db, "yes29", old_ask=0.39, new_ask=0.395)
            _store_book_pair(db, "no29", old_ask=0.60, new_ask=0.60)

            result = process_forecast_entries(db, date(2026, 5, 4), today_hkt=date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 1)
            position = db.execute(
                "select net_shares from paper_positions where outcome_id = 'yes29'"
            ).fetchone()
            self.assertIsNotNone(position)
            self.assertGreater(position["net_shares"], 0)
            decision = db.execute(
                "select status from paper_decisions where action = 'BUY' order by id desc limit 1"
            ).fetchone()
            self.assertEqual(decision["status"], "filled")

    def test_forecast_change_uses_candidate_execution_bridge(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            _store_forecast(db, 28, "2026-05-04T00:45:00+08:00")
            _store_forecast(db, 29, "2026-05-04T01:45:00+08:00")
            _store_book_pair(db, "yes29", old_ask=0.39, new_ask=0.395)
            _store_book_pair(db, "no29", old_ask=0.60, new_ask=0.60)
            bridge_calls = []

            def bridge(actions, executor):
                bridge_calls.append(actions)
                return [
                    CandidateAction(
                        action.candidate_key,
                        action.conflict_keys,
                        lambda action=action: executor(action),
                    )
                    for action in actions
                ]

            with patch("whenitrains.runner.executable_candidate_actions", side_effect=bridge):
                result = process_forecast_entries(
                    db, date(2026, 5, 4), today_hkt=date(2026, 5, 4)
                )

            self.assertEqual(result.buys_filled, 1)
            self.assertEqual(len(bridge_calls), 1)
            action = bridge_calls[0][0]
            self.assertEqual(action.intent, "buy_forecast_change_yes")
            self.assertEqual(action.token_id, "yes29")
            self.assertEqual(action.side, "BUY_YES")
            self.assertTrue(action.candidate_key.startswith("forecast_change:2026-05-04:"))
            self.assertIn("token:yes29", action.conflict_keys)
            self.assertIn("risk:entry_budget", action.conflict_keys)

    def test_forecast_entries_use_prefiltered_ladder_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            _store_forecast(db, 28, "2026-05-04T00:45:00+08:00")
            _store_forecast(db, 29, "2026-05-04T01:45:00+08:00")
            _store_book_pair(db, "yes29", old_ask=0.39, new_ask=0.395)
            _store_book_pair(db, "no29", old_ask=0.60, new_ask=0.60)
            rows = list(db.execute(
                """
                select o.id, o.market_id, o.polymarket_market_id, o.label,
                       o.predicate_type, o.predicate_value_c, o.yes_token_id, o.no_token_id,
                       m.target_date_hkt, m.slug
                from outcomes o
                join markets m on m.id = o.market_id
                order by o.id
                """
            ))

            with patch(
                "whenitrains.runner.list_outcomes_for_date",
                side_effect=AssertionError("forecast hot path should use prefiltered ladder rows"),
            ):
                result = process_forecast_entries(
                    db,
                    date(2026, 5, 4),
                    today_hkt=date(2026, 5, 4),
                    ladder_rows={"highest": rows, "lowest": []},
                )

            self.assertEqual(result.buys_filled, 1)

    def test_lowest_forecast_change_buys_stale_new_minimum_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            _store_lowest_market(
                db,
                [
                    Outcome(
                        market_id="low29",
                        label="29°C",
                        predicate=parse_outcome_label("29°C"),
                        yes_token_id="yes-low29",
                        no_token_id="no-low29",
                    )
                ],
            )
            _store_forecast_range(db, low=30, high=33, update_time="2026-05-04T00:45:00+08:00")
            _store_forecast_range(db, low=29, high=33, update_time="2026-05-04T01:45:00+08:00")
            _store_book_pair(db, "yes-low29", old_ask=0.39, new_ask=0.395)

            result = process_forecast_entries(db, date(2026, 5, 4), today_hkt=date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 1)
            decision = db.execute(
                """
                select event_type, label, side, status
                from paper_decisions
                where action = 'BUY' and status = 'filled'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(decision["event_type"], "lowest_forecast_change")
            self.assertEqual(decision["label"], "29°C")
            self.assertEqual(decision["side"], "YES")

    def test_lowest_forecast_change_effective_low_includes_actual_min(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            _store_lowest_market(
                db,
                [
                    Outcome(
                        market_id="low24",
                        label="24°C",
                        predicate=parse_outcome_label("24°C"),
                        yes_token_id="yes-low24",
                        no_token_id="no-low24",
                    )
                ],
            )
            _store_aws_actual(db, high=29.0, low=24.7, hour=13, minute=10)
            _store_forecast_range(db, low=25.3, high=30, update_time="2026-05-04T12:11:46+08:00")
            _store_forecast_range(db, low=25.8, high=30, update_time="2026-05-04T13:11:47+08:00")
            _store_book_pair(db, "yes-low24", old_ask=0.001, new_ask=0.001)

            result = process_forecast_entries(
                db, date(2026, 5, 4), today_hkt=date(2026, 5, 4)
            )

            self.assertEqual(result.buys_filled, 0)
            self.assertIn("forecast low unchanged", result.notes)
            buy_count = db.execute(
                """
                select count(*)
                from paper_decisions
                where event_type = 'lowest_forecast_change'
                  and action = 'BUY'
                """
            ).fetchone()
            self.assertEqual(buy_count[0], 0)

    def test_forecast_change_skips_when_market_moved_more_than_twenty_cents(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            _store_forecast(db, 28, "2026-05-04T00:45:00+08:00")
            _store_forecast(db, 29, "2026-05-04T01:45:00+08:00")
            _store_book_pair(db, "yes29", old_ask=0.40, new_ask=0.61)
            _store_book_pair(db, "no29", old_ask=0.60, new_ask=0.60)

            result = process_forecast_entries(db, date(2026, 5, 4), today_hkt=date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 0)
            self.assertEqual(result.buys_missed, 1)
            self.assertIn("no stale forecast candidates", result.notes)

    def test_forecast_change_allows_twenty_cent_move_if_entry_price_is_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            _store_forecast(db, 28, "2026-05-04T00:45:00+08:00")
            _store_forecast(db, 29, "2026-05-04T01:45:00+08:00")
            _store_book_pair(db, "yes29", old_ask=0.20, new_ask=0.40)
            _store_book_pair(db, "no29", old_ask=0.60, new_ask=0.60)

            result = process_forecast_entries(db, date(2026, 5, 4), today_hkt=date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 1)
            order = db.execute(
                "select simulated_fill_price from paper_orders where status = 'filled' order by id desc limit 1"
            ).fetchone()
            self.assertEqual(order["simulated_fill_price"], 0.40)

    def test_forecast_change_effective_high_includes_actual_max(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=_threshold_risk_outcomes(),
            )
            _store_aws_actual(db, high=29.6, hour=13, minute=10)
            _store_forecast(db, 29.1, "2026-05-04T12:11:46+08:00")
            _store_forecast(db, 28.8, "2026-05-04T13:11:47+08:00")
            _store_book_pair(db, "yes28", old_ask=0.001, new_ask=0.001)

            result = process_forecast_entries(
                db, date(2026, 5, 4), today_hkt=date(2026, 5, 4)
            )

            self.assertEqual(result.buys_filled, 0)
            self.assertIn("forecast high unchanged", result.notes)
            position = db.execute(
                "select net_shares from paper_positions where outcome_id = 'yes28'"
            ).fetchone()
            self.assertIsNone(position)
            buy_count = db.execute(
                """
                select count(*)
                from paper_decisions
                where event_type = 'forecast_change'
                  and action = 'BUY'
                """
            ).fetchone()
            self.assertEqual(buy_count[0], 0)

    def test_forecast_change_d2_skips_when_entry_price_is_above_twenty_cents(self):
        with tempfile.TemporaryDirectory() as tmp:
            target_date = date(2026, 5, 6)
            db = _seed_market(Path(tmp) / "test.db", target_date=target_date)
            _store_forecast(
                db,
                28,
                "2026-05-04T00:45:00+08:00",
                forecast_date=target_date,
            )
            _store_forecast(
                db,
                29,
                "2026-05-04T01:45:00+08:00",
                forecast_date=target_date,
            )
            _store_book_pair(db, "yes29", old_ask=0.19, new_ask=0.21)
            _store_book_pair(db, "no29", old_ask=0.81, new_ask=0.81)

            result = process_forecast_entries(
                db, target_date, today_hkt=date(2026, 5, 4)
            )

            self.assertEqual(result.buys_filled, 0)
            self.assertEqual(result.buys_missed, 1)
            decision = db.execute(
                """
                select status, reason
                from paper_decisions
                where action = 'BUY'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(decision["status"], "missed")
            self.assertEqual(decision["reason"], "no ask depth at or below max price")

    def test_forecast_change_d1_skips_when_entry_price_is_above_forty_cents(self):
        with tempfile.TemporaryDirectory() as tmp:
            target_date = date(2026, 5, 5)
            db = _seed_market(Path(tmp) / "test.db", target_date=target_date)
            _store_forecast(
                db,
                28,
                "2026-05-04T00:45:00+08:00",
                forecast_date=target_date,
            )
            _store_forecast(
                db,
                29,
                "2026-05-04T01:45:00+08:00",
                forecast_date=target_date,
            )
            _store_book_pair(db, "yes29", old_ask=0.39, new_ask=0.41)
            _store_book_pair(db, "no29", old_ask=0.61, new_ask=0.61)

            result = process_forecast_entries(
                db, target_date, today_hkt=date(2026, 5, 4)
            )

            self.assertEqual(result.buys_filled, 0)
            self.assertEqual(result.buys_missed, 1)
            decision = db.execute(
                """
                select status, reason
                from paper_decisions
                where action = 'BUY'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(decision["status"], "missed")
            self.assertEqual(decision["reason"], "no ask depth at or below max price")

    def test_forecast_change_d1_buys_when_entry_price_is_at_forty_cents(self):
        with tempfile.TemporaryDirectory() as tmp:
            target_date = date(2026, 5, 5)
            db = _seed_market(Path(tmp) / "test.db", target_date=target_date)
            _store_forecast(
                db,
                28,
                "2026-05-04T00:45:00+08:00",
                forecast_date=target_date,
            )
            _store_forecast(
                db,
                29,
                "2026-05-04T01:45:00+08:00",
                forecast_date=target_date,
            )
            _store_book_pair(db, "yes29", old_ask=0.39, new_ask=0.40)
            _store_book_pair(db, "no29", old_ask=0.61, new_ask=0.61)

            result = process_forecast_entries(
                db, target_date, today_hkt=date(2026, 5, 4)
            )

            self.assertEqual(result.buys_filled, 1)
            order = db.execute(
                "select simulated_fill_price from paper_orders where status = 'filled' order by id desc limit 1"
            ).fetchone()
            self.assertEqual(order["simulated_fill_price"], 0.40)

    def test_live_forecast_change_refreshes_quote_before_buy(self):
        with tempfile.TemporaryDirectory() as tmp:
            target_date = date(2026, 5, 5)
            db = _seed_market(Path(tmp) / "test.db", target_date=target_date)
            _store_forecast(
                db,
                28,
                "2026-05-04T00:45:00+08:00",
                forecast_date=target_date,
            )
            _store_forecast(
                db,
                29,
                "2026-05-04T01:45:00+08:00",
                forecast_date=target_date,
            )
            _store_book_pair(db, "yes29", old_ask=0.39, new_ask=0.395)
            _store_book_pair(db, "no29", old_ask=0.61, new_ask=0.61)
            fresh_book = OrderBook(
                "yes29",
                bids=[(0.39, 1000)],
                asks=[(0.41, 1000)],
                tick_size=0.01,
                min_order_size=5,
            )
            client = _FakeLiveClient()

            with patch("whenitrains.runner.fetch_orderbook", return_value=fresh_book):
                result = run_live_tick(db, client, today_hkt=date(2026, 5, 4), order_cap_usd=5)

            self.assertEqual(result.buys_filled, 0)
            self.assertEqual(result.buys_missed, 1)
            self.assertEqual(client.buys, [])
            decision = db.execute(
                """
                select status, reason
                from paper_decisions
                where action = 'BUY'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(decision["status"], "missed")
            self.assertEqual(decision["reason"], "no ask depth at or below max price")

    def test_live_forecast_change_uses_fresh_websocket_book_without_rest_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            target_date = date(2026, 5, 5)
            db = _seed_market(Path(tmp) / "test.db", target_date=target_date)
            _store_forecast(
                db,
                28,
                "2026-05-04T00:45:00+08:00",
                forecast_date=target_date,
            )
            _store_forecast(
                db,
                29,
                "2026-05-04T01:45:00+08:00",
                forecast_date=target_date,
            )
            _store_book_pair(db, "yes29", old_ask=0.39, new_ask=0.395)
            _store_book_pair(db, "no29", old_ask=0.61, new_ask=0.61)
            cache = OrderBookCache(monotonic_fn=lambda: 10.0)
            cache.seed(
                OrderBook(
                    "yes29",
                    bids=[(0.37, 1000)],
                    asks=[(0.39, 1000)],
                    tick_size=0.01,
                    min_order_size=5,
                )
            )
            client = _FakeLiveClient()

            with patch("whenitrains.runner.fetch_orderbook") as fetch:
                result = run_live_tick(
                    db,
                    client,
                    today_hkt=date(2026, 5, 4),
                    order_cap_usd=5,
                    book_cache=cache,
                )

            fetch.assert_not_called()
            self.assertEqual(result.buys_filled, 1)
            self.assertEqual(client.buys, [("yes29", 0.39, 5)])

    def test_live_forecast_change_skips_when_websocket_book_cache_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            target_date = date(2026, 5, 5)
            db = _seed_market(Path(tmp) / "test.db", target_date=target_date)
            _store_forecast(
                db,
                28,
                "2026-05-04T00:45:00+08:00",
                forecast_date=target_date,
            )
            _store_forecast(
                db,
                29,
                "2026-05-04T01:45:00+08:00",
                forecast_date=target_date,
            )
            _store_book_pair(db, "yes29", old_ask=0.39, new_ask=0.395)
            _store_book_pair(db, "no29", old_ask=0.61, new_ask=0.61)
            now = [10.0]
            cache = OrderBookCache(monotonic_fn=lambda: now[0])
            cache.seed(
                OrderBook(
                    "yes29",
                    bids=[(0.37, 1000)],
                    asks=[(0.39, 1000)],
                    tick_size=0.01,
                    min_order_size=5,
                )
            )
            now[0] = 10.3
            client = _FakeLiveClient()

            with patch("whenitrains.runner.fetch_orderbook") as fetch:
                result = run_live_tick(
                    db,
                    client,
                    today_hkt=date(2026, 5, 4),
                    order_cap_usd=5,
                    book_cache=cache,
                )

            fetch.assert_not_called()
            self.assertEqual(result.buys_filled, 0)
            self.assertEqual(result.buys_missed, 1)
            self.assertEqual(client.buys, [])
            decision = db.execute(
                """
                select status, reason
                from paper_decisions
                where action = 'BUY'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(decision["status"], "missed")
            self.assertEqual(decision["reason"], "stale Polymarket orderbook cache")

    def test_forecast_change_d2_buys_when_entry_price_is_at_twenty_cents(self):
        with tempfile.TemporaryDirectory() as tmp:
            target_date = date(2026, 5, 6)
            db = _seed_market(Path(tmp) / "test.db", target_date=target_date)
            _store_forecast(
                db,
                28,
                "2026-05-04T00:45:00+08:00",
                forecast_date=target_date,
            )
            _store_forecast(
                db,
                29,
                "2026-05-04T01:45:00+08:00",
                forecast_date=target_date,
            )
            _store_book_pair(db, "yes29", old_ask=0.19, new_ask=0.20)
            _store_book_pair(db, "no29", old_ask=0.81, new_ask=0.81)

            result = process_forecast_entries(
                db, target_date, today_hkt=date(2026, 5, 4)
            )

            self.assertEqual(result.buys_filled, 1)
            order = db.execute(
                "select simulated_fill_price from paper_orders where status = 'filled' order by id desc limit 1"
            ).fetchone()
            self.assertEqual(order["simulated_fill_price"], 0.20)

    def test_forecast_change_does_not_sweep_far_above_top_ask(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            _store_forecast(db, 28, "2026-05-04T00:45:00+08:00")
            _store_forecast(db, 29, "2026-05-04T01:45:00+08:00")
            store_orderbook(
                db,
                "yes29",
                OrderBook(
                    "yes29",
                    bids=[(0.23, 1000)],
                    asks=[(0.24, 1000)],
                    tick_size=0.01,
                    min_order_size=5,
                ),
            )
            store_orderbook(
                db,
                "yes29",
                OrderBook(
                    "yes29",
                    bids=[(0.23, 1000)],
                    asks=[
                        (0.25, 63),
                        (0.26, 43),
                        (0.27, 14.68),
                        (0.28, 5),
                        (0.29, 43),
                        (0.30, 5),
                        (0.55, 200),
                    ],
                    tick_size=0.01,
                    min_order_size=5,
                ),
            )

            result = process_forecast_entries(db, date(2026, 5, 4), today_hkt=date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 1)
            order = db.execute(
                """
                select limit_price, simulated_fill_size_usd
                from paper_orders
                where status = 'filled'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertAlmostEqual(order["limit_price"], 0.30)
            self.assertLess(order["simulated_fill_size_usd"], 250.0)

    def test_forecast_change_skips_near_settled_repriced_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=_forecast_bucket_outcomes(),
            )
            _store_forecast(db, 24, "2026-05-05T13:11:46+08:00")
            _store_forecast(db, 23, "2026-05-05T15:12:19+08:00")
            _store_book_pair(db, "yes23", old_ask=0.92, new_ask=0.93)
            _store_book_pair(db, "no23", old_ask=0.10, new_ask=0.10)
            _store_book_pair(db, "yes24", old_ask=0.08, new_ask=0.07)
            _store_book_pair(db, "no24", old_ask=0.93, new_ask=0.94)

            result = process_forecast_entries(db, date(2026, 5, 4), today_hkt=date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 0)
            decision = db.execute(
                """
                select label, status, reason from paper_decisions
                where action = 'BUY'
                  and label = '23°C'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(decision["label"], "23°C")
            self.assertEqual(decision["status"], "missed")
            self.assertEqual(decision["reason"], "no ask depth at or below max price")

    def test_forecast_change_event_is_processed_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            _store_forecast(db, 28, "2026-05-04T00:45:00+08:00")
            _store_forecast(db, 29, "2026-05-04T01:45:00+08:00")
            _store_book_pair(db, "yes29", old_ask=0.39, new_ask=0.395)
            _store_book_pair(db, "no29", old_ask=0.60, new_ask=0.60)

            first = process_forecast_entries(db, date(2026, 5, 4))
            second = process_forecast_entries(db, date(2026, 5, 4))

            self.assertEqual(first.buys_filled, 1)
            self.assertEqual(second.buys_filled, 0)
            self.assertEqual(second.buys_missed, 0)
            buy_count = db.execute(
                "select count(*) from paper_decisions where action = 'BUY'"
            ).fetchone()[0]
            self.assertEqual(buy_count, 1)

    def test_forecast_change_missing_orderbooks_is_retryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            _store_forecast(db, 28, "2026-05-04T00:45:00+08:00")
            _store_forecast(db, 29, "2026-05-04T01:45:00+08:00")

            first = process_forecast_entries(
                db, date(2026, 5, 4), today_hkt=date(2026, 5, 4)
            )
            processed_count = db.execute(
                """
                select count(*)
                from paper_decisions
                where event_type = 'forecast_change'
                  and action = 'EVENT'
                  and status = 'processed'
                """
            ).fetchone()[0]
            _store_book_pair(db, "yes29", old_ask=0.39, new_ask=0.395)

            second = process_forecast_entries(
                db, date(2026, 5, 4), today_hkt=date(2026, 5, 4)
            )

            self.assertEqual(first.buys_filled, 0)
            self.assertEqual(processed_count, 0)
            self.assertEqual(second.buys_filled, 1)

    def test_forecast_change_duplicate_hko_update_is_processed_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            _store_forecast(db, 28, "2026-05-04T00:45:00+08:00")
            _store_forecast(db, 29, "2026-05-04T01:45:00+08:00")
            _store_book_pair(db, "yes29", old_ask=0.39, new_ask=0.395)

            first = process_forecast_entries(db, date(2026, 5, 4), today_hkt=date(2026, 5, 4))
            _store_forecast(db, 29, "2026-05-04T01:45:00+08:00")
            second = process_forecast_entries(db, date(2026, 5, 4), today_hkt=date(2026, 5, 4))

            self.assertEqual(first.buys_filled, 1)
            self.assertEqual(second.buys_filled, 0)
            buy_count = db.execute(
                "select count(*) from paper_decisions where action = 'BUY'"
            ).fetchone()[0]
            self.assertEqual(buy_count, 1)

    def test_run_tick_trades_future_forecast_market_but_not_actual_cross(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                target_date=date(2026, 5, 5),
                outcomes=[
                    Outcome(
                        market_id="m29",
                        label="29°C",
                        predicate=parse_outcome_label("29°C"),
                        yes_token_id="future_yes29",
                        no_token_id="future_no29",
                    )
                ],
            )
            _store_forecast(db, 28, "2026-05-04T00:45:00+08:00", forecast_date=date(2026, 5, 5))
            _store_forecast(db, 29, "2026-05-04T01:45:00+08:00", forecast_date=date(2026, 5, 5))
            _store_book_pair(db, "future_yes29", old_ask=0.39, new_ask=0.395)
            _store_book_pair(db, "future_no29", old_ask=0.60, new_ask=0.60)
            _store_observation(db, 29.0)
            _store_observation(db, 30.0)

            result = run_paper_tick(db, today_hkt=date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 1)
            position = db.execute(
                "select net_shares from paper_positions where outcome_id = 'future_yes29'"
            ).fetchone()
            self.assertIsNotNone(position)
            actual_buy_decisions = db.execute(
                """
                select count(*) from paper_decisions
                where event_type = 'actual_cross' and action = 'BUY'
                """
            ).fetchone()[0]
            self.assertEqual(actual_buy_decisions, 0)

    def test_all_forecast_entries_ignores_future_dates_without_market(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            _store_forecast(db, 28, "2026-05-04T00:45:00+08:00", forecast_date=date(2026, 5, 5))
            _store_forecast(db, 29, "2026-05-04T01:45:00+08:00", forecast_date=date(2026, 5, 5))

            result = process_all_forecast_entries(db, date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 0)
            self.assertEqual(result.notes, ("no tradeable forecast dates",))

    def test_exit_loop_holds_without_new_invalidating_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            _store_book_pair(db, "yes29", old_ask=0.40, new_ask=0.40)
            from whenitrains.paper_db import execute_paper_buy

            execute_paper_buy(
                db,
                token_id="yes29",
                side="YES",
                size_usd=100,
                asks=[(0.40, 1000)],
                max_order_usd=250,
                reason="test",
            )
            entry_time = datetime.now(timezone.utc) - timedelta(minutes=11)
            db.execute(
                "update paper_positions set updated_at_utc = ? where outcome_id = ?",
                (entry_time.isoformat(), "yes29"),
            )
            db.commit()
            store_orderbook(
                db,
                "yes29",
                OrderBook("yes29", bids=[(0.40, 1000)], asks=[(0.41, 1000)], tick_size=0.01, min_order_size=5),
            )

            result = process_open_position_exits(db)

            self.assertEqual(result.sells_filled, 0)
            position = db.execute(
                "select net_shares from paper_positions where outcome_id = 'yes29'"
            ).fetchone()
            self.assertGreater(position["net_shares"], 0)

    def test_exit_loop_does_not_count_missing_depth_when_position_is_not_invalidated(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            from whenitrains.paper_db import execute_paper_buy

            execute_paper_buy(
                db,
                token_id="yes29",
                side="YES",
                size_usd=100,
                asks=[(0.40, 1000)],
                max_order_usd=250,
                reason="test",
            )
            _store_observation(db, 29.5)
            store_orderbook(
                db,
                "yes29",
                OrderBook("yes29", bids=[], asks=[(0.41, 1000)], tick_size=0.01, min_order_size=5),
            )

            result = process_open_position_exits(db, today_hkt=date(2026, 5, 4))

            self.assertEqual(result.sells_missed, 0)
            self.assertEqual(result.notes, ())

    def test_exit_loop_takes_profit_on_peak_heating_near_upper_exact_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            from whenitrains.paper_db import execute_paper_buy

            execute_paper_buy(
                db,
                token_id="yes29",
                side="YES",
                size_usd=100,
                asks=[(0.40, 1000)],
                max_order_usd=250,
                reason="test",
            )
            _store_aws_actual(db, 29.6, hour=12)
            _store_aws_actual(db, 29.8, hour=13)
            store_orderbook(
                db,
                "yes29",
                OrderBook("yes29", bids=[(0.72, 1000)], asks=[(0.75, 1000)], tick_size=0.01, min_order_size=5),
            )

            result = process_open_position_exits(db, today_hkt=date(2026, 5, 4))

            self.assertEqual(result.sells_filled, 1)
            position = db.execute(
                "select net_shares, realized_pnl from paper_positions where outcome_id = 'yes29'"
            ).fetchone()
            self.assertAlmostEqual(position["net_shares"], 0.0)
            self.assertGreater(position["realized_pnl"], 0)
            decision = db.execute(
                """
                select event_type, status, reason, details_json
                from paper_decisions
                where action = 'SELL'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(decision["event_type"], "exit_check")
            self.assertEqual(decision["status"], "filled")
            self.assertEqual(decision["reason"], "near-boundary peak heating take profit")
            self.assertIn('"boundary_c": 30.0', decision["details_json"])

    def test_exit_loop_does_not_take_peak_boundary_profit_before_heating_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            from whenitrains.paper_db import execute_paper_buy

            execute_paper_buy(
                db,
                token_id="yes29",
                side="YES",
                size_usd=100,
                asks=[(0.40, 1000)],
                max_order_usd=250,
                reason="test",
            )
            _store_aws_actual(db, 29.6, hour=9)
            _store_aws_actual(db, 29.8, hour=10)
            store_orderbook(
                db,
                "yes29",
                OrderBook("yes29", bids=[(0.72, 1000)], asks=[(0.75, 1000)], tick_size=0.01, min_order_size=5),
            )

            result = process_open_position_exits(db, today_hkt=date(2026, 5, 4))

            self.assertEqual(result.sells_filled, 0)

    def test_exit_loop_does_not_take_peak_boundary_profit_when_not_near_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            from whenitrains.paper_db import execute_paper_buy

            execute_paper_buy(
                db,
                token_id="yes29",
                side="YES",
                size_usd=100,
                asks=[(0.40, 1000)],
                max_order_usd=250,
                reason="test",
            )
            _store_aws_actual(db, 29.5, hour=12)
            _store_aws_actual(db, 29.7, hour=13)
            store_orderbook(
                db,
                "yes29",
                OrderBook("yes29", bids=[(0.72, 1000)], asks=[(0.75, 1000)], tick_size=0.01, min_order_size=5),
            )

            result = process_open_position_exits(db, today_hkt=date(2026, 5, 4))

            self.assertEqual(result.sells_filled, 0)

    def test_exit_loop_does_not_take_peak_boundary_profit_when_actual_max_is_flat(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            from whenitrains.paper_db import execute_paper_buy

            execute_paper_buy(
                db,
                token_id="yes29",
                side="YES",
                size_usd=100,
                asks=[(0.40, 1000)],
                max_order_usd=250,
                reason="test",
            )
            _store_aws_actual(db, 29.8, hour=12)
            _store_aws_actual(db, 29.8, hour=13)
            store_orderbook(
                db,
                "yes29",
                OrderBook("yes29", bids=[(0.72, 1000)], asks=[(0.75, 1000)], tick_size=0.01, min_order_size=5),
            )

            result = process_open_position_exits(db, today_hkt=date(2026, 5, 4))

            self.assertEqual(result.sells_filled, 0)

    def test_exit_loop_settles_resolved_past_date_position_without_bid_depth(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            db.execute("update markets set status = 'resolved'")
            db.commit()
            from whenitrains.paper_db import execute_paper_buy

            execute_paper_buy(
                db,
                token_id="yes29",
                side="YES",
                size_usd=100,
                asks=[(0.40, 1000)],
                max_order_usd=250,
                reason="test",
            )
            _store_observation(db, 29.5)

            result = process_open_position_exits(db, today_hkt=date(2026, 5, 5))

            self.assertEqual(result.sells_filled, 1)
            self.assertIn("settled resolved 29°C YES @ 1.00", result.notes)
            position = db.execute(
                "select net_shares, realized_pnl from paper_positions where outcome_id = 'yes29'"
            ).fetchone()
            self.assertAlmostEqual(position["net_shares"], 0.0)
            self.assertGreater(position["realized_pnl"], 0)
            order = db.execute(
                """
                select status, simulated_fill_price, reason
                from paper_orders
                where outcome_id = 'yes29'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(order["status"], "filled")
            self.assertEqual(order["simulated_fill_price"], 1.0)
            self.assertEqual(order["reason"], "resolved market settlement")

    def test_live_tick_settles_resolved_past_date_position_locally(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            db.execute("update markets set status = 'resolved'")
            db.commit()
            upsert_live_position(db, "no29", 25.0, 0.40, 0.0)
            _store_observation(db, 29.5)
            client = _FakeLiveClient()

            result = run_live_tick(db, client, today_hkt=date(2026, 5, 5), order_cap_usd=5)

            self.assertEqual(result.sells_filled, 1)
            self.assertIn("settled resolved 29°C NO @ 0.00", result.notes)
            position = db.execute(
                "select net_shares, realized_pnl from live_positions where outcome_id = 'no29'"
            ).fetchone()
            self.assertAlmostEqual(position["net_shares"], 0.0)
            self.assertLess(position["realized_pnl"], 0)
            order = db.execute(
                """
                select side, status, fill_price, reason
                from live_orders
                where outcome_id = 'no29'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(order["side"], "SETTLEMENT")
            self.assertEqual(order["status"], "filled")
            self.assertEqual(order["fill_price"], 0.0)
            self.assertEqual(order["reason"], "resolved market settlement")

    def test_exit_loop_signposts_no_bid_depth_for_invalidated_sell(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            from whenitrains.paper_db import execute_paper_buy

            execute_paper_buy(
                db,
                token_id="yes29",
                side="YES",
                size_usd=100,
                asks=[(0.40, 1000)],
                max_order_usd=250,
                reason="test",
            )
            _store_observation(db, 30.0)
            store_orderbook(
                db,
                "yes29",
                OrderBook("yes29", bids=[], asks=[(0.41, 1000)], tick_size=0.01, min_order_size=5),
            )

            result = process_open_position_exits(db, today_hkt=date(2026, 5, 4))

            self.assertEqual(result.sells_missed, 1)
            self.assertIn(
                "sell missed 29°C YES: no bid depth (trigger=position invalidated by observed max, bid=n/a)",
                result.notes,
            )

    def test_live_exit_signposts_no_sellable_balance_for_invalidated_sell(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            upsert_live_position(db, "yes29", 25.0, 0.40, 0.0)
            _store_observation(db, 30.0)
            store_orderbook(
                db,
                "yes29",
                OrderBook("yes29", bids=[(0.10, 1000)], asks=[(0.41, 1000)], tick_size=0.01, min_order_size=5),
            )
            client = _FakeLiveClient()
            client.token_balances["yes29"] = 0.0

            result = run_live_tick(db, client, today_hkt=date(2026, 5, 4), order_cap_usd=5)

            self.assertEqual(result.sells_missed, 1)
            self.assertEqual(client.sells, [])
            self.assertIn(
                "sell missed 29°C YES: no sellable token balance (trigger=position invalidated by observed max, bid=0.100)",
                result.notes,
            )

    def test_live_exit_skips_sub_precision_dust_without_logging_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            upsert_live_position(db, "yes29", 0.003, 0.40, 0.0)
            _store_observation(db, 30.0)
            store_orderbook(
                db,
                "yes29",
                OrderBook("yes29", bids=[(0.10, 1000)], asks=[(0.41, 1000)], tick_size=0.01, min_order_size=5),
            )
            client = _FakeLiveClient()
            client.token_balances["yes29"] = 0.003

            result = run_live_tick(db, client, today_hkt=date(2026, 5, 4), order_cap_usd=5)

            self.assertEqual(result.sells_missed, 0)
            self.assertEqual(client.sells, [])
            self.assertFalse(any("sell missed" in note for note in result.notes))
            pos = db.execute("select net_shares from live_positions where outcome_id = 'yes29'").fetchone()
            self.assertAlmostEqual(pos["net_shares"], 0.003)
            live_order_count = db.execute("select count(*) from live_orders").fetchone()[0]
            self.assertEqual(live_order_count, 0)

    def test_tick_exits_invalidated_exact_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            from whenitrains.paper_db import execute_paper_buy

            execute_paper_buy(
                db,
                token_id="yes29",
                side="YES",
                size_usd=100,
                asks=[(0.40, 1000)],
                max_order_usd=250,
                reason="test",
            )
            store_orderbook(
                db,
                "yes29",
                OrderBook("yes29", bids=[(0.35, 1000)], asks=[(0.36, 1000)], tick_size=0.01, min_order_size=5),
            )
            _store_observation(db, 30.0)

            result = run_paper_tick(db, today_hkt=date(2026, 5, 4))

            self.assertEqual(result.sells_filled, 1)

    def test_open_position_exit_uses_candidate_execution_bridge(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            from whenitrains.paper_db import execute_paper_buy

            execute_paper_buy(
                db,
                token_id="yes29",
                side="YES",
                size_usd=100,
                asks=[(0.40, 1000)],
                max_order_usd=250,
                reason="test",
            )
            store_orderbook(
                db,
                "yes29",
                OrderBook("yes29", bids=[(0.35, 1000)], asks=[(0.36, 1000)], tick_size=0.01, min_order_size=5),
            )
            _store_observation(db, 30.0)
            bridge_calls = []

            def bridge(actions, executor):
                bridge_calls.append(actions)
                return [
                    CandidateAction(
                        action.candidate_key,
                        action.conflict_keys,
                        lambda action=action: executor(action),
                    )
                    for action in actions
                ]

            with patch("whenitrains.runner.executable_candidate_actions", side_effect=bridge):
                result = process_open_position_exits(db, today_hkt=date(2026, 5, 4))

            self.assertEqual(result.sells_filled, 1)
            self.assertEqual(len(bridge_calls), 1)
            action = bridge_calls[0][0]
            self.assertEqual(action.intent, "sell_exit_check")
            self.assertEqual(action.token_id, "yes29")
            self.assertEqual(action.side, "SELL")
            self.assertEqual(action.candidate_key, "exit_check:2026-05-04:yes29:sell_exit_check:yes29")
            self.assertIn("token:yes29", action.conflict_keys)
            self.assertIn("position:yes29", action.conflict_keys)

    def test_open_position_exit_uses_batched_outcome_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            from whenitrains.paper_db import execute_paper_buy

            execute_paper_buy(
                db,
                token_id="yes29",
                side="YES",
                size_usd=100,
                asks=[(0.40, 1000)],
                max_order_usd=250,
                reason="test",
            )
            store_orderbook(
                db,
                "yes29",
                OrderBook("yes29", bids=[(0.35, 1000)], asks=[(0.36, 1000)], tick_size=0.01, min_order_size=5),
            )
            _store_observation(db, 30.0)
            with patch(
                "whenitrains.runner.find_outcome_by_token",
                side_effect=AssertionError("exit hot path should use batched rows"),
            ):
                result = process_open_position_exits(
                    db,
                    today_hkt=date(2026, 5, 4),
                )

            self.assertEqual(result.sells_filled, 1)

    def test_actual_cross_buys_stale_gte_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=[
                    Outcome(
                        market_id="m30",
                        label="30°C or higher",
                        predicate=parse_outcome_label("30°C or higher"),
                        yes_token_id="yes30",
                        no_token_id="no30",
                    )
                ],
            )
            _store_forecast(db, 29, "2026-05-04T00:45:00+08:00")
            _store_observation(db, 29.0)
            _store_observation(db, 30.0)
            _store_book_pair(db, "yes30", old_ask=0.40, new_ask=0.405)
            _store_book_pair(db, "no30", old_ask=0.60, new_ask=0.60)

            result = process_actual_entries(db, date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 1)
            position = db.execute(
                "select net_shares from paper_positions where outcome_id = 'yes30'"
            ).fetchone()
            self.assertIsNotNone(position)

    def test_actual_cross_missing_orderbooks_is_retryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=[
                    Outcome(
                        market_id="m30",
                        label="30°C or higher",
                        predicate=parse_outcome_label("30°C or higher"),
                        yes_token_id="yes30",
                        no_token_id="no30",
                    )
                ],
            )
            _store_forecast(db, 29, "2026-05-04T00:45:00+08:00")
            _store_observation(db, 29.0)
            _store_observation(db, 30.0)

            first = process_actual_entries(db, date(2026, 5, 4))
            processed_count = db.execute(
                """
                select count(*)
                from paper_decisions
                where event_type = 'actual_cross'
                  and action = 'EVENT'
                  and status = 'processed'
                """
            ).fetchone()[0]
            _store_book_pair(db, "yes30", old_ask=0.40, new_ask=0.405)

            second = process_actual_entries(db, date(2026, 5, 4))

            self.assertEqual(first.buys_filled, 0)
            self.assertEqual(processed_count, 0)
            self.assertEqual(second.buys_filled, 1)

    def test_actual_cross_notes_are_deduped_when_no_bucket_cross_occurs(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=[
                    Outcome(
                        market_id="m30",
                        label="30°C or higher",
                        predicate=parse_outcome_label("30°C or higher"),
                        yes_token_id="yes30",
                        no_token_id="no30",
                    )
                ],
            )
            _store_forecast(db, 31, "2026-05-04T00:45:00+08:00")
            _store_observation(db, 28.0)
            _store_observation(db, 29.0)
            _store_observation(db, 30.0)

            result = process_actual_entries(db, date(2026, 5, 4))

            self.assertEqual(
                result.notes.count("no actual cross candidates"), 1
            )

    def test_actual_cross_buys_no_on_invalidated_lower_exact_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=[
                    Outcome(
                        market_id="m25",
                        label="25°C",
                        predicate=parse_outcome_label("25°C"),
                        yes_token_id="yes25",
                        no_token_id="no25",
                    ),
                    Outcome(
                        market_id="m26",
                        label="26°C or higher",
                        predicate=parse_outcome_label("26°C or higher"),
                        yes_token_id="yes26",
                        no_token_id="no26",
                    ),
                ],
            )
            _store_forecast(db, 25, "2026-05-04T00:45:00+08:00")
            _store_observation(db, 25.0)
            _store_observation(db, 26.0)
            _store_book_pair(db, "yes26", old_ask=0.40, new_ask=0.405)
            _store_book_pair(db, "no25", old_ask=0.30, new_ask=0.305)

            result = process_actual_entries(db, date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 2)
            bought = {
                row["label"] + " " + row["side"]
                for row in db.execute(
                    "select label, side from paper_decisions where action = 'BUY' and status = 'filled'"
                )
            }
            self.assertEqual(bought, {"25°C NO", "26°C or higher YES"})

    def test_actual_cross_buys_invalidated_no_even_after_price_moves(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=[
                    Outcome(
                        market_id="m25",
                        label="25°C",
                        predicate=parse_outcome_label("25°C"),
                        yes_token_id="yes25",
                        no_token_id="no25",
                    )
                ],
            )
            _store_forecast(db, 25, "2026-05-04T00:45:00+08:00")
            _store_observation(db, 25.0)
            _store_observation(db, 26.0)
            _store_book_pair(db, "no25", old_ask=0.30, new_ask=0.95)

            result = process_actual_entries(db, date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 1)
            decision = db.execute(
                """
                select status, reason
                from paper_decisions
                where event_type = 'actual_cross' and action = 'BUY'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(decision["status"], "filled")

    def test_actual_cross_new_yes_uses_ten_cent_move_and_seventy_cent_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=[
                    Outcome(
                        market_id="m30",
                        label="30°C or higher",
                        predicate=parse_outcome_label("30°C or higher"),
                        yes_token_id="yes30",
                        no_token_id="no30",
                    )
                ],
            )
            _store_forecast(db, 29, "2026-05-04T00:45:00+08:00")
            _store_observation(db, 29.0)
            _store_observation(db, 30.0)
            _store_book_pair(db, "yes30", old_ask=0.60, new_ask=0.69)

            result = process_actual_entries(db, date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 1)

    def test_actual_cross_new_yes_rejects_above_seventy_cents(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=[
                    Outcome(
                        market_id="m30",
                        label="30°C or higher",
                        predicate=parse_outcome_label("30°C or higher"),
                        yes_token_id="yes30",
                        no_token_id="no30",
                    )
                ],
            )
            _store_forecast(db, 29, "2026-05-04T00:45:00+08:00")
            _store_observation(db, 29.0)
            _store_observation(db, 30.0)
            _store_book_pair(db, "yes30", old_ask=0.62, new_ask=0.71)

            result = process_actual_entries(db, date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 0)
            self.assertEqual(result.buys_missed, 1)
            decision = db.execute(
                """
                select status, reason
                from paper_decisions
                where event_type = 'actual_cross' and action = 'BUY'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(decision["status"], "missed")
            self.assertEqual(decision["reason"], "no ask depth at or below max price")

    def test_actual_cross_does_not_buy_no_for_already_invalidated_lower_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=[
                    Outcome(
                        market_id="m24",
                        label="24°C",
                        predicate=parse_outcome_label("24°C"),
                        yes_token_id="yes24",
                        no_token_id="no24",
                    ),
                    Outcome(
                        market_id="m26",
                        label="26°C or higher",
                        predicate=parse_outcome_label("26°C or higher"),
                        yes_token_id="yes26",
                        no_token_id="no26",
                    ),
                ],
            )
            _store_forecast(db, 25, "2026-05-04T00:45:00+08:00")
            _store_observation(db, 25.0)
            _store_observation(db, 26.0)
            _store_book_pair(db, "yes26", old_ask=0.40, new_ask=0.405)
            _store_book_pair(db, "no24", old_ask=0.30, new_ask=0.305)

            result = process_actual_entries(db, date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 1)
            bought = {
                row["label"] + " " + row["side"]
                for row in db.execute(
                    "select label, side from paper_decisions where action = 'BUY' and status = 'filled'"
                )
            }
            self.assertEqual(bought, {"26°C or higher YES"})

    def test_actual_cross_event_is_processed_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=[
                    Outcome(
                        market_id="m30",
                        label="30°C or higher",
                        predicate=parse_outcome_label("30°C or higher"),
                        yes_token_id="yes30",
                        no_token_id="no30",
                    )
                ],
            )
            _store_forecast(db, 29, "2026-05-04T00:45:00+08:00")
            _store_observation(db, 29.0)
            _store_observation(db, 30.0)
            _store_book_pair(db, "yes30", old_ask=0.40, new_ask=0.405)
            _store_book_pair(db, "no30", old_ask=0.60, new_ask=0.60)

            first = process_actual_entries(db, date(2026, 5, 4))
            second = process_actual_entries(db, date(2026, 5, 4))

            self.assertEqual(first.buys_filled, 1)
            self.assertEqual(second.buys_filled, 0)
            self.assertEqual(second.buys_missed, 0)
            self.assertEqual(second.signals, 0)
            buy_count = db.execute(
                "select count(*) from paper_decisions where action = 'BUY'"
            ).fetchone()[0]
            self.assertEqual(buy_count, 1)

    def test_actual_cross_scans_past_distinct_transition_after_later_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=[
                    Outcome(
                        market_id="m30",
                        label="30°C or higher",
                        predicate=parse_outcome_label("30°C or higher"),
                        yes_token_id="yes30",
                        no_token_id="no30",
                    )
                ],
            )
            _store_forecast(db, 29, "2026-05-04T00:45:00+08:00")
            _store_observation(db, 29.0)
            _store_observation(db, 30.0)
            _store_observation(db, 30.0)
            _store_book_pair(db, "yes30", old_ask=0.40, new_ask=0.405)
            _store_book_pair(db, "no30", old_ask=0.60, new_ask=0.60)

            result = process_actual_entries(db, date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 1)
            self.assertIn("observed max changed 29.0 -> 30.0", result.notes)

    def test_candidate_buy_skips_near_settled_entry_price(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=[
                    Outcome(
                        market_id="m30",
                        label="30°C or higher",
                        predicate=parse_outcome_label("30°C or higher"),
                        yes_token_id="yes30",
                        no_token_id="no30",
                    )
                ],
            )
            _store_forecast(db, 29, "2026-05-04T00:45:00+08:00")
            _store_observation(db, 29.0)
            _store_observation(db, 30.0)
            _store_book_pair(db, "yes30", old_ask=0.995, new_ask=0.999)

            result = process_actual_entries(db, date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 0)
            self.assertEqual(result.buys_missed, 1)
            decision = db.execute(
                """
                select reason from paper_decisions
                where action = 'BUY' and status = 'missed'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(decision["reason"], "entry price above max")

    def test_actual_cross_trades_bucket_cross_when_forecast_is_same_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=[
                    Outcome(
                        market_id="m30",
                        label="30°C or higher",
                        predicate=parse_outcome_label("30°C or higher"),
                        yes_token_id="yes30",
                        no_token_id="no30",
                    )
                ],
            )
            _store_forecast(db, 30.8, "2026-05-04T00:45:00+08:00")
            _store_observation(db, 29.0)
            _store_observation(db, 30.0)
            _store_book_pair(db, "yes30", old_ask=0.40, new_ask=0.405)

            result = process_actual_entries(db, date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 1)
            self.assertEqual(result.signals, 1)

    def test_actual_cross_forecast_guard_skips_yes_below_signal_bucket_but_allows_no(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=[
                    Outcome(
                        market_id="m28",
                        label="28°C",
                        predicate=parse_outcome_label("28°C"),
                        yes_token_id="yes28",
                        no_token_id="no28",
                    ),
                    Outcome(
                        market_id="m29",
                        label="29°C or higher",
                        predicate=parse_outcome_label("29°C or higher"),
                        yes_token_id="yes29",
                        no_token_id="no29",
                    ),
                ],
            )
            _store_forecast(db, 30.0, "2026-05-04T00:45:00+08:00")
            _store_observation(db, 28.7)
            _store_observation(db, 29.2)
            _store_book_pair(db, "no28", old_ask=0.40, new_ask=0.405)

            result = process_actual_entries(db, date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 1)
            self.assertEqual(result.buys_missed, 1)
            self.assertEqual(result.signals, 2)
            decisions = db.execute(
                """
                select outcome_id, side, status, reason
                from paper_decisions
                where event_type = 'actual_cross'
                  and action = 'BUY'
                order by id
                """
            ).fetchall()
            self.assertEqual(
                [(row["outcome_id"], row["side"], row["status"], row["reason"]) for row in decisions],
                [
                    ("no28", "NO", "filled", "actual max invalidated lower bucket before price moved"),
                    (
                        "yes29",
                        "YES",
                        "missed",
                        "forecast signal already above crossed bucket",
                    ),
                ],
            )

    def test_actual_cross_falls_back_to_csdi_when_aws_has_no_max_transition(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=[
                    Outcome(
                        market_id="m29",
                        label="29°C or higher",
                        predicate=parse_outcome_label("29°C or higher"),
                        yes_token_id="yes29",
                        no_token_id="no29",
                    )
                ],
            )
            _store_forecast(db, 29.3, "2026-05-04T00:45:00+08:00")
            _store_aws_actual(db, high=28.7, minute=0)
            _store_observation(db, 29.2)
            _store_book_pair(db, "yes29", old_ask=0.40, new_ask=0.405)

            result = process_actual_entries(db, date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 1)
            self.assertEqual(result.signals, 1)

    def test_actual_cross_prefers_aws_max_transitions_when_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=[
                    Outcome(
                        market_id="m29",
                        label="29°C or higher",
                        predicate=parse_outcome_label("29°C or higher"),
                        yes_token_id="yes29",
                        no_token_id="no29",
                    )
                ],
            )
            _store_forecast(db, 29.3, "2026-05-04T00:45:00+08:00")
            _store_observation(db, 29.2)
            _store_aws_actual(db, high=28.7, minute=0)
            _store_aws_actual(db, high=28.8, minute=5)
            _store_book_pair(db, "yes29", old_ask=0.40, new_ask=0.405)

            result = process_actual_entries(db, date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 0)
            self.assertEqual(result.signals, 0)

    def test_actual_cross_yes_allows_seventy_five_cent_entry_in_peak_hour_sure_bet(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=[
                    Outcome(
                        market_id="m29",
                        label="29°C or higher",
                        predicate=parse_outcome_label("29°C or higher"),
                        yes_token_id="yes29",
                        no_token_id="no29",
                    )
                ],
            )
            _store_forecast(db, 29.0, "2026-05-04T00:45:00+08:00")
            _set_latest_ocf_sample_fetched_at(db, "2026-05-04T00:45:00+08:00")
            _store_peak_decline_hourly_forecast(db, date(2026, 5, 4), peak=29.0)
            _store_aws_actual(db, high=28.7, hour=13)
            _store_aws_actual(db, high=29.2, hour=14)
            _store_book_pair(db, "yes29", old_ask=0.75, new_ask=0.75)

            result = process_actual_entries(db, date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 1)
            decision = db.execute(
                """
                select status, reason
                from paper_decisions
                where event_type = 'actual_cross' and action = 'BUY'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(decision["status"], "filled")

    def test_actual_cross_yes_rejects_above_seventy_five_cents_in_peak_hour_sure_bet(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=[
                    Outcome(
                        market_id="m29",
                        label="29°C or higher",
                        predicate=parse_outcome_label("29°C or higher"),
                        yes_token_id="yes29",
                        no_token_id="no29",
                    )
                ],
            )
            _store_forecast(db, 29.0, "2026-05-04T00:45:00+08:00")
            _set_latest_ocf_sample_fetched_at(db, "2026-05-04T00:45:00+08:00")
            _store_peak_decline_hourly_forecast(db, date(2026, 5, 4), peak=29.0)
            _store_aws_actual(db, high=28.7, hour=13)
            _store_aws_actual(db, high=29.2, hour=14)
            _store_book_pair(db, "yes29", old_ask=0.76, new_ask=0.76)

            result = process_actual_entries(db, date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 0)
            self.assertEqual(result.buys_missed, 1)
            decision = db.execute(
                """
                select status, reason
                from paper_decisions
                where event_type = 'actual_cross' and action = 'BUY'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(decision["status"], "missed")
            self.assertEqual(decision["reason"], "no ask depth at or below max price")

    def test_actual_cross_yes_does_not_raise_entry_threshold_when_peak_repeats(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=[
                    Outcome(
                        market_id="m29",
                        label="29°C or higher",
                        predicate=parse_outcome_label("29°C or higher"),
                        yes_token_id="yes29",
                        no_token_id="no29",
                    )
                ],
            )
            _store_forecast(db, 29.0, "2026-05-04T00:45:00+08:00")
            _set_latest_ocf_sample_fetched_at(db, "2026-05-04T00:45:00+08:00")
            _store_early_breach_hourly_forecast(db, date(2026, 5, 4), bucket=29.0)
            _store_aws_actual(db, high=28.7, hour=13)
            _store_aws_actual(db, high=29.2, hour=14)
            _store_book_pair(db, "yes29", old_ask=0.76, new_ask=0.76)

            result = process_actual_entries(db, date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 0)
            self.assertEqual(result.buys_missed, 1)
            decision = db.execute(
                """
                select status, reason
                from paper_decisions
                where event_type = 'actual_cross' and action = 'BUY'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(decision["status"], "missed")
            self.assertEqual(decision["reason"], "no ask depth at or below max price")

    def test_actual_cross_fast_lane_buys_exact_yes_and_invalidated_no_when_latest_forecast_agrees(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=[
                    Outcome(
                        market_id="m25",
                        label="25°C",
                        predicate=parse_outcome_label("25°C"),
                        yes_token_id="yes25",
                        no_token_id="no25",
                    ),
                    Outcome(
                        market_id="m26",
                        label="26°C",
                        predicate=parse_outcome_label("26°C"),
                        yes_token_id="yes26",
                        no_token_id="no26",
                    ),
                ],
            )
            _store_forecast(db, 25.8, "2026-05-04T00:45:00+08:00")
            _set_latest_ocf_sample_fetched_at(db, "2026-05-04T00:45:00+08:00")
            _store_peak_decline_hourly_forecast(
                db, date(2026, 5, 4), peak=25.8, fetched_at_hkt="2026-05-04T13:55:00+08:00"
            )
            _store_current_hour_matches_actual_future_declines_forecast(
                db, date(2026, 5, 4), actual=26.1, fetched_at_hkt="2026-05-04T14:01:00+08:00"
            )
            _store_aws_actual(db, high=25.6, hour=13, minute=50)
            _store_aws_actual(db, high=26.1, hour=14, minute=0)
            _store_book_pair(db, "no25", old_ask=0.74, new_ask=0.74)
            _store_book_pair(db, "yes26", old_ask=0.50, new_ask=0.74)

            result = process_actual_entries(db, date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 2)
            bought = {
                row["label"] + " " + row["side"]
                for row in db.execute(
                    "select label, side from paper_decisions where action = 'BUY' and status = 'filled'"
                )
            }
            self.assertEqual(bought, {"25°C NO", "26°C YES"})

    def test_actual_cross_fast_lane_uses_candidate_execution_bridge(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=[
                    Outcome(
                        market_id="m25",
                        label="25°C",
                        predicate=parse_outcome_label("25°C"),
                        yes_token_id="yes25",
                        no_token_id="no25",
                    ),
                    Outcome(
                        market_id="m26",
                        label="26°C",
                        predicate=parse_outcome_label("26°C"),
                        yes_token_id="yes26",
                        no_token_id="no26",
                    ),
                ],
            )
            _store_forecast(db, 25.8, "2026-05-04T00:45:00+08:00")
            _set_latest_ocf_sample_fetched_at(db, "2026-05-04T00:45:00+08:00")
            _store_peak_decline_hourly_forecast(
                db, date(2026, 5, 4), peak=25.8, fetched_at_hkt="2026-05-04T13:55:00+08:00"
            )
            _store_current_hour_matches_actual_future_declines_forecast(
                db, date(2026, 5, 4), actual=26.1, fetched_at_hkt="2026-05-04T14:01:00+08:00"
            )
            _store_aws_actual(db, high=25.6, hour=13, minute=50)
            _store_aws_actual(db, high=26.1, hour=14, minute=0)
            _store_book_pair(db, "no25", old_ask=0.74, new_ask=0.74)
            _store_book_pair(db, "yes26", old_ask=0.50, new_ask=0.74)
            bridge_calls = []

            def bridge(actions, executor):
                bridge_calls.append(actions)
                return [
                    CandidateAction(
                        action.candidate_key,
                        action.conflict_keys,
                        lambda action=action: executor(action),
                    )
                    for action in actions
                ]

            with patch("whenitrains.runner.executable_candidate_actions", side_effect=bridge):
                result = process_actual_entries(db, date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 2)
            self.assertEqual(len(bridge_calls), 1)
            self.assertEqual(
                {action.intent for action in bridge_calls[0]},
                {"buy_crossed_bucket_yes", "buy_invalidated_bucket_no"},
            )
            self.assertTrue(all(action.candidate_key.startswith("actual_cross:") for action in bridge_calls[0]))

    def test_actual_cross_builds_ladder_metadata_once_for_hot_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=[
                    Outcome(
                        market_id="m30",
                        label="30°C or higher",
                        predicate=parse_outcome_label("30°C or higher"),
                        yes_token_id="yes30",
                        no_token_id="no30",
                    )
                ],
            )
            _store_forecast(db, 29, "2026-05-04T00:45:00+08:00")
            _store_observation(db, 29.0)
            _store_observation(db, 30.0)
            _store_book_pair(db, "yes30", old_ask=0.40, new_ask=0.405)
            _store_book_pair(db, "no30", old_ask=0.60, new_ask=0.60)
            rows = list(db.execute(
                """
                select o.id, o.market_id, o.polymarket_market_id, o.label,
                       o.predicate_type, o.predicate_value_c, o.yes_token_id, o.no_token_id,
                       m.target_date_hkt, m.slug
                from outcomes o
                join markets m on m.id = o.market_id
                order by o.id
                """
            ))

            with patch("whenitrains.runner.build_active_ladder_metadata") as metadata, patch(
                "whenitrains.runner.list_outcomes_for_date",
                side_effect=AssertionError("hot path should use precomputed ladder rows"),
            ):
                metadata.return_value = []
                result = process_actual_entries(
                    db,
                    date(2026, 5, 4),
                    ladder_rows={"highest": rows, "lowest": []},
                )

            self.assertEqual(result.buys_filled, 1)
            metadata.assert_called_once()

    def test_actual_cross_fast_lane_skips_exact_yes_when_latest_forecast_later_hour_reaches_actual(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=[
                    Outcome(
                        market_id="m26",
                        label="26°C",
                        predicate=parse_outcome_label("26°C"),
                        yes_token_id="yes26",
                        no_token_id="no26",
                    )
                ],
            )
            _store_forecast(db, 25.8, "2026-05-04T00:45:00+08:00")
            _set_latest_ocf_sample_fetched_at(db, "2026-05-04T00:45:00+08:00")
            _store_peak_decline_hourly_forecast(
                db, date(2026, 5, 4), peak=25.8, fetched_at_hkt="2026-05-04T13:55:00+08:00"
            )
            _store_successive_hour_not_below_actual_forecast(
                db, date(2026, 5, 4), actual=26.1, fetched_at_hkt="2026-05-04T14:01:00+08:00"
            )
            _store_aws_actual(db, high=25.6, hour=13, minute=50)
            _store_aws_actual(db, high=26.1, hour=14, minute=0)
            _store_book_pair(db, "yes26", old_ask=0.50, new_ask=0.74)

            result = process_actual_entries(db, date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 0)
            self.assertEqual(result.buys_missed, 1)
            decision = db.execute(
                """
                select status, reason
                from paper_decisions
                where event_type = 'actual_cross' and action = 'BUY'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(decision["status"], "missed")
            self.assertEqual(decision["reason"], "latest hourly forecast does not confirm exact-bucket fast lane")

    def test_actual_cross_fast_lane_uses_preceding_forecast_even_when_new_forecast_marks_current_hour_at_actual(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=[
                    Outcome(
                        market_id="m26",
                        label="26°C",
                        predicate=parse_outcome_label("26°C"),
                        yes_token_id="yes26",
                        no_token_id="no26",
                    )
                ],
            )
            _store_forecast(db, 25.8, "2026-05-04T00:45:00+08:00")
            _set_latest_ocf_sample_fetched_at(db, "2026-05-04T00:45:00+08:00")
            _store_peak_decline_hourly_forecast(
                db, date(2026, 5, 4), peak=25.8, fetched_at_hkt="2026-05-04T13:55:00+08:00"
            )
            _store_current_hour_matches_actual_future_declines_forecast(
                db, date(2026, 5, 4), actual=26.1, fetched_at_hkt="2026-05-04T14:01:00+08:00"
            )
            _store_aws_actual(db, high=25.6, hour=13, minute=50)
            _store_aws_actual(db, high=26.1, hour=14, minute=0)
            _store_book_pair(db, "yes26", old_ask=0.50, new_ask=0.74)

            result = process_actual_entries(db, date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 1)

    def test_actual_cross_fast_lane_requires_preceding_forecast_basis(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=[
                    Outcome(
                        market_id="m26",
                        label="26°C",
                        predicate=parse_outcome_label("26°C"),
                        yes_token_id="yes26",
                        no_token_id="no26",
                    )
                ],
            )
            _store_forecast(db, 25.8, "2026-05-04T14:01:00+08:00")
            _set_latest_ocf_sample_fetched_at(db, "2026-05-04T14:01:00+08:00")
            _store_current_hour_matches_actual_future_declines_forecast(
                db, date(2026, 5, 4), actual=26.1, fetched_at_hkt="2026-05-04T14:01:00+08:00"
            )
            _store_aws_actual(db, high=25.6, hour=13, minute=50)
            _store_aws_actual(db, high=26.1, hour=14, minute=0)
            _store_book_pair(db, "yes26", old_ask=0.50, new_ask=0.74)

            result = process_actual_entries(db, date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 0)
            self.assertEqual(result.buys_missed, 1)

    def test_actual_cross_ignores_previous_day_observation_transition(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                target_date=date(2026, 5, 5),
                outcomes=[
                    Outcome(
                        market_id="m24",
                        label="24°C or higher",
                        predicate=parse_outcome_label("24°C or higher"),
                        yes_token_id="yes24gte",
                        no_token_id="no24gte",
                    )
                ],
            )
            _store_forecast(
                db,
                23,
                "2026-05-05T00:45:00+08:00",
                forecast_date=date(2026, 5, 5),
            )
            _store_observation(db, 22.9, observed_date=date(2026, 5, 4))
            _store_observation(db, 27.6, observed_date=date(2026, 5, 4))
            _store_book_pair(db, "yes24gte", old_ask=0.40, new_ask=0.405)

            result = process_actual_entries(db, date(2026, 5, 5))

            self.assertEqual(result.buys_filled, 0)
            self.assertEqual(result.signals, 0)

    def test_forecast_down_buys_new_forecast_yes_and_no_above_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=_forecast_bucket_outcomes(),
            )
            _store_forecast(db, 24, "2026-05-05T04:11:45+08:00")
            _store_forecast(db, 23, "2026-05-05T05:31:39+08:00")
            for token in [
                "yes22",
                "yes23",
                "yes24",
                "no22",
                "no23",
                "no24",
                "yes25",
                "no25",
            ]:
                _store_book_pair(db, token, old_ask=0.20, new_ask=0.20)

            result = process_forecast_entries(db, date(2026, 5, 4), today_hkt=date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 3)
            bought = {
                row["label"] + " " + row["side"]
                for row in db.execute(
                    "select label, side from paper_decisions where action = 'BUY' and status = 'filled'"
                )
            }
            self.assertEqual(bought, {"23°C YES", "24°C NO", "25°C NO"})

    def test_forecast_up_buys_new_forecast_yes_and_no_below_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=_forecast_bucket_outcomes(),
            )
            _store_forecast(db, 24, "2026-05-05T04:11:45+08:00")
            _store_forecast(db, 25, "2026-05-05T05:31:39+08:00")
            for token in [
                "yes22",
                "yes23",
                "yes24",
                "no22",
                "no23",
                "no24",
                "yes25",
                "no25",
            ]:
                _store_book_pair(db, token, old_ask=0.20, new_ask=0.20)

            result = process_forecast_entries(db, date(2026, 5, 4), today_hkt=date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 4)
            bought = {
                row["label"] + " " + row["side"]
                for row in db.execute(
                    "select label, side from paper_decisions where action = 'BUY' and status = 'filled'"
                )
            }
            self.assertEqual(bought, {"22°C NO", "23°C NO", "24°C NO", "25°C YES"})

    def test_forecast_change_sells_positions_invalidated_by_new_forecast(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=_forecast_bucket_outcomes(),
            )
            from whenitrains.paper_db import execute_paper_buy

            execute_paper_buy(
                db,
                token_id="yes23",
                side="YES",
                size_usd=100,
                asks=[(0.30, 1000)],
                max_order_usd=250,
                reason="test",
            )
            _store_forecast(db, 23, "2026-05-05T04:11:45+08:00")
            _store_forecast(db, 24, "2026-05-05T05:31:39+08:00")
            for token in [
                "yes22",
                "yes23",
                "yes24",
                "no22",
                "no23",
                "no24",
                "yes25",
                "no25",
            ]:
                _store_book_pair(db, token, old_ask=0.20, new_ask=0.20)
            store_orderbook(
                db,
                "yes23",
                OrderBook("yes23", bids=[(0.25, 1000)], asks=[(0.26, 1000)], tick_size=0.01, min_order_size=5),
            )

            result = process_forecast_entries(
                db, date(2026, 5, 4), today_hkt=date(2026, 5, 4)
            )

            self.assertEqual(result.sells_filled, 1)
            position = db.execute(
                "select net_shares from paper_positions where outcome_id = 'yes23'"
            ).fetchone()
            self.assertEqual(position["net_shares"], 0)
            decision = db.execute(
                """
                select reason from paper_decisions
                where event_type = 'forecast_exit' and action = 'SELL'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(decision["reason"], "position invalidated by hourly forecast")

    def test_forecast_position_exit_uses_candidate_execution_bridge(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=_forecast_bucket_outcomes(),
            )
            from whenitrains.paper_db import execute_paper_buy

            execute_paper_buy(
                db,
                token_id="yes23",
                side="YES",
                size_usd=100,
                asks=[(0.30, 1000)],
                max_order_usd=250,
                reason="test",
            )
            _store_forecast(db, 23, "2026-05-05T04:11:45+08:00")
            _store_forecast(db, 24, "2026-05-05T05:31:39+08:00")
            store_orderbook(
                db,
                "yes23",
                OrderBook("yes23", bids=[(0.25, 1000)], asks=[(0.26, 1000)], tick_size=0.01, min_order_size=5),
            )
            bridge_calls = []

            def bridge(actions, executor):
                bridge_calls.append(actions)
                return [
                    CandidateAction(
                        action.candidate_key,
                        action.conflict_keys,
                        lambda action=action: executor(action),
                    )
                    for action in actions
                ]

            with patch("whenitrains.runner.executable_candidate_actions", side_effect=bridge):
                result = process_forecast_position_exits(
                    db,
                    date(2026, 5, 4),
                    24.0,
                    event_key="forecast_change:test",
                )

            self.assertEqual(result.sells_filled, 1)
            self.assertEqual(len(bridge_calls), 1)
            action = bridge_calls[0][0]
            self.assertEqual(action.intent, "sell_forecast_exit")
            self.assertEqual(action.token_id, "yes23")
            self.assertEqual(action.side, "SELL")
            self.assertEqual(action.candidate_key, "forecast_change:test:sell_forecast_exit:yes23")
            self.assertIn("token:yes23", action.conflict_keys)
            self.assertIn("position:yes23", action.conflict_keys)

    def test_forecast_position_exit_uses_batched_outcome_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=_forecast_bucket_outcomes(),
            )
            from whenitrains.paper_db import execute_paper_buy

            execute_paper_buy(
                db,
                token_id="yes23",
                side="YES",
                size_usd=100,
                asks=[(0.30, 1000)],
                max_order_usd=250,
                reason="test",
            )
            _store_forecast(db, 23, "2026-05-05T04:11:45+08:00")
            _store_forecast(db, 24, "2026-05-05T05:31:39+08:00")
            store_orderbook(
                db,
                "yes23",
                OrderBook("yes23", bids=[(0.25, 1000)], asks=[(0.26, 1000)], tick_size=0.01, min_order_size=5),
            )
            with patch(
                "whenitrains.runner.find_outcome_by_token",
                side_effect=AssertionError("forecast exit hot path should use batched rows"),
            ):
                result = process_forecast_position_exits(
                    db,
                    date(2026, 5, 4),
                    24.0,
                    event_key="forecast_change:test",
                )

            self.assertEqual(result.sells_filled, 1)

    def test_forecast_value_buys_cheap_forecast_bucket_when_favorite_is_lower(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=_threshold_risk_outcomes(),
            )
            _store_forecast(db, 29, "2026-05-05T09:11:53+08:00")
            _store_book_pair(db, "yes28", old_ask=0.60, new_ask=0.60)
            _store_book_pair(db, "yes29", old_ask=0.30, new_ask=0.30)
            _store_book_pair(db, "yes30", old_ask=0.10, new_ask=0.10)

            result = process_forecast_entries(
                db, date(2026, 5, 4), today_hkt=date(2026, 5, 4)
            )

            self.assertEqual(result.buys_filled, 1)
            decision = db.execute(
                """
                select event_type, label, side, reason
                from paper_decisions
                where action = 'BUY' and status = 'filled'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(decision["event_type"], "forecast_value")
            self.assertEqual(decision["label"], "29°C")
            self.assertEqual(decision["side"], "YES")

    def test_forecast_value_uses_candidate_execution_bridge(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=_threshold_risk_outcomes(),
            )
            _store_forecast(db, 29, "2026-05-05T09:11:53+08:00")
            _store_book_pair(db, "yes28", old_ask=0.60, new_ask=0.60)
            _store_book_pair(db, "yes29", old_ask=0.30, new_ask=0.30)
            _store_book_pair(db, "yes30", old_ask=0.10, new_ask=0.10)
            bridge_calls = []

            def bridge(actions, executor):
                bridge_calls.append(actions)
                return [
                    CandidateAction(
                        action.candidate_key,
                        action.conflict_keys,
                        lambda action=action: executor(action),
                    )
                    for action in actions
                ]

            with patch("whenitrains.runner.executable_candidate_actions", side_effect=bridge):
                result = process_forecast_entries(
                    db, date(2026, 5, 4), today_hkt=date(2026, 5, 4)
                )

            self.assertEqual(result.buys_filled, 1)
            self.assertEqual(len(bridge_calls), 1)
            action = bridge_calls[0][0]
            self.assertEqual(action.intent, "buy_forecast_value_yes")
            self.assertEqual(action.token_id, "yes29")
            self.assertEqual(action.side, "BUY_YES")
            self.assertTrue(action.candidate_key.startswith("forecast_value:2026-05-04:"))
            self.assertIn("token:yes29", action.conflict_keys)
            self.assertIn("risk:entry_budget", action.conflict_keys)

    def test_forecast_value_does_not_use_aws_actual_as_forecast(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=_threshold_risk_outcomes(),
            )
            _store_forecast(db, 30.4, "2026-05-04T09:11:53+08:00")
            _store_aws_actual(db, high=29.3)
            _store_book_pair(db, "yes28", old_ask=0.60, new_ask=0.60)
            _store_book_pair(db, "yes29", old_ask=0.30, new_ask=0.30)
            _store_book_pair(db, "yes30", old_ask=0.10, new_ask=0.10)

            result = process_forecast_entries(
                db, date(2026, 5, 4), today_hkt=date(2026, 5, 4)
            )

            self.assertEqual(result.buys_filled, 1)
            decision = db.execute(
                """
                select event_type, label
                from paper_decisions
                where action = 'BUY' and status = 'filled'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(decision["event_type"], "forecast_value")
            self.assertEqual(decision["label"], "30°C")

    def test_forecast_value_skips_stale_ocf_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=[
                    Outcome(
                        market_id="m30",
                        label="30°C",
                        predicate=parse_outcome_label("30°C"),
                        yes_token_id="yes30",
                        no_token_id="no30",
                    ),
                    Outcome(
                        market_id="m29",
                        label="29°C",
                        predicate=parse_outcome_label("29°C"),
                        yes_token_id="yes29",
                        no_token_id="no29",
                    ),
                ],
            )
            _store_forecast(db, 30, "2026-05-05T09:11:53+08:00")
            stale_at = (datetime.now(timezone.utc) - timedelta(minutes=91)).isoformat()
            db.execute("update ocf_forecast_samples set fetched_at_utc = ?", (stale_at,))
            db.commit()
            _store_book_pair(db, "yes30", old_ask=0.20, new_ask=0.20)
            _store_book_pair(db, "yes29", old_ask=0.60, new_ask=0.60)

            result = process_forecast_entries(
                db, date(2026, 5, 4), today_hkt=date(2026, 5, 4)
            )

            self.assertEqual(result.buys_filled, 0)
            self.assertTrue(any("OCF forecast sample stale" in note for note in result.notes))

    def test_low_forecast_value_buys_cheap_bucket_when_favorite_is_higher(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=_threshold_risk_outcomes(),
            )
            _store_lowest_market(db, _threshold_risk_outcomes())
            _store_forecast_range(db, low=29, high=33, update_time="2026-05-05T09:11:53+08:00")
            _store_book_pair(db, "yes28", old_ask=0.10, new_ask=0.10)
            _store_book_pair(db, "yes29", old_ask=0.30, new_ask=0.30)
            _store_book_pair(db, "yes30", old_ask=0.60, new_ask=0.60)

            result = process_forecast_entries(
                db, date(2026, 5, 4), today_hkt=date(2026, 5, 4)
            )

            self.assertEqual(result.buys_filled, 1)
            decision = db.execute(
                """
                select event_type, label, side
                from paper_decisions
                where action = 'BUY' and status = 'filled'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(decision["event_type"], "lowest_forecast_value")
            self.assertEqual(decision["label"], "29°C")
            self.assertEqual(decision["side"], "YES")

    def test_forecast_value_blocks_late_day_peak_bucket_buy(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=_threshold_risk_outcomes(),
            )
            _store_forecast(db, 29, "2026-05-04T15:11:53+08:00")
            _store_late_peak_hourly_forecast(db, date(2026, 5, 4), 29.0)
            _store_book_pair(db, "yes28", old_ask=0.60, new_ask=0.60)
            _store_book_pair(db, "yes29", old_ask=0.20, new_ask=0.20)
            _store_book_pair(db, "yes30", old_ask=0.10, new_ask=0.10)

            result = process_forecast_entries(
                db, date(2026, 5, 4), today_hkt=date(2026, 5, 4)
            )

            self.assertEqual(result.buys_filled, 0)
            order_count = db.execute("select count(*) from paper_orders").fetchone()[0]
            self.assertEqual(order_count, 0)
            decision = db.execute(
                """
                select status, reason
                from paper_decisions
                where event_type = 'forecast_value'
                  and action = 'BUY'
                  and label = '29°C'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(decision["status"], "ignored")
            self.assertEqual(decision["reason"], "late-day forecast peak guard")

    def test_forecast_value_blocks_when_hourly_forecast_never_reaches_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=_threshold_risk_outcomes(),
            )
            _store_forecast(db, 29, "2026-05-04T15:11:53+08:00")
            _store_below_bucket_hourly_forecast(db, date(2026, 5, 4), 29.0)
            _store_book_pair(db, "yes28", old_ask=0.60, new_ask=0.60)
            _store_book_pair(db, "yes29", old_ask=0.20, new_ask=0.20)
            _store_book_pair(db, "yes30", old_ask=0.10, new_ask=0.10)

            result = process_forecast_value_entry(
                db, date(2026, 5, 4), today_hkt=date(2026, 5, 4)
            )

            self.assertEqual(result.buys_filled, 0)
            order_count = db.execute("select count(*) from paper_orders").fetchone()[0]
            self.assertEqual(order_count, 0)
            decision_count = db.execute(
                """
                select count(*)
                from paper_decisions
                where event_type = 'forecast_value'
                  and action = 'BUY'
                  and label = '29°C'
                """
            ).fetchone()[0]
            self.assertEqual(decision_count, 0)

    def test_late_day_peak_guard_preempts_cheap_threshold_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=_threshold_risk_outcomes(),
            )
            _store_forecast(db, 29, "2026-05-04T15:11:53+08:00")
            _store_late_peak_hourly_forecast(db, date(2026, 5, 4), 29.0)
            _store_book_pair(db, "yes28", old_ask=0.60, new_ask=0.60)
            _store_book_pair(db, "yes29", old_ask=0.31, new_ask=0.31)
            _store_book_pair(db, "yes30", old_ask=0.10, new_ask=0.10)

            result = process_forecast_entries(
                db, date(2026, 5, 4), today_hkt=date(2026, 5, 4)
            )

            self.assertIn(
                "forecast value skipped: 2026-05-04 29°C late-day forecast peak guard",
                result.notes,
            )
            self.assertNotIn(
                "forecast value skipped: 2026-05-04 29°C ask=0.310 > cheap_threshold=0.300",
                result.notes,
            )
            decision = db.execute(
                """
                select status, reason
                from paper_decisions
                where event_type = 'forecast_value'
                  and action = 'BUY'
                  and label = '29°C'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(decision["status"], "ignored")
            self.assertEqual(decision["reason"], "late-day forecast peak guard")

    def test_forecast_value_allows_bucket_when_hourly_forecast_breaches_before_21(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=_threshold_risk_outcomes(),
            )
            _store_forecast(db, 29, "2026-05-04T15:11:53+08:00")
            _store_early_breach_hourly_forecast(db, date(2026, 5, 4), 29.0)
            _store_book_pair(db, "yes28", old_ask=0.60, new_ask=0.60)
            _store_book_pair(db, "yes29", old_ask=0.20, new_ask=0.20)
            _store_book_pair(db, "yes30", old_ask=0.10, new_ask=0.10)

            result = process_forecast_entries(
                db, date(2026, 5, 4), today_hkt=date(2026, 5, 4)
            )

            self.assertEqual(result.buys_filled, 1)
            decision = db.execute(
                """
                select status, reason
                from paper_decisions
                where event_type = 'forecast_value'
                  and action = 'BUY'
                  and label = '29°C'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(decision["status"], "filled")
            self.assertEqual(
                decision["reason"],
                "forecast bucket priced unrealistically low vs HKO forecast",
            )

    def test_exit_loop_sells_for_late_day_peak_hourly_invalidation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=_threshold_risk_outcomes(),
            )
            from whenitrains.paper_db import execute_paper_buy

            execute_paper_buy(
                db,
                token_id="yes29",
                side="YES",
                size_usd=100,
                asks=[(0.20, 1000)],
                max_order_usd=250,
                reason="test",
            )
            _store_late_peak_hourly_forecast(db, date(2026, 5, 4), 29.0)
            store_orderbook(
                db,
                "yes29",
                OrderBook(
                    "yes29",
                    bids=[(0.18, 1000)],
                    asks=[(0.20, 1000)],
                    tick_size=0.01,
                    min_order_size=5,
                ),
            )

            result = process_open_position_exits(db, today_hkt=date(2026, 5, 4))

            self.assertEqual(result.sells_filled, 1)
            position = db.execute(
                "select net_shares from paper_positions where outcome_id = 'yes29'"
            ).fetchone()
            self.assertEqual(position["net_shares"], 0)
            decision = db.execute(
                """
                select reason
                from paper_decisions
                where event_type = 'exit_check' and action = 'SELL'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(decision["reason"], "late-day forecast peak guard")

    def test_forecast_exit_prefers_decimal_hourly_forecast_over_rounded_daily_max(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=[
                    Outcome(
                        market_id="m25",
                        label="25°C",
                        predicate=parse_outcome_label("25°C"),
                        yes_token_id="yes25",
                        no_token_id="no25",
                    )
                ],
            )
            from whenitrains.paper_db import execute_paper_buy

            execute_paper_buy(
                db,
                token_id="yes25",
                side="YES",
                size_usd=100,
                asks=[(0.20, 1000)],
                max_order_usd=250,
                reason="test",
            )
            _store_below_bucket_hourly_forecast(db, date(2026, 5, 4), 25.0)
            store_orderbook(
                db,
                "yes25",
                OrderBook(
                    "yes25",
                    bids=[(0.18, 1000)],
                    asks=[(0.20, 1000)],
                    tick_size=0.01,
                    min_order_size=5,
                ),
            )

            result = process_forecast_position_exits(
                db,
                target_date=date(2026, 5, 4),
                new_forecast_max_c=25.0,
            )

            self.assertEqual(result.sells_filled, 1)
            position = db.execute(
                "select net_shares from paper_positions where outcome_id = 'yes25'"
            ).fetchone()
            self.assertEqual(position["net_shares"], 0)
            decision = db.execute(
                """
                select reason
                from paper_decisions
                where event_type = 'forecast_exit' and action = 'SELL'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(decision["reason"], "position invalidated by hourly forecast")

    def test_forecast_value_skips_when_favorite_is_above_non_top_forecast_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=_threshold_risk_outcomes(),
            )
            _store_forecast(db, 29, "2026-05-05T09:11:53+08:00")
            _store_book_pair(db, "yes28", old_ask=0.10, new_ask=0.10)
            _store_book_pair(db, "yes29", old_ask=0.30, new_ask=0.30)
            _store_book_pair(db, "yes30", old_ask=0.60, new_ask=0.60)

            result = process_forecast_entries(
                db, date(2026, 5, 4), today_hkt=date(2026, 5, 4)
            )

            self.assertEqual(result.buys_filled, 0)
            self.assertIn(
                "forecast value skipped: 2026-05-04 favorite 30°C is above forecast bucket 29°C; threshold risk",
                result.notes,
            )

    def test_forecast_value_not_cheap_note_names_bucket_and_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=_threshold_risk_outcomes(),
            )
            _store_forecast(db, 29, "2026-05-05T09:11:53+08:00")
            _store_book_pair(db, "yes28", old_ask=0.60, new_ask=0.60)
            _store_book_pair(db, "yes29", old_ask=0.31, new_ask=0.31)
            _store_book_pair(db, "yes30", old_ask=0.10, new_ask=0.10)

            result = process_forecast_entries(
                db, date(2026, 5, 4), today_hkt=date(2026, 5, 4)
            )

            self.assertIn(
                "forecast value skipped: 2026-05-04 29°C ask=0.310 > cheap_threshold=0.300",
                result.notes,
            )

    def test_forecast_value_ignores_lowest_temperature_markets(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            _store_lowest_market(db, _threshold_risk_outcomes())
            _store_forecast(db, 29, "2026-05-05T09:11:53+08:00")
            _store_book_pair(db, "yes29", old_ask=0.10, new_ask=0.10)

            result = process_forecast_entries(
                db, date(2026, 5, 4), today_hkt=date(2026, 5, 4)
            )

            self.assertEqual(result.buys_filled, 0)
            self.assertIn(
                "forecast value skipped: 2026-05-04 missing outcomes",
                result.notes,
            )

    def test_forecast_value_lead_time_note_explains_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=_threshold_risk_outcomes(),
                target_date=date(2026, 5, 7),
            )
            _store_forecast(
                db,
                29,
                "2026-05-05T09:11:53+08:00",
                forecast_date=date(2026, 5, 7),
            )
            _store_book_pair(db, "yes28", old_ask=0.60, new_ask=0.60)
            _store_book_pair(db, "yes29", old_ask=0.20, new_ask=0.20)
            _store_book_pair(db, "yes30", old_ask=0.10, new_ask=0.10)

            result = process_forecast_entries(
                db, date(2026, 5, 7), today_hkt=date(2026, 5, 5)
            )

            self.assertIn(
                "forecast value skipped: 2026-05-07 lead_days=2 > max=1",
                result.notes,
            )

    def test_forecast_value_buys_cheap_top_bucket_when_favorite_is_lower(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=[
                    Outcome(
                        market_id="m28",
                        label="28°C",
                        predicate=parse_outcome_label("28°C"),
                        yes_token_id="yes28",
                        no_token_id="no28",
                    ),
                    Outcome(
                        market_id="m29top",
                        label="29°C or higher",
                        predicate=parse_outcome_label("29°C or higher"),
                        yes_token_id="yes29top",
                        no_token_id="no29top",
                    ),
                ],
            )
            _store_forecast(db, 29, "2026-05-05T09:11:53+08:00")
            _store_book_pair(db, "yes28", old_ask=0.60, new_ask=0.60)
            _store_book_pair(db, "yes29top", old_ask=0.30, new_ask=0.30)

            result = process_forecast_entries(
                db, date(2026, 5, 4), today_hkt=date(2026, 5, 4)
            )

            self.assertEqual(result.buys_filled, 1)
            decision = db.execute(
                """
                select label, side from paper_decisions
                where event_type = 'forecast_value' and action = 'BUY' and status = 'filled'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(decision["label"], "29°C or higher")
            self.assertEqual(decision["side"], "YES")

    def test_actual_low_cross_buys_stale_exact_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            _store_lowest_market(
                db,
                [
                    Outcome(
                        market_id="low29",
                        label="29°C",
                        predicate=parse_outcome_label("29°C"),
                        yes_token_id="yes-low29",
                        no_token_id="no-low29",
                    )
                ],
            )
            _store_forecast_range(db, low=29, high=33, update_time="2026-05-04T09:00:00+08:00")
            _store_min_observation(db, low=30.2, hour=7)
            _store_min_observation(db, low=29.4, hour=8)
            _store_book_pair(db, "yes-low29", old_ask=0.25, new_ask=0.25)

            result = process_actual_entries(db, date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 1)
            decision = db.execute(
                """
                select event_type, label, side
                from paper_decisions
                where event_type = 'actual_low_cross' and action = 'BUY'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(decision["label"], "29°C")
            self.assertEqual(decision["side"], "YES")

    def test_actual_low_cross_uses_candidate_execution_bridge(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            _store_lowest_market(
                db,
                [
                    Outcome(
                        market_id="low29",
                        label="29°C",
                        predicate=parse_outcome_label("29°C"),
                        yes_token_id="yes-low29",
                        no_token_id="no-low29",
                    )
                ],
            )
            _store_forecast_range(db, low=29, high=33, update_time="2026-05-04T09:00:00+08:00")
            _store_min_observation(db, low=30.2, hour=7)
            _store_min_observation(db, low=29.4, hour=8)
            _store_book_pair(db, "yes-low29", old_ask=0.25, new_ask=0.25)
            bridge_calls = []

            def bridge(actions, executor):
                bridge_calls.append(actions)
                return [
                    CandidateAction(
                        action.candidate_key,
                        action.conflict_keys,
                        lambda action=action: executor(action),
                    )
                    for action in actions
                ]

            with patch("whenitrains.runner.executable_candidate_actions", side_effect=bridge):
                result = process_actual_entries(db, date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 1)
            self.assertEqual(len(bridge_calls), 1)
            action = bridge_calls[0][0]
            self.assertEqual(action.intent, "buy_actual_low_cross_yes")
            self.assertEqual(action.token_id, "yes-low29")
            self.assertEqual(action.side, "BUY_YES")
            self.assertTrue(action.candidate_key.startswith("actual_low_cross:2026-05-04:"))
            self.assertIn("token:yes-low29", action.conflict_keys)
            self.assertIn("risk:entry_budget", action.conflict_keys)

    def test_actual_low_cross_missing_orderbooks_is_retryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            _store_lowest_market(
                db,
                [
                    Outcome(
                        market_id="low29",
                        label="29°C",
                        predicate=parse_outcome_label("29°C"),
                        yes_token_id="yes-low29",
                        no_token_id="no-low29",
                    )
                ],
            )
            _store_forecast_range(db, low=29, high=33, update_time="2026-05-04T09:00:00+08:00")
            _store_min_observation(db, low=30.2, hour=7)
            _store_min_observation(db, low=29.4, hour=8)

            first = process_actual_entries(db, date(2026, 5, 4))
            processed_count = db.execute(
                """
                select count(*)
                from paper_decisions
                where event_type = 'actual_low_cross'
                  and action = 'EVENT'
                  and status = 'processed'
                """
            ).fetchone()[0]
            _store_book_pair(db, "yes-low29", old_ask=0.25, new_ask=0.25)

            second = process_actual_entries(db, date(2026, 5, 4))

            self.assertEqual(first.buys_filled, 0)
            self.assertEqual(processed_count, 0)
            self.assertEqual(second.buys_filled, 1)

    def test_forecast_value_buy_only_sweeps_depth_up_to_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=_threshold_risk_outcomes(),
            )
            _store_forecast(db, 29, "2026-05-05T09:11:53+08:00")
            _store_book_pair(db, "yes28", old_ask=0.60, new_ask=0.60)
            store_orderbook(
                db,
                "yes29",
                OrderBook(
                    "yes29",
                    bids=[(0.25, 1000)],
                    asks=[(0.30, 100), (0.31, 1000)],
                    tick_size=0.01,
                    min_order_size=5,
                ),
            )
            store_orderbook(
                db,
                "yes30",
                OrderBook(
                    "yes30",
                    bids=[(0.08, 1000)],
                    asks=[(0.10, 1000)],
                    tick_size=0.01,
                    min_order_size=5,
                ),
            )

            result = process_forecast_entries(
                db, date(2026, 5, 4), today_hkt=date(2026, 5, 4)
            )

            self.assertEqual(result.buys_filled, 1)
            order = db.execute(
                """
                select limit_price, simulated_fill_price, simulated_fill_size_usd
                from paper_orders
                where side = 'BUY_YES'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertAlmostEqual(order["limit_price"], 0.30)
            self.assertAlmostEqual(order["simulated_fill_price"], 0.30)
            self.assertAlmostEqual(order["simulated_fill_size_usd"], 30.0)

    def test_forecast_value_can_add_on_repeated_dips_until_budget_reached(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=_threshold_risk_outcomes(),
            )
            _store_forecast(db, 29, "2026-05-05T09:11:53+08:00")
            _store_book_pair(db, "yes28", old_ask=0.60, new_ask=0.60)
            _store_book_pair(db, "yes30", old_ask=0.10, new_ask=0.10)
            store_orderbook(
                db,
                "yes29",
                OrderBook("yes29", bids=[(0.25, 1000)], asks=[(0.30, 100)], tick_size=0.01, min_order_size=5),
            )

            first = process_forecast_entries(
                db, date(2026, 5, 4), today_hkt=date(2026, 5, 4)
            )
            store_orderbook(
                db,
                "yes29",
                OrderBook("yes29", bids=[(0.25, 1000)], asks=[(0.29, 1000)], tick_size=0.01, min_order_size=5),
            )
            second = process_forecast_entries(
                db, date(2026, 5, 4), today_hkt=date(2026, 5, 4)
            )

            self.assertEqual(first.buys_filled, 1)
            self.assertEqual(second.buys_filled, 1)
            invested = db.execute(
                """
                select net_shares * avg_price
                from paper_positions
                where outcome_id = 'yes29'
                """
            ).fetchone()[0]
            self.assertAlmostEqual(invested, 250.0)
            filled_orders = db.execute(
                """
                select count(*), sum(simulated_fill_size_usd)
                from paper_orders
                where outcome_id = 'yes29' and status = 'filled'
                """
            ).fetchone()
            self.assertEqual(filled_orders[0], 2)
            self.assertAlmostEqual(filled_orders[1], 250.0)

    def test_forecast_value_same_value_and_price_is_processed_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=_threshold_risk_outcomes(),
            )
            _store_forecast(db, 29, "2026-05-05T09:11:53+08:00")
            _store_book_pair(db, "yes28", old_ask=0.60, new_ask=0.60)
            _store_book_pair(db, "yes30", old_ask=0.10, new_ask=0.10)
            _store_book_pair(db, "yes29", old_ask=0.23, new_ask=0.23)

            first = process_forecast_entries(
                db, date(2026, 5, 4), today_hkt=date(2026, 5, 4)
            )
            _store_forecast(db, 29, "2026-05-05T09:12:53+08:00")
            second = process_forecast_entries(
                db, date(2026, 5, 4), today_hkt=date(2026, 5, 4)
            )

            self.assertEqual(first.buys_filled, 1)
            self.assertEqual(second.signals, 0)
            self.assertIn(
                "forecast value skipped: 2026-05-04 29°C already processed at ask=0.230",
                second.notes,
            )
            event_count = db.execute(
                """
                select count(*)
                from paper_decisions
                where event_type = 'forecast_value' and action = 'EVENT'
                """
            ).fetchone()[0]
            self.assertEqual(event_count, 1)

    def test_forecast_value_ignores_floating_point_dust_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(
                Path(tmp) / "test.db",
                outcomes=_threshold_risk_outcomes(),
            )
            _store_forecast(db, 29, "2026-05-05T09:11:53+08:00")
            _store_book_pair(db, "yes28", old_ask=0.60, new_ask=0.60)
            _store_book_pair(db, "yes29", old_ask=0.30, new_ask=0.30)
            _store_book_pair(db, "yes30", old_ask=0.10, new_ask=0.10)
            db.execute(
                """
                insert into paper_positions
                (outcome_id, net_shares, avg_price, realized_pnl, updated_at_utc)
                values (?, ?, ?, 0, ?)
                """,
                (
                    "yes29",
                    833.3333333333333,
                    0.29999999999999993,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            db.commit()

            result = process_forecast_entries(
                db, date(2026, 5, 4), today_hkt=date(2026, 5, 4)
            )

            self.assertEqual(result.buys_filled, 0)
            order_count = db.execute("select count(*) from paper_orders").fetchone()[0]
            self.assertEqual(order_count, 0)
            decision = db.execute(
                """
                select status, reason from paper_decisions
                where action = 'BUY'
                order by id desc limit 1
                """
            ).fetchone()
            self.assertEqual(decision["status"], "ignored")
            self.assertEqual(decision["reason"], "position budget reached")

    def test_flw_forecasts_do_not_trigger_active_forecast_trading(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            _store_forecast(
                db,
                28,
                "2026-05-04T00:45:00+08:00",
                source_type="flw_page",
            )
            _store_forecast(
                db,
                29,
                "2026-05-04T01:45:00+08:00",
                source_type="flw_page",
            )
            _store_book_pair(db, "yes29", old_ask=0.40, new_ask=0.405)

            result = process_forecast_entries(db, date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 0)
            self.assertIn("need two decimal forecast highs", result.notes)

    def test_dashboard_reports_key_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            today = datetime.now(HKT).date()
            _store_forecast(
                db,
                25,
                f"{today.isoformat()}T00:45:00+08:00",
                forecast_date=today,
            )
            _store_observation(db, 24.5)

            output = render_dashboard(db)

            self.assertIn("latest forecast high: 25.0", output)
            self.assertIn("latest since-midnight max: 24.5", output)
            self.assertIn("buy orders filled/missed:", output)


def _seed_market(path: Path, outcomes=None, target_date=date(2026, 5, 4)):
    db = connect(path)
    migrate(db)
    market = TemperatureMarket(
        event_id="event",
        event_slug=f"highest-temperature-in-hong-kong-on-{target_date.isoformat()}",
        title=f"Highest temperature in Hong Kong on {target_date.isoformat()}?",
        target_date=target_date,
        outcomes=outcomes
        or [
            Outcome(
                market_id="m29",
                label="29°C",
                predicate=parse_outcome_label("29°C"),
                yes_token_id="yes29",
                no_token_id="no29",
            )
        ],
    )
    store_polymarket_event(db, market)
    return db


def _store_lowest_market(db, outcomes=None, target_date=date(2026, 5, 4)):
    store_polymarket_event(
        db,
        TemperatureMarket(
            event_id="lowest-event",
            event_slug=f"lowest-temperature-in-hong-kong-on-{target_date.isoformat()}",
            title=f"Lowest temperature in Hong Kong on {target_date.isoformat()}?",
            target_date=target_date,
            outcomes=outcomes or _threshold_risk_outcomes(),
        ),
    )


def _forecast_bucket_outcomes():
    return [
        Outcome(
            market_id="m22",
            label="22°C",
            predicate=parse_outcome_label("22°C"),
            yes_token_id="yes22",
            no_token_id="no22",
        ),
        Outcome(
            market_id="m23",
            label="23°C",
            predicate=parse_outcome_label("23°C"),
            yes_token_id="yes23",
            no_token_id="no23",
        ),
        Outcome(
            market_id="m24",
            label="24°C",
            predicate=parse_outcome_label("24°C"),
            yes_token_id="yes24",
            no_token_id="no24",
        ),
        Outcome(
            market_id="m25",
            label="25°C",
            predicate=parse_outcome_label("25°C"),
            yes_token_id="yes25",
            no_token_id="no25",
        ),
    ]


def _threshold_risk_outcomes():
    return [
        Outcome(
            market_id="m28",
            label="28°C",
            predicate=parse_outcome_label("28°C"),
            yes_token_id="yes28",
            no_token_id="no28",
        ),
        Outcome(
            market_id="m29",
            label="29°C",
            predicate=parse_outcome_label("29°C"),
            yes_token_id="yes29",
            no_token_id="no29",
        ),
        Outcome(
            market_id="m30",
            label="30°C",
            predicate=parse_outcome_label("30°C"),
            yes_token_id="yes30",
            no_token_id="no30",
        ),
    ]


def _store_forecast(
    db,
    high: float,
    update_time: str,
    forecast_date=date(2026, 5, 4),
    source_type="ocf_station",
):
    snapshot = store_raw_snapshot(db, "hko", f"forecast-{update_time}", str(high))
    store_hko_forecasts(
        db,
        snapshot.id,
        [
            HkoForecast(
                source_type=source_type,
                forecast_date_hkt=forecast_date,
                forecast_min_c=None,
                forecast_max_c=high,
                update_time=update_time,
            )
        ],
    )
    if source_type == "ocf_station":
        store_ocf_forecast_samples(
            db,
            snapshot.id,
            [
                OcfForecastSample(
                    forecast_date_hkt=forecast_date,
                    forecast_min_c=None,
                    forecast_max_c=int(high),
                    raw_min_c=None,
                    raw_max_c=high,
                    hourly_temperatures=[
                        {
                            "forecast_hour_hkt": f"{forecast_date.isoformat()}T14:00:00+08:00",
                            "temperature_c": high,
                        }
                    ],
                    raw={"LastModified": update_time},
                )
            ],
        )


def _store_forecast_range(
    db,
    low: float,
    high: float,
    update_time: str,
    forecast_date=date(2026, 5, 4),
):
    snapshot = store_raw_snapshot(db, "hko", f"forecast-{update_time}", f"{low}-{high}")
    store_hko_forecasts(
        db,
        snapshot.id,
        [
            HkoForecast(
                source_type="ocf_station",
                forecast_date_hkt=forecast_date,
                forecast_min_c=int(low),
                forecast_max_c=int(high),
                update_time=update_time,
            )
        ],
    )
    store_ocf_forecast_samples(
        db,
        snapshot.id,
        [
            OcfForecastSample(
                forecast_date_hkt=forecast_date,
                forecast_min_c=int(low),
                forecast_max_c=int(high),
                raw_min_c=low,
                raw_max_c=high,
                hourly_temperatures=[
                    {
                        "forecast_hour_hkt": f"{forecast_date.isoformat()}T06:00:00+08:00",
                        "temperature_c": low,
                    },
                    {
                        "forecast_hour_hkt": f"{forecast_date.isoformat()}T14:00:00+08:00",
                        "temperature_c": high,
                    },
                ],
                raw={"LastModified": update_time},
            )
        ],
    )


def _store_observation(db, high: float, observed_date=date(2026, 5, 4)):
    snapshot = store_raw_snapshot(db, "hko", f"obs-{high}", str(high))
    store_hko_observation(
        db,
        snapshot.id,
        HkoObservation(
            observed_at_hkt=datetime(
                observed_date.year, observed_date.month, observed_date.day, 12, 0, tzinfo=HKT
            ),
            station="HK Observatory",
            since_midnight_max_c=high,
            since_midnight_min_c=21.0,
            raw={},
        ),
    )


def _store_aws_actual(
    db,
    high: float,
    low: float = 21.0,
    temperature: float | None = None,
    hour: int = 12,
    minute: int = 0,
    observed_date=date(2026, 5, 4),
):
    snapshot = store_raw_snapshot(db, "hko", f"aws-actual-{high}", str(high))
    store_hko_current_temperature(
        db,
        snapshot.id,
        HkoCurrentTemperature(
            observed_at_hkt=datetime(
                observed_date.year,
                observed_date.month,
                observed_date.day,
                hour,
                minute,
                tzinfo=HKT,
            ),
            station="HKO",
            temperature_c=temperature if temperature is not None else high,
            since_midnight_max_c=high,
            since_midnight_min_c=low,
            raw={},
        ),
    )


def _store_min_observation(
    db, low: float, hour: int, observed_date=date(2026, 5, 4)
):
    snapshot = store_raw_snapshot(db, "hko", f"obs-low-{hour}-{low}", str(low))
    store_hko_observation(
        db,
        snapshot.id,
        HkoObservation(
            observed_at_hkt=datetime(
                observed_date.year, observed_date.month, observed_date.day, hour, 0, tzinfo=HKT
            ),
            station="HK Observatory",
            since_midnight_max_c=33.0,
            since_midnight_min_c=low,
            raw={},
        ),
    )


def _store_book_pair(db, token_id: str, old_ask: float, new_ask: float):
    store_orderbook(
        db,
        token_id,
        OrderBook(token_id, bids=[(old_ask - 0.02, 1000)], asks=[(old_ask, 1000)], tick_size=0.01, min_order_size=5),
    )
    store_orderbook(
        db,
        token_id,
        OrderBook(token_id, bids=[(new_ask - 0.02, 1000)], asks=[(new_ask, 1000)], tick_size=0.01, min_order_size=5),
    )


def _store_late_peak_hourly_forecast(db, forecast_date: date, peak: float):
    snapshot = store_raw_snapshot(db, "hko", f"ocf-hourly-{forecast_date}", str(peak))
    store_ocf_forecast_samples(
        db,
        snapshot.id,
        [
            OcfForecastSample(
                forecast_date_hkt=forecast_date,
                forecast_min_c=None,
                forecast_max_c=int(peak),
                raw_min_c=None,
                raw_max_c=peak,
                hourly_temperatures=[
                    {
                        "forecast_hour_hkt": f"{forecast_date.isoformat()}T18:00:00+08:00",
                        "temperature_c": peak - 0.8,
                    },
                    {
                        "forecast_hour_hkt": f"{forecast_date.isoformat()}T20:00:00+08:00",
                        "temperature_c": peak - 0.4,
                    },
                    {
                        "forecast_hour_hkt": f"{forecast_date.isoformat()}T21:00:00+08:00",
                        "temperature_c": peak - 0.2,
                    },
                    {
                        "forecast_hour_hkt": f"{forecast_date.isoformat()}T23:00:00+08:00",
                        "temperature_c": peak,
                    },
                ],
                raw={"LastModified": int(f"{forecast_date:%Y%m%d}151153")},
            )
        ],
    )


def _store_peak_decline_hourly_forecast(
    db, forecast_date: date, peak: float, fetched_at_hkt: str | None = None
):
    snapshot = store_raw_snapshot(
        db, "hko", f"ocf-hourly-peak-decline-{forecast_date}", str(peak)
    )
    store_ocf_forecast_samples(
        db,
        snapshot.id,
        [
            OcfForecastSample(
                forecast_date_hkt=forecast_date,
                forecast_min_c=None,
                forecast_max_c=int(peak),
                raw_min_c=None,
                raw_max_c=peak,
                hourly_temperatures=[
                    {
                        "forecast_hour_hkt": f"{forecast_date.isoformat()}T13:00:00+08:00",
                        "temperature_c": peak - 0.3,
                    },
                    {
                        "forecast_hour_hkt": f"{forecast_date.isoformat()}T14:00:00+08:00",
                        "temperature_c": peak,
                    },
                    {
                        "forecast_hour_hkt": f"{forecast_date.isoformat()}T15:00:00+08:00",
                        "temperature_c": peak - 0.4,
                    },
                    {
                        "forecast_hour_hkt": f"{forecast_date.isoformat()}T16:00:00+08:00",
                        "temperature_c": peak - 0.8,
                    },
                ],
                raw={"LastModified": int(f"{forecast_date:%Y%m%d}131153")},
            )
        ],
    )
    _set_latest_ocf_sample_fetched_at(
        db, fetched_at_hkt or f"{forecast_date.isoformat()}T13:11:53+08:00"
    )


def _store_successive_hour_not_below_actual_forecast(
    db, forecast_date: date, actual: float, fetched_at_hkt: str | None = None
):
    snapshot = store_raw_snapshot(
        db, "hko", f"ocf-hourly-future-not-below-{forecast_date}", str(actual)
    )
    store_ocf_forecast_samples(
        db,
        snapshot.id,
        [
            OcfForecastSample(
                forecast_date_hkt=forecast_date,
                forecast_min_c=None,
                forecast_max_c=int(actual),
                raw_min_c=None,
                raw_max_c=actual,
                hourly_temperatures=[
                    {
                        "forecast_hour_hkt": f"{forecast_date.isoformat()}T13:00:00+08:00",
                        "temperature_c": actual - 0.5,
                    },
                    {
                        "forecast_hour_hkt": f"{forecast_date.isoformat()}T14:00:00+08:00",
                        "temperature_c": actual - 0.3,
                    },
                    {
                        "forecast_hour_hkt": f"{forecast_date.isoformat()}T15:00:00+08:00",
                        "temperature_c": actual,
                    },
                ],
                raw={"LastModified": int(f"{forecast_date:%Y%m%d}131154")},
            )
        ],
    )
    if fetched_at_hkt is not None:
        _set_latest_ocf_sample_fetched_at(db, fetched_at_hkt)


def _store_current_hour_matches_actual_future_declines_forecast(
    db, forecast_date: date, actual: float, fetched_at_hkt: str | None = None
):
    snapshot = store_raw_snapshot(
        db, "hko", f"ocf-hourly-current-actual-{forecast_date}", str(actual)
    )
    store_ocf_forecast_samples(
        db,
        snapshot.id,
        [
            OcfForecastSample(
                forecast_date_hkt=forecast_date,
                forecast_min_c=None,
                forecast_max_c=int(actual),
                raw_min_c=None,
                raw_max_c=actual,
                hourly_temperatures=[
                    {
                        "forecast_hour_hkt": f"{forecast_date.isoformat()}T14:00:00+08:00",
                        "temperature_c": actual,
                    },
                    {
                        "forecast_hour_hkt": f"{forecast_date.isoformat()}T15:00:00+08:00",
                        "temperature_c": actual - 0.4,
                    },
                    {
                        "forecast_hour_hkt": f"{forecast_date.isoformat()}T16:00:00+08:00",
                        "temperature_c": actual - 0.8,
                    },
                ],
                raw={"LastModified": int(f"{forecast_date:%Y%m%d}140100")},
            )
        ],
    )
    if fetched_at_hkt is not None:
        _set_latest_ocf_sample_fetched_at(db, fetched_at_hkt)


def _set_latest_ocf_sample_fetched_at(db, fetched_at_hkt: str):
    fetched_at_utc = datetime.fromisoformat(fetched_at_hkt).astimezone(timezone.utc).isoformat()
    db.execute(
        "update ocf_forecast_samples set fetched_at_utc = ? where id = (select max(id) from ocf_forecast_samples)",
        (fetched_at_utc,),
    )
    db.commit()


def _store_below_bucket_hourly_forecast(db, forecast_date: date, bucket: float):
    snapshot = store_raw_snapshot(
        db, "hko", f"ocf-hourly-below-{forecast_date}", str(bucket)
    )
    store_ocf_forecast_samples(
        db,
        snapshot.id,
        [
            OcfForecastSample(
                forecast_date_hkt=forecast_date,
                forecast_min_c=None,
                forecast_max_c=int(bucket),
                raw_min_c=None,
                raw_max_c=bucket,
                hourly_temperatures=[
                    {
                        "forecast_hour_hkt": f"{forecast_date.isoformat()}T18:00:00+08:00",
                        "temperature_c": bucket - 0.6,
                    },
                    {
                        "forecast_hour_hkt": f"{forecast_date.isoformat()}T20:00:00+08:00",
                        "temperature_c": bucket - 1.0,
                    },
                    {
                        "forecast_hour_hkt": f"{forecast_date.isoformat()}T21:00:00+08:00",
                        "temperature_c": bucket - 0.5,
                    },
                    {
                        "forecast_hour_hkt": f"{forecast_date.isoformat()}T23:00:00+08:00",
                        "temperature_c": bucket - 0.3,
                    },
                ],
                raw={"LastModified": int(f"{forecast_date:%Y%m%d}151153")},
            )
        ],
    )


def _store_early_breach_hourly_forecast(db, forecast_date: date, bucket: float):
    snapshot = store_raw_snapshot(
        db, "hko", f"ocf-hourly-early-{forecast_date}", str(bucket)
    )
    store_ocf_forecast_samples(
        db,
        snapshot.id,
        [
            OcfForecastSample(
                forecast_date_hkt=forecast_date,
                forecast_min_c=None,
                forecast_max_c=int(bucket),
                raw_min_c=None,
                raw_max_c=bucket,
                hourly_temperatures=[
                    {
                        "forecast_hour_hkt": f"{forecast_date.isoformat()}T18:00:00+08:00",
                        "temperature_c": bucket - 0.6,
                    },
                    {
                        "forecast_hour_hkt": f"{forecast_date.isoformat()}T20:00:00+08:00",
                        "temperature_c": bucket,
                    },
                    {
                        "forecast_hour_hkt": f"{forecast_date.isoformat()}T21:00:00+08:00",
                        "temperature_c": bucket,
                    },
                    {
                        "forecast_hour_hkt": f"{forecast_date.isoformat()}T23:00:00+08:00",
                        "temperature_c": bucket,
                    },
                ],
                raw={"LastModified": int(f"{forecast_date:%Y%m%d}151153")},
            )
        ],
    )


if __name__ == "__main__":
    unittest.main()
