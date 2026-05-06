# HK High Temp Latency Status

Last updated: 2026-05-04 HKT

## Current State

Milestones 1-5 are implemented for local paper trading:

- Milestone 1: Python project skeleton, config, CLI, test setup.
- Milestone 2: HKO ingestion/parser layer for since-midnight CSV, the OCF HKO station forecast feed, and SQLite persistence.
- Milestone 3: Polymarket event/market parsing, CLOB orderbook parsing, and SQLite persistence.
- Milestone 4: Latency signal primitives for directional impact, price-response classification, and trade candidate generation.
- Milestone 5: Paper trader with executable-depth fills, position tracking, risk rejects, and CLI-ready local storage.

Live trading remains intentionally disabled/not implemented.

## API Discovery Findings

### HKO

Since-midnight max/min CSV:

`https://data.weather.gov.hk/weatherAPI/hko_data/csdi/dataset/latest_since_midnight_maxmin_csdi_4.csv`

Source dataset page:

`https://data.gov.hk/en-data/dataset/hk-hko-rss-max-and-min-air-temp-since-midnight`

Update frequency: every 10 minutes.

Observed schema:

- `Date time (Year)`
- `Date time (Month)`
- `Date time (Day)`
- `Date time (Hour)`
- `Date time (Minute)`
- `Date time (Time Zone)`
- `Automatic Weather Station`
- `Maximum Air Temperature Since Midnight(degree Celsius)`
- `Minimum Air Temperature Since Midnight(degree Celsius)`

The resolving row uses `Automatic Weather Station = HK Observatory`.

OCF HKO station forecast feed:

Page: `https://maps.weather.gov.hk/ocf/text_e.html?mode=0&station=HKO`

Data feed: `https://maps.weather.gov.hk/ocf/dat/HKO.xml`

Observed payload:

- `LastModified`
- `StationCode`
- `DailyForecast`
- `HourlyWeatherForecast`

Findings:

- The OCF station feed is the current trading forecast source.
- The public page is JavaScript-rendered; the underlying station data feed returns JSON despite the `.xml` suffix.
- `DailyForecast[].ForecastMaximumTemperature` reproduces the displayed `Max & Min Temperature Forecast` high after nearest-integer display rounding.
- `HourlyWeatherForecast[]` reproduces the hourly `Temperature Forecast` table and is stored in the sampler table for cadence analysis.
- The old local weather forecast bulletin parser remains in tests as a fallback fixture, but it is no longer in the trading signal path.
- The Open Data API `flw` feed can lag the actual bulletin update and is removed from the trading signal path.
- The Open Data API `fnd` / 9-day forecast feed has no reliable low-latency signal pattern yet and is removed from the trading signal path.

### Polymarket

Daily HK event discovery works by exact event slug:

`https://gamma-api.polymarket.com/events?slug=highest-temperature-in-hong-kong-on-may-4-2026`

`https://gamma-api.polymarket.com/events?slug=highest-temperature-in-hong-kong-on-may-5-2026`

Findings:

- The event response contains a nested `markets` array with each displayed ladder outcome as a separate binary market.
- The direct Gamma `/markets?slug={event_slug}` lookup returns `[]`; use `/events?slug=...`.
- The daily event has `negRisk=true`.
- `groupItemTitle` is the outcome label to parse.
- `clobTokenIds` is JSON-encoded and ordered as YES token, NO token.
- `outcomes` is JSON-encoded as `["Yes", "No"]`.
- Best bid/ask are present on each nested market when available.

CLOB orderbook discovery:

`https://clob.polymarket.com/book?token_id={token_id}`

Findings:

- Response fields include `asset_id`, `bids`, `asks`, `tick_size`, `min_order_size`, `last_trade_price`, and `neg_risk`.
- Bid/ask rows are `{ "price": "...", "size": "..." }`.
- Sort asks ascending and bids descending before paper-fill simulation.

## Red/Green TDD Record

Initial red run:

```bash
python3 -m unittest discover -s tests
```

Result: failed with import errors because implementation modules did not exist yet.

Green run:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

Earlier green run:

```text
Ran 18 tests in 0.015s
OK
```

Current green run after adding the paper runner, dashboard, missed-decision logging, and actual-cross entry handling:

```text
Ran 28 tests in 0.148s
OK
```

CLI smoke checks:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db /private/tmp/whenitrains-smoke.sqlite3 init-db
PYTHONPATH=src python3 -m whenitrains.cli --db /private/tmp/whenitrains-smoke.sqlite3 fetch-hko
PYTHONPATH=src python3 -m whenitrains.cli --db /private/tmp/whenitrains-smoke.sqlite3 discover-market 2026-05-04
PYTHONPATH=src python3 -m whenitrains.cli --db /private/tmp/whenitrains-paper-smoke.sqlite3 fetch-orderbooks
PYTHONPATH=src python3 -m whenitrains.cli --db /private/tmp/whenitrains-paper-smoke.sqlite3 calc-entry '25°C' YES 100
PYTHONPATH=src python3 -m whenitrains.cli --db /private/tmp/whenitrains-paper-smoke.sqlite3 paper-buy '25°C' YES 100
PYTHONPATH=src python3 -m whenitrains.cli --db /private/tmp/whenitrains-paper-smoke.sqlite3 check-exit '25°C' YES --take-profit 0.20
PYTHONPATH=src python3 -m whenitrains.cli --db /private/tmp/whenitrains-paper-smoke.sqlite3 paper-sell '25°C' YES
PYTHONPATH=src python3 -m whenitrains.cli --db /private/tmp/whenitrains-live-paper-smoke.sqlite3 paper-loop --ticks 1 --interval 1
PYTHONPATH=src python3 -m whenitrains.cli --db /private/tmp/whenitrains-live-paper-smoke.sqlite3 dashboard
```

Results:

- DB initialization succeeded.
- HKO live snapshots were fetched and stored.
- Polymarket May 4 event discovery succeeded and stored 11 outcomes.
- Orderbooks were fetched for every May 4 YES and NO token.
- Entry calculation produced visible-depth average fill estimates.
- Paper buy persisted a token-keyed position.
- Exit check compared current bid to average entry price.
- Paper sell walked visible bid depth, closed the position, and persisted realized PnL.
- Full paper-loop smoke fetched HKO, discovered the current-day market, fetched 22 token orderbooks, and ran the decision pass.
- Dashboard smoke reported latest forecast high, latest since-midnight max, market/outcome counts, orderbook snapshots, buy/sell counters, realized PnL, executable unrealized PnL, total profit estimate, and worst-case open loss.
- Python `urllib` initially received HTTP 403 from Gamma; adding `User-Agent: whenitrains/0.1` fixed the discovery call.

## Test Coverage

### Milestone 1: Skeleton

Covered by importability of the package and CLI module smoke path.

Implementation files:

- `pyproject.toml`
- `src/whenitrains/__init__.py`
- `src/whenitrains/config.py`
- `src/whenitrains/cli.py`

### Milestone 2: HKO Ingestion

Tests:

- `tests/test_hko.py::test_parse_since_midnight_hk_observatory_row`
- `tests/test_hko.py::test_parse_flw_webpage_bulletin_time_and_range`
- `tests/test_hko.py::test_parse_flw_page_warns_when_range_missing`
- `tests/test_hko.py::test_parse_flw_page_data_json_builds_rendered_bulletin`
- `tests/test_hko.py::test_parse_ocf_station_json_daily_and_hourly_forecasts`

Implementation:

- `src/whenitrains/hko.py`

Details:

- Parses HKO Observatory since-midnight max/min CSV.
- Parses HKT timestamp from CSV fields.
- Parses the OCF HKO station forecast feed.
- Stores the displayed integer max/min forecast rows in `hko_forecasts`.
- Stores raw decimal daily max/min values and hourly forecast rows in `ocf_forecast_samples`.
- Emits `parse_warning=True` when OCF forecast date or max value is missing.

### Milestone 3: Polymarket Ingestion

Tests:

- `tests/test_markets.py::test_exact_bucket_does_not_round`
- `tests/test_markets.py::test_top_boundary_bucket`
- `tests/test_markets.py::test_bottom_boundary_bucket`
- `tests/test_markets.py::test_parse_event_markets_maps_yes_no_tokens`
- `tests/test_markets.py::test_current_day_market_filter`
- CLI smoke: `discover-market 2026-05-04` persisted 11 live outcomes.

Implementation:

- `src/whenitrains/markets.py`
- `src/whenitrains/polymarket.py`

Details:

- Parses exact buckets such as `25°C`.
- Parses top boundary buckets such as `26°C or higher`.
- Parses bottom boundary buckets such as `16°C or below`.
- Filters trading scope to the current-day market for the current-day OCF forecast signal.
- Maps Gamma nested market rows to YES/NO CLOB token IDs.
- Parses CLOB orderbook price/size strings.

### Milestone 4: Latency Signal Engine

Tests:

- `tests/test_signal.py::test_forecast_upgrade_increases_new_bucket`
- `tests/test_signal.py::test_forecast_upgrade_decreases_old_bucket`
- `tests/test_signal.py::test_far_away_longshot_is_no_material_impact`
- `tests/test_signal.py::test_price_response_collapses_all_lag_to_not_moved`
- `tests/test_engine.py::test_builds_buy_yes_candidate_when_forecast_upgrade_not_priced`

Implementation:

- `src/whenitrains/signals.py`
- `src/whenitrains/engine.py`

Details:

- Classifies directional impact as increase/decrease/no material impact.
- Uses proximity filtering to avoid far-away long shots.
- Collapses unchanged, too-small movement, and movement against event into `PRICE_NOT_MOVED_WITH_EVENT`.
- Builds `BUY_YES` / `BUY_NO` candidates only when the price has not moved with the HKO event.

### Milestone 5: Paper Trader

Tests:

- `tests/test_paper.py::test_buy_fills_through_ask_depth_and_updates_position`
- `tests/test_paper.py::test_rejects_order_over_max_size`
- `tests/test_paper.py::test_drawdown_freezes_new_entries_at_80_percent`
- `tests/test_paper.py::test_calculate_entry_uses_visible_ask_depth`
- `tests/test_paper.py::test_paper_buy_and_sell_persist_position_and_pnl`
- `tests/test_paper.py::test_calculate_exit_sells_after_max_hold_time`
- `tests/test_runner.py::test_forecast_change_buys_stale_affected_outcome`
- `tests/test_runner.py::test_actual_cross_buys_stale_gte_outcome`
- `tests/test_runner.py::test_exit_loop_sells_on_timeout`
- `tests/test_runner.py::test_tick_exits_invalidated_exact_position`
- `tests/test_runner.py::test_dashboard_reports_key_stats`

Implementation:

- `src/whenitrains/paper.py`
- `src/whenitrains/paper_db.py`
- `src/whenitrains/runner.py`

Details:

- Simulates marketable limit buys through ask depth.
- Simulates sells through bid depth.
- Updates average entry price and realized PnL.
- Rejects orders above max order size.
- Freezes new buys after the paper-mode 80% daily drawdown limit.
- Persists paper orders and paper positions keyed by CLOB token ID.
- Calculates entry quote: limit price, average fill, shares, and cost.
- Calculates exit condition using current executable bid minus average entry price.
- Exits after 10 minutes if the price has not moved favorably enough to hit take-profit.
- Runs a local autonomous paper tick/loop that fetches HKO, discovers the current-day market, refreshes current-day orderbooks, detects HKO events, writes paper decisions, places paper buys, and exits open positions.
- Logs missed buy/sell decisions to `paper_decisions`.
- Dashboard reports realized PnL, executable unrealized PnL, total profit estimate, worst-case open loss, and decision counters.

## Paper PnL And Market Impact

Paper trading cannot know exact real profit because a real order can change the market.

What the simulator does account for:

- Visible depth at the time of the snapshot.
- Average fill price through multiple ask or bid levels.
- Direct slippage from consuming displayed liquidity.
- Realized PnL from simulated proceeds minus average entry cost.

What it cannot know:

- Queue priority if we post instead of take.
- Liquidity cancellations between snapshot and order arrival.
- Other traders reacting after our order appears.
- Hidden liquidity or maker behavior.
- Whether a live order would partially fill and then move the market.

Interpretation:

- Small paper trades near top of book are the most reliable.
- Larger paper trades are useful stress tests but should be treated as rough scenario estimates.
- Before scaling live size, run small live pilot orders and compare actual fill quality against paper assumptions.

## Implementation Steps Completed

1. Added tests for HKO parsing, market semantics, storage, signal classification, and paper fills.
2. Ran tests before implementation and confirmed red state.
3. Implemented HKO parser/client primitives.
4. Implemented market predicate parser and settlement matching.
5. Implemented Polymarket event and orderbook parsing.
6. Implemented SQLite schema and raw snapshot dedupe.
7. Implemented directional impact and price response classification.
8. Implemented paper trader fills and risk controls.
9. Implemented one-shot CLI commands for DB init, HKO collection, and market discovery.
10. Ran full test suite and confirmed green state.
11. Ran live read-only CLI smoke checks for HKO and Polymarket.
12. Added API discovery findings to the spec.
13. Replaced the forecast trading input with the OCF HKO station forecast feed behind `https://maps.weather.gov.hk/ocf/text_e.html?mode=0&station=HKO`.
14. Added an OCF cadence sampler that records both the max/min daily forecast table and hourly temperature forecast table every 10 minutes for 24 hours.
15. Added response-header capture for raw snapshots and learned forecast update-minute discovery from payload `LastModified` and HTTP `Last-Modified`.
16. Added Polymarket resolution-rule guard for HK highest-temperature markets.
17. Enabled forecast-latency paper trading for future OCF forecast dates with discovered markets.

## Remaining Work

Paper-mode milestones 1-5 are complete as local building blocks, one-shot CLI commands, an autonomous local paper loop, and a polling-window scheduler. Remaining work is now the live-trading review layer and richer operations:

- Add external alerting beyond the current terminal output.
- Add live CLOB credential setup.
- Add kill switch behavior that cancels live orders.
- Reduce live drawdown from the paper-mode 80% stress-test setting.
- Add integration tests using recorded HKO/Gamma/CLOB fixtures.

## Scheduler/Alert/Dashboard Decisions

Scheduler defaults for the POC:

- HKO since-midnight max/min CSV: source updates extremely regularly every 10 minutes, typically near `:00`, `:09`, `:19`, `:29`, `:38`, `:48`, and `:58`; poll from 10:00 to 20:00 HKT only.
- HKO since-midnight max/min CSV: for each expected publication time, poll from T-1m through T+2m every 10 seconds. If the content hash changes, perform one confirmation fetch, then stop polling that window.
- HKO OCF station forecast feed: source is `https://maps.weather.gov.hk/ocf/dat/HKO.xml`, discovered from the OCF text page JavaScript. It returns JSON despite the `.xml` extension, with `DailyForecast` and `HourlyWeatherForecast`.
- HKO OCF station forecast feed: update cadence is unknown, so the interim scheduler polls every 10 minutes with a narrow 10-second window. Run `sample-ocf --interval-minutes 10 --hours 24` to collect the 24-hour cadence sample before tightening these windows.
- HKO OCF station forecast feed: every fetch stores full response headers plus HTTP `Date`, HTTP `Last-Modified`, and `ETag`. Raw snapshots are no longer deduped by content hash because unchanged payloads can still provide useful response metadata.
- HKO OCF station forecast feed: payload `LastModified` and HTTP `Last-Modified` are converted to HKT minute-of-day entries in `hko_source_update_minutes`. The scheduler includes those learned minutes as daily forecast poll windows while keeping the coarse 10-minute discovery probe.
- Polymarket/orderbooks: monitor target-day markets until the Hong Kong day ends.
- Future-date forecast trading: market discovery now runs for every OCF forecast date at or after the current HKT date. Orderbook polling covers all discovered HK high-temperature outcomes. Forecast-change entries are evaluated per target date.
- Current-day actual trading: since-midnight actual-cross entries, actual invalidation, and hold-to-maturity logic remain current-day only. Future-date positions can still exit by take-profit or 10-minute timeout, but are not invalidated by today's actual max.
- Current scheduler implementation: `paper-scheduler` evaluates HKO source windows every loop, fetches HKO only when inside the agreed windows, refreshes all discovered HK high-temperature orderbooks on a separate 15-second cadence, discovers markets for all current/future OCF forecast dates on a 5-minute cadence, and runs the paper decision pass every loop.
- Scheduler output is quiet by default: orderbook-only/no-op ticks are suppressed. It prints when HKO is fetched, a signal/trade/missed-trade occurs, or a non-noop decision is made.
- Use `paper-scheduler --verbose` to restore noisy output: every scheduler tick plus all orderbook bid/ask lines.
- HKO source polling respects the in-window 10-second cadence; unchanged HKO payloads no longer print every scheduler tick.
- Individual Polymarket CLOB orderbook fetch failures are logged as warnings and do not crash the scheduler.
- Polymarket market discovery validates resolution text against the expected HKO Daily Extract `Absolute Daily Max (deg. C)` wording. Date changes in the first sentence are allowed. Any missing/changed resolution logic prints `🚨🚨🚨 RESOLUTION RULES WARNING ... 🚨🚨🚨` and persists a critical `risk_events` row.
- Forecast-change and actual-cross trading events are keyed and processed once. Repeated scheduler ticks no longer create duplicate missed buys for the same HKO event; duplicate open-position attempts are logged as ignored rather than missed.
- Use `reset-paper --yes` to clear paper orders, positions, decisions, and signals without deleting HKO snapshots, markets, or orderbooks.
- Resolution: after the target day ends, check Polymarket once per day for final resolution.
- Scheduler must use a single-process DB lock and dedupe unchanged HKO payload hashes.
- Backoff: on HTTP 429, timeout, DNS/network failure, or repeated non-2xx responses, slow that source to 10 seconds; if failures continue, slow to 60 seconds; clear after a successful fetch plus one confirmation fetch.
- Backoff alerts are terminal/log warnings. New entries freeze if source freshness exceeds safety limits.

Stale-price window:

- Starts when a new HKO event is detected and persisted.
- Event time comes from HKO `updateTime`, HKO observation time, or local fetch time in that order.
- Entry remains eligible only while the relevant YES/NO price has not moved in the event-implied direction by the configured minimum move.
- Initial POC value: 90 seconds after event detection.
- Expiry means no new entry from that HKO event; existing positions still use take-profit, invalidation, hold-to-maturity, or risk rules.

Missed trade definitions:

- `buy_missed`: price already moved, no executable depth, below fee threshold, spread/depth guard failed, risk cap rejected, duplicate signal rejected, or stale data guard fired.
- `sell_missed`: exit condition met but no executable bid/depth, below fee threshold, stale orderbook, or risk/safety guard blocked execution.

Alerts:

- Terminal/log-only first.
- Severity levels: info, trade, warning, critical.
- Repeated identical warnings should be throttled.

Dashboard:

- Start with a terminal summary command backed by SQLite.
- Track unique HKO forecasts, latest since-midnight max, current OCF forecast max by day, discovered markets/outcomes, latest bid/ask, buys/sells placed, buys/sells missed, open positions, realized PnL, executable unrealized PnL, total profit, worst-case open loss, source freshness, decision counters, last scheduler run, and recent errors.

## OCF Forecast Source Update - May 4, 2026

Discovery:

- The rendered OCF page is `https://maps.weather.gov.hk/ocf/text_e.html?mode=0&station=HKO`.
- Its JavaScript fetches station data from `https://maps.weather.gov.hk/ocf/dat/HKO.xml`.
- The station feed returned `LastModified: 20260504131147`, `StationCode: HKO`, `DailyForecast`, and `HourlyWeatherForecast` during the smoke test.
- The daily max/min table display can be reproduced from `DailyForecast[].ForecastMaximumTemperature` and `ForecastMinimumTemperature`; for example, raw `27.1` becomes displayed high `27`.
- The sampler stores raw decimal daily values and hourly table rows in `ocf_forecast_samples`, while `hko_forecasts` stores the displayed integer forecast high used by the trading signal.
- The sampler stores response headers in `raw_snapshots`. In the smoke test, HTTP `Last-Modified: Mon, 04 May 2026 05:12:19 GMT` produced learned minute `13:12` HKT, and payload `LastModified: 20260504131147` produced learned minute `13:11` HKT.

Commands:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 sample-ocf --interval-minutes 10 --hours 24
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 sample-ocf --ticks 1 --interval-minutes 0
```

Verification:

- Parser/storage tests cover OCF daily max/min parsing and hourly temperature sample persistence.
- Header/update-minute tests cover HTTP date parsing, full raw snapshot retention, and learned scheduler minutes.
- Smoke test against HKO OCF feed stored 10 forecast rows; first row was `2026-05-04`, displayed high `27`, raw high `27.1`, update time `2026-05-04T13:11:47+08:00`.
- Full test suite: `PYTHONPATH=src python3 -m unittest discover -s tests` -> `Ran 44 tests ... OK`.

## Paper Trading Fixes - May 5, 2026

DB review findings:

- The OCF forecast source did not produce a same-source forecast high change during the sampled run. May 4 stayed at `28.0`; May 5 stayed at `24.0`.
- The observed `25.0 -> 28.0` forecast-change event was source pollution from older `flw_page`/legacy rows mixed with the active OCF source.
- The May 4 actual max crossed into the top bucket, but the `26°C or higher` YES market was already near `0.999`, leaving no useful paper-tradable upside.
- Actual-cross processing was vulnerable to missing a transition after duplicate/unchanged observation rows arrived, because it only compared the latest two observed rows.

Implementation changes:

- Active forecast discovery/trading/dashboard logic now uses `ocf_station` only. Legacy `flw_page`/`fnd` rows remain in the DB for audit but cannot trigger paper forecast trades.
- Actual-cross logic now scans all distinct increasing observed-max transitions and processes any unprocessed threshold-crossing transition, instead of only comparing the latest two rows.
- Added `Settings.max_entry_price = 0.98`; candidate buys above this executable ask are logged as missed with reason `entry price above max`.
- This prevents near-settled entries such as buying YES at `0.999`, where the upside is too small for the latency POC.

Verification:

- Added tests for ignoring FLW forecasts in active trading, scanning a past actual-cross transition after later duplicate observations, and rejecting near-settled entry prices.
- Full test suite: `PYTHONPATH=src python3 -m unittest discover -s tests` -> `Ran 52 tests ... OK`.

## Forecast-Move Strategy Update - May 5, 2026

Overnight paper run finding:

- The May 5 OCF forecast changed from `24.0` to `23.0`.
- Buying `23°C YES`, `24°C NO`, and `25°C NO` would have worked materially better than broad proximity buying.
- `22°C YES` was too far from the new forecast value and stayed near zero.
- The paper loss was dominated by selling May 5 positions against May 4 actual max data; actual max checks must be target-date scoped.

Updated entry rules:

- Forecast down, e.g. `24.0 -> 23.0`: buy only the new forecast bucket YES (`23°C YES`) and buy NO on exact/GTE values above the new forecast (`24°C NO`, `25°C NO`, etc.).
- Forecast up, e.g. `24.0 -> 25.0`: buy only the new forecast bucket YES (`25°C YES`, or the matching top bucket if applicable) and buy NO on exact/bottom values below the new forecast.
- Do not buy extra far-away YES outcomes just because the move direction weakly helps them.
- Actual-cross trading is now allowed only when the same-day actual max crosses above the current same-day OCF forecast max.

Updated exit rules:

- Removed take-profit and 10-minute timeout exits from the scheduler path.
- Positions are held until a later forecast change or same-day actual max update invalidates them.
- Forecast invalidation exits: sell YES if the new forecast no longer matches that bucket; sell NO if the new forecast now matches that bucket.
- Actual invalidation exits are same-date only; previous-day actual max values cannot invalidate future/current-day positions.

Verification:

- Added tests for forecast-down selection, forecast-up selection, forecast invalidation exits, same-date actual-cross gating, and ignoring previous-day actual transitions.
- Full test suite: `PYTHONPATH=src python3 -m unittest discover -s tests` -> `Ran 57 tests ... OK`.

## Orderbook Polling Cleanup - May 5, 2026

Finding:

- After a market date passed, stored outcomes for that date could still be polled by the scheduler.
- Polymarket CLOB returned repeated `HTTP Error 404: Not Found` for those stale tokens.

Implementation:

- Default orderbook polling now fetches only outcomes with market target dates at or after the current HKT date.
- Explicit date-specific orderbook fetches still work for debugging.

Verification:

- Added a storage test confirming past market outcomes are excluded from default active polling.
- Full test suite: `PYTHONPATH=src python3 -m unittest discover -s tests` -> `Ran 58 tests ... OK`.

## Forecast Value Entry Rule - May 5, 2026

Rule:

- HKO today/next-day max-temperature forecasts are treated as high-confidence anchors.
- If the HKO forecast bucket is cheap (`YES` ask at or below `0.30`) and the market favorite is below the forecast bucket, buy the forecast bucket `YES`.
- If the market favorite is above the forecast bucket and the forecast bucket is not the top bucket, skip the trade because the market may be pricing threshold risk just above the forecast integer.
- If the forecast bucket is the top bucket and the favorite is below it, the cheap-top-bucket buy remains valid.

Implementation:

- Added a `forecast_value` entry path that runs even when the forecast value has not changed.
- Scope is today and next-day markets only via `Settings.forecast_value_max_lead_days = 1`.
- Cheap forecast bucket threshold is configurable via `Settings.forecast_value_max_yes_ask = 0.30`.
- Existing duplicate-position and max-entry-price guards still apply.

Verification:

- Added tests for cheap forecast bucket with lower favorite, skip when favorite is above a non-top forecast bucket, and buy when the cheap forecast bucket is the top bucket.
- Full test suite: `PYTHONPATH=src python3 -m unittest discover -s tests` -> `Ran 61 tests ... OK`.

Logging update:

- Forecast-value skip notes now include the target date, forecast bucket label, current YES ask, configured cheap threshold, favorite bucket, or lead-time reason.
- Example: `forecast value skipped: 2026-05-06 27°C ask=0.460 > cheap_threshold=0.300`.
- Example: `forecast value skipped: 2026-05-07 lead_days=2 > max=1`.
- Full test suite after logging update: `PYTHONPATH=src python3 -m unittest discover -s tests` -> `Ran 63 tests ... OK`.

Sizing update:

- Forecast-value buys only consume ask depth at or below the configured cheap threshold (`0.30`).
- Order size is capped by remaining position budget: buy up to `$250` total invested in that token, not `$250` on every dip.
- Repeated dips below `0.30` can add to the same position until the `$250` budget is reached.
- Forecast invalidation exits still sell the full open position when the HKO forecast turns against that bucket.
- Full test suite after sizing update: `PYTHONPATH=src python3 -m unittest discover -s tests` -> `Ran 66 tests ... OK`.

## Forecast-Change Repricing Guard - May 5, 2026

Rule:

- Forecast-change latency entries now require both:
  - directional YES-ask movement with the event is `<= 0.20`, and
  - executable entry ask for the token being bought is `<= 0.70`.
- This is intended to avoid buying after the market has already repriced, e.g. `23°C YES` at `0.93` after a downside forecast update.
- Actual-cross trades remain governed by the broader near-settlement guard and are not constrained by the forecast-change `0.70` cap.

Implementation:

- Added `Settings.forecast_change_max_price_move = 0.20`.
- Added `Settings.forecast_change_max_entry_price = 0.70`.
- Forecast-change candidate generation skips outcomes whose directional move exceeds `0.20`.
- Forecast-change order execution only sweeps ask depth at or below `0.70`.

Verification:

- Added regression tests for:
  - skipping a forecast-change trade after a `0.21` directional move,
  - allowing a `0.20` directional move when entry is still below the cap,
  - rejecting a near-repriced `23°C YES` at `0.93`.
- Full test suite: `PYTHONPATH=src python3 -m unittest discover -s tests` -> `Ran 69 tests ... OK`.

## D+0 Hourly Forecast Accuracy Collection - May 5, 2026

Goal:

- Start collecting same-day hourly forecast-vs-actual data so we can later measure HKO OCF `h+1`, `h+2`, etc. temperature error.

Implementation:

- Added the live current-weather endpoint:
  `https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=rhrread&lang=en`.
- Parse the `temperature.data` row where `place == "Hong Kong Observatory"`.
- Store those current-temperature readings in `hko_current_observations.temperature_c`, separate from since-midnight max/min rows.
- `fetch-hko` now stores since-midnight max/min, OCF forecasts, and the current HKO temperature snapshot.
- `paper-scheduler` now collects current HKO temperature as low-priority research data: at most hourly, only on otherwise idle ticks, and after the paper trading decision path has already run.
- Added `research-hourly-accuracy`, which compares stored OCF hourly forecast rows to stored current-temperature observations by forecast target hour.
- Lead-hour semantics use ceiling math: an OCF issue at `17:02` for the `18:00` forecast hour is treated as `h+1`.

How to run:

- Collect once: `PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 fetch-hko`
- Report stored hourly accuracy: `PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 research-hourly-accuracy`
- Export report: `PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 research-hourly-accuracy --output data/research/hko_hourly_accuracy.csv`

Verification:

- Added parser, storage, scheduler-cadence, and hourly matching tests.
- Full test suite: `PYTHONPATH=src python3 -m unittest discover -s tests` -> `Ran 76 tests ... OK`.
- Live smoke against HKO endpoints succeeded in `/private/tmp/whenitrains-hourly-smoke.sqlite3`; it stored a `Hong Kong Observatory` current temp and produced an initial `h+1` match from OCF hourly forecast to actual current temp.

## SQLite DB Protection Plan - May 5, 2026

Risk:

- The live SQLite DB is under `data/`, which is intentionally gitignored.
- Git cannot recover it if an agent deletes `data/whenitrains.sqlite3`.
- Plain file copies can be inconsistent if SQLite is being written while copied.

Implemented safeguards:

- Added `backup-db`, which uses SQLite's online backup API and runs `pragma integrity_check` on the backup.
- Default backup location: `data/backups/`.
- Default retention: latest 5 backups. When a sixth backup is created, the oldest backup is deleted.
- `paper-scheduler` creates a startup backup by default before entering the polling loop.
- `reset-paper --yes` creates a backup before clearing paper state unless `--no-backup` is explicitly passed.
- Added `AGENTS.md` with hard safety rules for future coding agents:
  - do not delete `data/`, `data/backups/`, or SQLite DB files,
  - do not run broad cleanup commands in this repo,
  - use `/private/tmp/*.sqlite3` for destructive tests,
  - create a DB backup before storage/migration/reset work.

Commands:

- Manual backup: `PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 backup-db`
- Custom retention: `PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 backup-db --keep 5`
- Disposable scheduler test without backup: `PYTHONPATH=src python3 -m whenitrains.cli --db /private/tmp/test.sqlite3 paper-scheduler --ticks 1 --no-startup-backup`

Verification:

- Added a storage test that creates three backups, prunes to the latest two, and checks the backed-up DB still contains the expected rows.
