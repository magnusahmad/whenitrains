import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from whenitrains.hko import HKT, HkoForecast, HkoObservation
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

            result = process_forecast_entries(db, date(2026, 5, 4))

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

    def test_exit_loop_sells_on_timeout(self):
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

            self.assertEqual(result.sells_filled, 1)
            position = db.execute(
                "select net_shares from paper_positions where outcome_id = 'yes29'"
            ).fetchone()
            self.assertEqual(position["net_shares"], 0)

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
            _store_observation(db, 29.9)
            _store_observation(db, 30.0)
            _store_book_pair(db, "yes30", old_ask=0.40, new_ask=0.405)
            _store_book_pair(db, "no30", old_ask=0.60, new_ask=0.60)

            result = process_actual_entries(db, date(2026, 5, 4))

            self.assertEqual(result.buys_filled, 1)
            position = db.execute(
                "select net_shares from paper_positions where outcome_id = 'yes30'"
            ).fetchone()
            self.assertIsNotNone(position)

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
            _store_observation(db, 29.9)
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

    def test_dashboard_reports_key_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_market(Path(tmp) / "test.db")
            _store_forecast(db, 25, "2026-05-04T00:45:00+08:00")
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


def _store_forecast(db, high: float, update_time: str, forecast_date=date(2026, 5, 4)):
    snapshot = store_raw_snapshot(db, "hko", f"forecast-{update_time}", str(high))
    store_hko_forecasts(
        db,
        snapshot.id,
        [
            HkoForecast(
                source_type="flw_page",
                forecast_date_hkt=forecast_date,
                forecast_min_c=None,
                forecast_max_c=high,
                update_time=update_time,
            )
        ],
    )


def _store_observation(db, high: float):
    snapshot = store_raw_snapshot(db, "hko", f"obs-{high}", str(high))
    store_hko_observation(
        db,
        snapshot.id,
        HkoObservation(
            observed_at_hkt=datetime(2026, 5, 4, 12, 0, tzinfo=HKT),
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


if __name__ == "__main__":
    unittest.main()
