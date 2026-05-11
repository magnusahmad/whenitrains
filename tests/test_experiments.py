import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from whenitrains.experiments.backtest import run_experiment_backtest_day
from whenitrains.experiments.config import ExperimentConfig
from whenitrains.experiments.experimental_scheduler import run_experiment_tick
from whenitrains.experiments.store import create_experiment_run
from whenitrains.hko import HkoForecast, OcfForecastSample
from whenitrains.markets import parse_outcome_label
from whenitrains.polymarket import OrderBook, Outcome, TemperatureMarket
from whenitrains.storage import (
    connect,
    migrate,
    store_hko_forecasts,
    store_ocf_forecast_samples,
    store_orderbook,
    store_polymarket_event,
    store_raw_snapshot,
)


class ExperimentHarnessTests(unittest.TestCase):
    def test_migration_creates_experiment_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            self.addCleanup(db.close)
            migrate(db)

            tables = {
                row["name"]
                for row in db.execute(
                    "select name from sqlite_master where type = 'table'"
                )
            }

            self.assertIn("experiment_runs", tables)
            self.assertIn("experiment_decisions", tables)
            self.assertIn("experiment_orders", tables)
            self.assertIn("experiment_positions", tables)
            self.assertIn("experiment_metrics", tables)

    def test_experiment_tick_writes_only_experiment_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            self.addCleanup(db.close)
            migrate(db)
            _seed_forecast_market_and_book(db, forecast=26, ask=0.25)
            config = ExperimentConfig()
            run_id = create_experiment_run(
                db, config, target_start=date(2026, 5, 4), target_end=date(2026, 5, 4)
            )

            result = run_experiment_tick(
                db, run_id=run_id, config=config, target_date=date(2026, 5, 4)
            )

            self.assertEqual(result.orders_filled, 1)
            self.assertEqual(
                db.execute("select count(*) from experiment_orders").fetchone()[0], 1
            )
            self.assertEqual(
                db.execute("select count(*) from experiment_positions").fetchone()[0], 1
            )
            self.assertEqual(db.execute("select count(*) from paper_orders").fetchone()[0], 0)
            self.assertEqual(
                db.execute("select count(*) from paper_positions").fetchone()[0], 0
            )

    def test_experiment_tick_dedupes_same_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            self.addCleanup(db.close)
            migrate(db)
            _seed_forecast_market_and_book(db, forecast=26, ask=0.25)
            config = ExperimentConfig()
            run_id = create_experiment_run(db, config)

            first = run_experiment_tick(
                db, run_id=run_id, config=config, target_date=date(2026, 5, 4)
            )
            second = run_experiment_tick(
                db, run_id=run_id, config=config, target_date=date(2026, 5, 4)
            )

            self.assertEqual(first.orders_filled, 1)
            self.assertEqual(second.orders_filled, 0)
            self.assertEqual(
                db.execute("select count(*) from experiment_orders").fetchone()[0], 1
            )

    def test_experiment_backtest_replays_without_paper_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.db"
            replay = Path(tmp) / "replay.db"
            db = connect(source)
            migrate(db)
            _seed_forecast_market_and_book(db, forecast=26, ask=0.25)
            db.close()

            result = run_experiment_backtest_day(
                source,
                date(2026, 5, 4),
                ExperimentConfig(),
                replay_db=replay,
                tick_source="data",
            )

            self.assertEqual(result.filled_order_count, 1)
            self.assertEqual(result.open_position_count, 1)
            self.assertGreater(result.cost_basis, 0)
            replay_db = connect(replay)
            self.addCleanup(replay_db.close)
            self.assertEqual(
                replay_db.execute("select count(*) from paper_orders").fetchone()[0], 0
            )
            self.assertEqual(
                replay_db.execute("select count(*) from experiment_orders").fetchone()[0], 1
            )

    def test_config_rejects_unknown_keys(self):
        with self.assertRaises(ValueError):
            ExperimentConfig.from_json_text(json.dumps({"unknown": True}))


def _seed_forecast_market_and_book(db, forecast: float, ask: float) -> None:
    market = TemperatureMarket(
        event_id="event",
        event_slug="highest-temperature-in-hong-kong-on-2026-05-04",
        title="Highest temperature in Hong Kong on 2026-05-04?",
        target_date=date(2026, 5, 4),
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
            Outcome(
                market_id="m27",
                label="27°C or higher",
                predicate=parse_outcome_label("27°C or higher"),
                yes_token_id="yes27",
                no_token_id="no27",
            ),
        ],
    )
    store_polymarket_event(db, market)
    snapshot = store_raw_snapshot(db, "hko", "forecast", str(forecast))
    db.execute(
        "update raw_snapshots set fetched_at_utc = ? where id = ?",
        ("2026-05-03T16:00:00+00:00", snapshot.id),
    )
    store_hko_forecasts(
        db,
        snapshot.id,
        [
            HkoForecast(
                source_type="ocf_station",
                forecast_date_hkt=date(2026, 5, 4),
                forecast_min_c=None,
                forecast_max_c=forecast,
                update_time="2026-05-04T00:00:00+08:00",
            )
        ],
    )
    store_ocf_forecast_samples(
        db,
        snapshot.id,
        [
            OcfForecastSample(
                forecast_date_hkt=date(2026, 5, 4),
                forecast_min_c=None,
                forecast_max_c=int(forecast),
                raw_min_c=None,
                raw_max_c=forecast,
                hourly_temperatures=[],
                raw={},
            )
        ],
    )
    db.execute(
        "update ocf_forecast_samples set fetched_at_utc = ? where snapshot_id = ?",
        ("2026-05-03T16:00:00+00:00", snapshot.id),
    )
    store_orderbook(
        db,
        "yes26",
        OrderBook(
            "yes26",
            bids=[(ask - 0.02, 1000)],
            asks=[(ask, 1000)],
            tick_size=0.01,
            min_order_size=5,
        ),
    )
    db.execute(
        "update orderbook_snapshots set fetched_at_utc = ? where id = (select max(id) from orderbook_snapshots)",
        ("2026-05-03T16:00:00+00:00",),
    )
    db.commit()


if __name__ == "__main__":
    unittest.main()
