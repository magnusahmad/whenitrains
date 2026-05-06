import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from whenitrains.hko import HKT, HkoForecast, HkoObservation, OcfForecastSample
from whenitrains.markets import parse_outcome_label
from whenitrains.polymarket import OrderBook, Outcome, TemperatureMarket
from whenitrains.runner import (
    process_actual_entries,
    process_all_forecast_entries,
    process_forecast_entries,
    process_open_position_exits,
    render_dashboard,
    run_paper_tick,
)
from whenitrains.storage import (
    connect,
    migrate,
    store_hko_forecasts,
    store_hko_observation,
    store_ocf_forecast_samples,
    store_orderbook,
    store_polymarket_event,
    store_raw_snapshot,
)


class RunnerTests(unittest.TestCase):
    def test_forecast_change_buys_stale_affected_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            _store_forecast(db, 28, "2026-05-04T00:45:00+08:00")
            _store_forecast(db, 29, "2026-05-04T01:45:00+08:00")
            _store_book_pair(db, "yes29", old_ask=0.40, new_ask=0.405)
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
            _store_book_pair(db, "yes29", old_ask=0.40, new_ask=0.60)
            _store_book_pair(db, "no29", old_ask=0.60, new_ask=0.60)

            result = process_forecast_entries(db, date(2026, 5, 4), today_hkt=date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 1)
            order = db.execute(
                "select simulated_fill_price from paper_orders where status = 'filled' order by id desc limit 1"
            ).fetchone()
            self.assertEqual(order["simulated_fill_price"], 0.60)

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
            _store_book_pair(db, "yes29", old_ask=0.40, new_ask=0.405)
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

    def test_forecast_change_duplicate_hko_update_is_processed_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            _store_forecast(db, 28, "2026-05-04T00:45:00+08:00")
            _store_forecast(db, 29, "2026-05-04T01:45:00+08:00")
            _store_book_pair(db, "yes29", old_ask=0.40, new_ask=0.405)

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
            _store_book_pair(db, "future_yes29", old_ask=0.40, new_ask=0.405)
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

    def test_actual_cross_only_trades_when_actual_exceeds_forecast_max(self):
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
            _store_observation(db, 29.0)
            _store_observation(db, 30.0)
            _store_book_pair(db, "yes30", old_ask=0.40, new_ask=0.405)

            result = process_actual_entries(db, date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 0)
            self.assertEqual(result.signals, 0)

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
            self.assertEqual(decision["reason"], "position invalidated by forecast change")

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

    def test_exit_loop_sells_late_day_peak_bucket_position(self):
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
            self.assertIn("need two forecast highs", result.notes)

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


if __name__ == "__main__":
    unittest.main()
