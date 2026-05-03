# HK High Temp Latency Status

Last updated: 2026-05-04 HKT

## Current State

Milestones 1-5 are implemented for local paper trading:

- Milestone 1: Python project skeleton, config, CLI, test setup.
- Milestone 2: HKO ingestion/parser layer for since-midnight CSV, the current-day HKO bulletin webpage scraper, and SQLite persistence.
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

Current-day local weather forecast bulletin webpage:

`https://www.weather.gov.hk/en/wxinfo/currwx/flw.htm`

Observed text patterns:

- `Bulletin updated at HH:MM HKT DD/Mon/YYYY`
- `between {min} and {max} degrees`
- `ranging between {min} and {max} degrees`

Findings:

- This webpage is the source to scrape for the current-day forecast high.
- The public HTML is Vue-rendered. It currently loads the rendered bulletin fields from `https://www.weather.gov.hk/json/DYN_DAT_MINDS_FLW.json`; when the static HTML shell lacks the bulletin text, the scraper fetches that page data payload, reconstructs the bulletin text, and applies the same `Bulletin updated at ...` and `between ... degrees` patterns.
- The Open Data API `flw` feed can lag the actual bulletin update and is removed from the trading signal path.
- The Open Data API `fnd` / 9-day forecast feed has no reliable low-latency signal pattern yet and is removed from the trading signal path.
- Because this bulletin only provides the current-day forecast high, paper trading is limited to the current-day market from midnight HKT onward.

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

Current green run after replacing API forecast ingestion with the bulletin webpage scraper:

```text
Ran 23 tests in 0.030s
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
PYTHONPATH=src python3 -m whenitrains.cli --db /private/tmp/whenitrains-paper-smoke.sqlite3 check-exit '25°C' YES --take-profit 0.03
PYTHONPATH=src python3 -m whenitrains.cli --db /private/tmp/whenitrains-paper-smoke.sqlite3 paper-sell '25°C' YES
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

Implementation:

- `src/whenitrains/hko.py`

Details:

- Parses HKO Observatory since-midnight max/min CSV.
- Parses HKT timestamp from CSV fields.
- Scrapes/parses the current-day HKO bulletin webpage.
- Falls back to the webpage's Vue data payload when the static HTML contains only template placeholders.
- Parses bulletin update datetime from `Bulletin updated at HH:MM HKT DD/Mon/YYYY`.
- Parses the current-day high from `between {min} and {max} degrees` or `ranging between {min} and {max} degrees`.
- Emits `parse_warning=True` when the update-time or range pattern is missing.

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
- Filters trading scope to the current-day market for the current-day bulletin signal.
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

Implementation:

- `src/whenitrains/paper.py`
- `src/whenitrains/paper_db.py`

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

## Remaining Work

Paper-mode milestones 1-5 are complete as local building blocks and one-shot CLI commands. The remaining work is the next layer of productionization:

- Build the long-running scheduler around the implemented one-shot commands.
- Add persisted paper-order write paths from the live strategy loop, not just the `PaperTrader` domain object.
- Add alerting.
- Add live CLOB credential setup.
- Add kill switch behavior that cancels live orders.
- Reduce live drawdown from the paper-mode 80% stress-test setting.
- Add integration tests using recorded HKO/Gamma/CLOB fixtures.

## Scheduler/Alert/Dashboard Decisions

Scheduler defaults for the POC:

- HKO since-midnight max/min CSV: source updates extremely regularly every 10 minutes, typically near `:00`, `:09`, `:19`, `:29`, `:38`, `:48`, and `:58`; poll from 10:00 to 20:00 HKT only.
- HKO since-midnight max/min CSV: for each expected publication time, poll from T-1m through T+2m every 10 seconds. If the content hash changes, perform one confirmation fetch, then stop polling that window.
- HKO local weather forecast bulletin webpage: expected updates are 00:00 HKT, 45 minutes past each hour, 16:15 HKT, and 23:15 HKT. For each expected publication time, poll from T-30s through T+2m every 10 seconds. If the content hash changes, perform one confirmation fetch, then stop polling that window.
- Polymarket/orderbooks: monitor target-day markets until the Hong Kong day ends.
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
- Track unique HKO forecasts, latest since-midnight max, current forecast max by day, latest scraped bulletin high, discovered markets/outcomes, latest bid/ask, buys/sells placed, buys/sells missed, open positions, realized PnL, executable unrealized PnL, total profit, worst-case open loss, source freshness, decision counters, last scheduler run, and recent errors.
