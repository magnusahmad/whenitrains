import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from whenitrains.dashboard_server import (
    INDEX_HTML,
    forecast_panels,
    hourly_actual_series,
    hourly_error_series,
    hourly_forecast_series,
    top_token_price_series,
    top_yes_price_series,
)
from whenitrains.hko import (
    HKT,
    HkoCurrentTemperature,
    HkoForecast,
    HkoObservation,
    OcfForecastSample,
)
from whenitrains.markets import parse_outcome_label
from whenitrains.polymarket import OrderBook, Outcome, TemperatureMarket
from whenitrains.storage import (
    connect,
    migrate,
    store_hko_forecasts,
    store_hko_current_temperature,
    store_hko_observation,
    store_ocf_forecast_samples,
    store_orderbook,
    store_paper_order_result,
    store_polymarket_event,
    store_raw_snapshot,
)


class DashboardServerTests(unittest.TestCase):
    def test_top_yes_price_series_returns_current_top_three_for_target_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            for token, ask in [
                ("yes24", 0.12),
                ("yes25", 0.42),
                ("yes26", 0.33),
                ("yes27", 0.21),
            ]:
                store_orderbook(
                    db,
                    token,
                    OrderBook(
                        token,
                        bids=[(ask - 0.02, 10)],
                        asks=[(ask, 10)],
                        tick_size=0.01,
                        min_order_size=5,
                    ),
                )

            series = top_yes_price_series(db, "2026-05-06")

            self.assertEqual([item["label"] for item in series], ["25°C", "26°C", "27°C"])
            self.assertEqual(series[0]["latest_yes"], 0.42)
            self.assertEqual(series[0]["points"][0]["value"], 0.42)

    def test_top_token_price_series_can_return_no_side_with_trade_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            store_orderbook(
                db,
                "no25",
                OrderBook(
                    "no25",
                    bids=[(0.58, 10)],
                    asks=[(0.60, 10)],
                    tick_size=0.01,
                    min_order_size=5,
                ),
            )
            store_paper_order_result(
                db,
                "no25",
                "BUY_NO",
                limit_price=0.60,
                size_usd=25,
                fill_price=0.60,
                fill_size_usd=25,
                status="filled",
                reason="test buy",
            )

            series = top_token_price_series(db, "2026-05-06", "NO")

            self.assertEqual(series[0]["label"], "25°C")
            self.assertEqual(series[0]["side"], "NO")
            self.assertEqual(series[0]["latest_price"], 0.60)
            self.assertEqual(series[0]["markers"][0]["text"], "B")
            self.assertEqual(series[0]["markers"][0]["price"], 0.60)

    def test_forecast_panels_split_d0_d1_d2(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            snapshot = store_raw_snapshot(db, "hko", "forecast", "{}")
            for target, high in [
                (date(2026, 5, 5), 24),
                (date(2026, 5, 6), 25),
                (date(2026, 5, 7), 26),
            ]:
                store_hko_forecasts(
                    db,
                    snapshot.id,
                    [
                        HkoForecast(
                            source_type="ocf_station",
                            forecast_date_hkt=target,
                            forecast_min_c=None,
                            forecast_max_c=high,
                            update_time="2026-05-05T12:00:00+08:00",
                            raw={"ForecastMaximumTemperature": high + 0.4},
                        )
                    ],
                )
            store_hko_observation(
                db,
                snapshot.id,
                HkoObservation(
                    observed_at_hkt=datetime(2026, 5, 5, 12, 0, tzinfo=HKT),
                    station="HK Observatory",
                    since_midnight_max_c=23.2,
                    since_midnight_min_c=21.0,
                    raw={},
                ),
            )

            payload = forecast_panels(db, today=date(2026, 5, 5))

            self.assertEqual([panel["lead_days"] for panel in payload["panels"]], [0, 1, 2])
            self.assertEqual([panel["target_date"] for panel in payload["panels"]], ["2026-05-05", "2026-05-06", "2026-05-07"])
            self.assertEqual(payload["panels"][0]["forecast"][0]["value"], 24.4)
            self.assertEqual(payload["panels"][1]["forecast"][0]["value"], 25.0)
            self.assertTrue(payload["panels"][0]["actual_max"])
            self.assertEqual(payload["panels"][1]["actual_max"], [])
            self.assertEqual(payload["token_side"], "YES")

            no_payload = forecast_panels(db, today=date(2026, 5, 5), token_side="NO")
            self.assertEqual(no_payload["token_side"], "NO")

    def test_d0_panel_includes_hourly_forecast_actual_and_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            snapshot = store_raw_snapshot(db, "hko", "ocf", "{}")
            store_ocf_forecast_samples(
                db,
                snapshot.id,
                [
                    OcfForecastSample(
                        forecast_date_hkt=date(2026, 5, 6),
                        forecast_min_c=23,
                        forecast_max_c=25,
                        raw_min_c=23.0,
                        raw_max_c=25.0,
                        hourly_temperatures=[
                            {
                                "forecast_hour_hkt": "2026-05-06T13:00:00+08:00",
                                "temperature_c": 24,
                            },
                            {
                                "forecast_hour_hkt": "2026-05-06T14:00:00+08:00",
                                "temperature_c": 25,
                            },
                        ],
                        raw={"LastModified": 20260506121145},
                    )
                ],
            )
            actual_snapshot = store_raw_snapshot(db, "hko", "rhrread", "{}")
            store_hko_current_temperature(
                db,
                actual_snapshot.id,
                HkoCurrentTemperature(
                    observed_at_hkt=datetime(2026, 5, 6, 13, 40, tzinfo=HKT),
                    station="Hong Kong Observatory",
                    temperature_c=24.6,
                    raw={},
                ),
            )

            panel = forecast_panels(db, today=date(2026, 5, 6))["panels"][0]

            self.assertEqual(panel["hourly_forecast"][0]["value"], 24.0)
            self.assertEqual(panel["hourly_actual"][0]["value"], 24.6)
            self.assertAlmostEqual(panel["hourly_error"][0]["value"], 0.6)
            self.assertEqual(hourly_forecast_series(db, "2026-05-06")[1]["value"], 25.0)
            actual = hourly_actual_series(db, "2026-05-06")
            self.assertEqual(actual[0]["value"], 24.6)
            self.assertAlmostEqual(
                hourly_error_series(panel["hourly_forecast"], actual)[0]["value"],
                0.6,
            )

    def test_hourly_actual_falls_back_to_since_midnight_max(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            snapshot = store_raw_snapshot(db, "hko", "obs", "{}")
            store_hko_observation(
                db,
                snapshot.id,
                HkoObservation(
                    observed_at_hkt=datetime(2026, 5, 6, 13, 50, tzinfo=HKT),
                    station="HK Observatory",
                    since_midnight_max_c=24.6,
                    since_midnight_min_c=21.6,
                    raw={},
                ),
            )

            actual = hourly_actual_series(db, "2026-05-06")

            self.assertEqual(actual[0]["value"], 24.6)

    def test_forecast_panels_limit_tradeable_tokens_but_force_include_trades(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_dashboard_db(Path(tmp) / "test.db")
            snapshot = store_raw_snapshot(db, "hko", "forecast", "{}")
            store_hko_forecasts(
                db,
                snapshot.id,
                [
                    HkoForecast(
                        source_type="ocf_station",
                        forecast_date_hkt=date(2026, 5, 6),
                        forecast_min_c=None,
                        forecast_max_c=25,
                        update_time="2026-05-05T12:00:00+08:00",
                        raw={},
                    )
                ],
            )
            for token, ask in [
                ("yes24", 0.02),
                ("yes25", 0.20),
                ("yes26", 0.40),
                ("yes27", 0.60),
                ("yes28", 0.80),
                ("yes29", 0.995),
                ("yes30", 0.005),
            ]:
                store_orderbook(
                    db,
                    token,
                    OrderBook(
                        token,
                        bids=[(max(0.0, ask - 0.01), 10)],
                        asks=[(ask, 10)],
                        tick_size=0.01,
                        min_order_size=5,
                    ),
                )
            store_paper_order_result(
                db,
                "yes24",
                "BUY_YES",
                limit_price=0.02,
                size_usd=10,
                fill_price=0.02,
                fill_size_usd=10,
                status="filled",
                reason="test buy",
            )

            panel = forecast_panels(db, today=date(2026, 5, 6))["panels"][0]

            self.assertLessEqual(len(panel["top_tokens"]), 5)
            self.assertIn("24°C", [item["label"] for item in panel["top_tokens"]])
            self.assertNotIn("29°C", [item["label"] for item in panel["top_tokens"]])
            self.assertNotIn("30°C", [item["label"] for item in panel["top_tokens"]])

    def test_dashboard_html_has_delayed_crosshair_tooltip(self):
        self.assertIn('id="chart-tooltip"', INDEX_HTML)
        self.assertIn("setTimeout(() => showTooltip(tooltipState), 1000)", INDEX_HTML)
        self.assertIn("chart.subscribeCrosshairMove", INDEX_HTML)
        self.assertIn("function chartValueAt(points, time)", INDEX_HTML)
        self.assertIn("value: chartValueAt(d.data, param.time)", INDEX_HTML)
        self.assertIn("horzLine: { visible: false, labelVisible: false }", INDEX_HTML)
        self.assertIn("priceLineVisible: false", INDEX_HTML)
        self.assertIn('id="token-side"', INDEX_HTML)
        self.assertIn("function markerOnlySeries(chart, markers)", INDEX_HTML)
        self.assertIn("s.setData(markers.map(m => ({ time: m.time, value: m.price })))", INDEX_HTML)
        self.assertIn("lineVisible: false", INDEX_HTML)
        self.assertNotIn("s.setMarkers(markers)", INDEX_HTML)
        self.assertIn("const markerSeries = markerOnlySeries", INDEX_HTML)
        self.assertIn(".trade-bubble.buy", INDEX_HTML)
        self.assertIn("function renderTradeBubbles(lead)", INDEX_HTML)
        self.assertIn("subscribeVisibleTimeRangeChange(renderAllTradeBubbles)", INDEX_HTML)
        self.assertIn("nearestTrade(d.markers, param.time)", INDEX_HTML)
        self.assertIn('/api/forecast-panels?side=${encodeURIComponent(tokenSide)}', INDEX_HTML)
        self.assertIn('"#ffb74d", "#4dd0e1"', INDEX_HTML)
        self.assertIn("Hourly forecast", INDEX_HTML)
        self.assertIn("Actual - forecast", INDEX_HTML)
        self.assertIn("d0HourlyForecastSeries.setData", INDEX_HTML)
        self.assertIn("d0HourlyErrorSeries.setData", INDEX_HTML)
        self.assertIn('data-series-key="hourlyActual"', INDEX_HTML)
        self.assertIn("legendButton(key, color", INDEX_HTML)
        self.assertIn("d0-token-${item.token_id}", INDEX_HTML)
        self.assertIn("applySeriesVisibility", INDEX_HTML)
        self.assertIn("seriesVisibility[key] = !isSeriesVisible(key)", INDEX_HTML)
        self.assertIn("mouseWheel: false", INDEX_HTML)
        self.assertIn("function installModifierWheelZoom", INDEX_HTML)
        self.assertIn("if (!event.metaKey && !event.ctrlKey) return", INDEX_HTML)
        self.assertIn("const chartTimeToUnixSeconds = (time) =>", INDEX_HTML)
        self.assertIn("tickMarkFormatter: fmtHKTTime", INDEX_HTML)
        self.assertIn("const cursorX = event.clientX - rect.left", INDEX_HTML)
        self.assertIn("const cursorLogical = chart.timeScale().coordinateToLogical(cursorX)", INDEX_HTML)
        self.assertIn("cursorLogical - nextSpan * cursorRatio", INDEX_HTML)
        self.assertIn('installModifierWheelZoom("pnl-chart", pnlChart)', INDEX_HTML)
        self.assertIn("function fitChartOnce", INDEX_HTML)
        self.assertIn("if (fittedCharts.has(key)) return", INDEX_HTML)
        self.assertIn("fittedCharts.clear()", INDEX_HTML)
        self.assertIn("pressedMouseMove: true", INDEX_HTML)
        self.assertIn("charts[lead].chart.removeSeries(s.series)", INDEX_HTML)
        self.assertIn("if (s.markerSeries) charts[lead].chart.removeSeries(s.markerSeries)", INDEX_HTML)


def _seed_dashboard_db(path: Path):
    db = connect(path)
    migrate(db)
    store_polymarket_event(
        db,
        TemperatureMarket(
            event_id="event",
            event_slug="highest-temperature-in-hong-kong-on-2026-05-06",
            title="Highest temperature in Hong Kong on 2026-05-06?",
            target_date=date(2026, 5, 6),
            outcomes=[
                Outcome(
                    market_id=f"m{temp}",
                    label=f"{temp}°C",
                    predicate=parse_outcome_label(f"{temp}°C"),
                    yes_token_id=f"yes{temp}",
                    no_token_id=f"no{temp}",
                )
                for temp in [24, 25, 26, 27, 28, 29, 30]
            ],
        ),
    )
    return db


if __name__ == "__main__":
    unittest.main()
