import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from whenitrains.backtest import run_backtest_day
from whenitrains.config import Settings
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


class BacktestTests(unittest.TestCase):
    def test_backtest_replays_forecast_change_with_asof_orderbooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.db"
            replay = Path(tmp) / "replay.db"
            db = connect(source)
            migrate(db)
            market = TemperatureMarket(
                event_id="event",
                event_slug="highest-temperature-in-hong-kong-on-2026-05-04",
                title="Highest temperature in Hong Kong on 2026-05-04?",
                target_date=date(2026, 5, 4),
                outcomes=[
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
            _store_forecast(db, 28, "2026-05-04T00:00:00+08:00")
            _store_forecast(db, 29, "2026-05-04T01:00:00+08:00")
            _store_orderbook_at(db, "yes29", "2026-05-03T16:50:00+00:00", 0.24)
            _store_orderbook_at(db, "yes29", "2026-05-03T17:00:00+00:00", 0.245)
            db.close()

            with patch.object(
                Settings, "ocf_forecast_freshness_max_age_minutes", 10_000_000
            ):
                result = run_backtest_day(
                    source,
                    date(2026, 5, 4),
                    replay_db=replay,
                    tick_source="data",
                )

            filled = [order for order in result.orders if order.status == "filled"]
            self.assertEqual(len(filled), 1)
            self.assertEqual(filled[0].label, "29°C")
            self.assertEqual(filled[0].side, "BUY_YES")


def _store_forecast(db, high: float, update_time: str) -> None:
    snapshot = store_raw_snapshot(db, "hko", f"forecast-{update_time}", str(high))
    db.execute(
        "update raw_snapshots set fetched_at_utc = ? where id = ?",
        (update_time, snapshot.id),
    )
    store_hko_forecasts(
        db,
        snapshot.id,
        [
            HkoForecast(
                source_type="ocf_station",
                forecast_date_hkt=date(2026, 5, 4),
                forecast_min_c=None,
                forecast_max_c=high,
                update_time=update_time,
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
                forecast_max_c=int(high),
                raw_min_c=None,
                raw_max_c=high,
                hourly_temperatures=[
                    {
                        "forecast_hour_hkt": "2026-05-04T14:00:00+08:00",
                        "temperature_c": high,
                    }
                ],
                raw={"LastModified": update_time},
            )
        ],
    )
    db.execute(
        "update ocf_forecast_samples set fetched_at_utc = ? where snapshot_id = ?",
        (update_time, snapshot.id),
    )


def _store_orderbook_at(
    db, token_id: str, fetched_at_utc: str, ask: float
) -> None:
    store_orderbook(
        db,
        token_id,
        OrderBook(
            token_id,
            bids=[(ask - 0.02, 1000)],
            asks=[(ask, 1000)],
            tick_size=0.01,
            min_order_size=5,
        ),
    )
    db.execute(
        "update orderbook_snapshots set fetched_at_utc = ? where id = (select max(id) from orderbook_snapshots)",
        (fetched_at_utc,),
    )
    db.commit()


if __name__ == "__main__":
    unittest.main()
