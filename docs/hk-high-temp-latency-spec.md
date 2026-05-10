# HK Highest Temperature Latency Bot Spec

## 1. Scope

Build a local-first paper trading system for Hong Kong Polymarket highest-temperature markets.

The first production candidate should trade only after the paper system proves:

- HKO update detection is reliable.
- HKO values are parsed and timestamped correctly.
- Polymarket market/outcome mapping is correct for integer and threshold outcomes.
- latency opportunity logs are coherent enough to audit after resolution.
- Risk controls behave deterministically.

This spec intentionally excludes forecast-error modelling. Modelling becomes a second strategy layer after the latency foundation is working.

## 2. Market Definition

Initial market family:

- Geography: Hong Kong
- Metric: official highest/daily maximum temperature
- Resolution source: HKO official daily maximum temperature, as published in the climatological daily extract / daily maximum temperature dataset.
- Outcome types:
  - Integer bins, e.g. `27 C`, `28 C`, `29 C`.
  - Boundary buckets, e.g. `16 C or below`, `26 C or higher`.

Confirmed market semantics from sampled May 4 and May 5, 2026 HK highest-temperature markets:

- Resolution source is the HKO Daily Extract value `Absolute Daily Max (deg. C)` for the specified date once finalized.
- The source measures to one decimal place and that precision is used for resolution.
- The market cannot resolve to YES until the data for the date is finalized.
- Revisions after the market's data is finalized are not considered.
- Integer buckets use no rounding. `29 C` means `29.0 <= max < 30.0`; `29.9 C` is `29 C`, not `30 C`.
- `N C or higher` means `max >= N.0`.
- `N C or below` means `max < N+1.0` for the bottom boundary bucket, e.g. `16 C or below` covers all values below `17.0`.

Each market still needs a parsed settlement predicate:

- `EXACT_C`: pays if `N.0 <= official max < N+1.0`.
- `GTE_C`: pays if official max is greater than or equal to `N.0`, used for labels like `N C or higher`.
- `BOTTOM_BUCKET_LTE_C`: pays if official max is below the next listed exact bucket, e.g. `16 C or below` means `official max < 17.0`.
- `OTHER`: ignored until manually mapped.

Parser examples:

- May 4, 2026 ladder: `16 C or below`, `17 C`, `18 C`, ..., `25 C`, `26 C or higher`.
- May 5, 2026 ladder: `18 C or below`, `19 C`, `20 C`, ..., `27 C`, `28 C or higher`.

## 3. Data Sources

### HKO Primary Sources

Use direct HKO sources that publish quickly enough for latency trading.

- AWS GIS automatic weather station latest readings for the Hong Kong Observatory station, updated around every 5 minutes: `https://www.hko.gov.hk/wxinfo/awsgis/latestReadings_AWS1_v2.txt`
- Since-midnight max/min actuals for the Hong Kong Observatory automatic weather station, updated every 10 minutes and retained for observation/cross-checking: `https://data.weather.gov.hk/weatherAPI/hko_data/csdi/dataset/latest_since_midnight_maxmin_csdi_4.csv`
- AWS GIS HKO station forecast feed: `https://www.hko.gov.hk/wxinfo/awsgis/forecast/HKO.xml`
- OCF HKO station forecast page: `https://maps.weather.gov.hk/ocf/text_e.html?mode=0&station=HKO`
- OCF HKO station forecast data feed discovered behind that page and retained as forecast fallback: `https://maps.weather.gov.hk/ocf/dat/HKO.xml`
- Local weather forecast bulletin webpage, retained as an old fallback/parser fixture only: `https://www.weather.gov.hk/en/wxinfo/currwx/flw.htm`
- Current weather report, retained as observation/fallback evidence only: `https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=rhrread&lang=en`
- Warning summary: `https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=warnsum&lang=en`
- Warning details: `https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=warningInfo&lang=en`
- Special weather tips: `https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=swt&lang=en`
- Historical daily max actuals: HKO/data.gov.hk daily maximum temperature dataset, specifically the Hong Kong Observatory station.

Parsing rules:

- AWS GIS actuals: use the `HKO` row from `latestReadings_AWS1_v2.txt`. Store `TEMP` as decimal current temperature, `MAXTEMP` as decimal since-midnight max, and `MINTEMP` as decimal since-midnight min. For actual-cross trading, actual invalidation, and dashboard current actuals, AWS GIS latest readings are the source of truth when present.
- CSDI and `rhrread`: keep ingesting these feeds for observation and latency comparison only. Do not treat a new CSDI or `rhrread` value between AWS GIS updates as a trading signal while AWS GIS is the configured D+0 source of truth; it may be stale relative to AWS GIS.
- AWS GIS station forecast: fetch `forecast/HKO.xml` first and parse `DailyForecast[].ForecastDate`, `ForecastMaximumTemperature`, `ForecastMinimumTemperature`, and `HourlyWeatherForecast[]`. Despite the `.xml` suffix it is JSON. It carries decimal hourly forecast temperatures and can carry decimal daily max/min values. Trading decisions use the full available decimal hourly path first for each covered date, then daily decimal max/min if hourly rows are unavailable. Rounded/display max is not a trading authority.
- OCF station forecast fallback: if the AWS GIS station forecast fetch or parse fails, fall back to `https://maps.weather.gov.hk/ocf/dat/HKO.xml`, which has the same station forecast shape. Store both feeds through the same normalized forecast/sample tables so the runner and dashboard consume one station-forecast model.
- The Open Data API `flw` and `fnd` feeds are removed as trading inputs for this POC because historical evidence shows they can lag the actual bulletin/webpage update or have unclear update timing.
- The old local weather forecast bulletin and 9-day forecast are removed from the trading signal path for this POC.
- The OCF station feed provides multiple days, but the current paper latency strategy still focuses on the current-day HK highest-temperature market first.

Store every fetched HKO response as raw HTML/JSON/CSV plus normalized rows. The raw snapshot is part of the audit trail.
Raw snapshots are not deduplicated by content hash: unchanged payloads can still carry new HTTP response metadata. Persist response headers, HTTP `Date`, HTTP `Last-Modified`, and `ETag` with every fetch.

### Polymarket Sources

- Gamma API / market discovery: find active Hong Kong highest-temperature markets.
- CLOB public endpoints: order books, best bid/ask, midpoints, last trades where available.
- CLOB authenticated endpoints later: order placement/cancel/query.

Trading credentials are not required for paper trading. Live trading will require wallet private-key signing plus derived L2 API credentials. Secrets must be environment variables or local secret storage, never committed.

Discovery findings from May 3, 2026:

- Gamma event lookup by exact slug works for daily HK temperature ladders:
  - `https://gamma-api.polymarket.com/events?slug=highest-temperature-in-hong-kong-on-may-4-2026`
  - `https://gamma-api.polymarket.com/events?slug=highest-temperature-in-hong-kong-on-may-5-2026`
- Gamma market lookup by the event slug returns no rows; use the event endpoint and read the nested `markets` array.
- Each ladder outcome is represented as a separate binary CLOB market under one `negRisk` event.
- `groupItemTitle` contains the displayed outcome label, e.g. `25°C`, `26°C or higher`.
- `clobTokenIds` is a JSON-encoded two-element array ordered as YES token then NO token.
- `outcomes` is a JSON-encoded `["Yes", "No"]`.
- Useful market fields include `bestBid`, `bestAsk`, `orderPriceMinTickSize`, `orderMinSize`, `acceptingOrders`, `negRisk`, `negRiskMarketID`, and `gameStartTime`.
- CLOB orderbook lookup works via `https://clob.polymarket.com/book?token_id={token_id}`.
- CLOB orderbook responses include `bids`, `asks`, `tick_size`, `min_order_size`, `last_trade_price`, and `neg_risk`. Price/size fields are strings and must be parsed as decimals/floats.
- In the CLOB orderbook response, bids and asks may not be sorted in executable order. Sort bids descending and asks ascending before simulating fills.
- Python HTTP clients should send a normal User-Agent header. Gamma returned HTTP 403 to Python's default urllib user agent during discovery, while the same endpoint worked with curl and with `User-Agent: whenitrains/0.1`.

## 4. System Modes

### Paper Mode

Default and only mode for v1.

- Reads live HKO and Polymarket data.
- Computes latency signals and stale-price trade candidates.
- Simulates orders with explicit fill assumptions.
- Tracks simulated positions, PnL, exposure, and drawdown.
- Never places real orders.

### Live Mode

Guarded mode behind explicit config. The live scaffold now exists, but paper remains the default and the live scheduler must still fail closed unless all live gates pass.

Required safeguards before enabling:

- `WHENITRAINS_TRADING_MODE=live`
- explicit `--live` flag on live commands
- configured wallet/funder/signature type
- pre-derived CLOB API key/secret/passphrase
- hot private key loaded from macOS Keychain
- max position and loss limits
- order-size caps
- kill switch
- dry-run parity tests passing
- manually reviewed market parser for the target market family

## 5. Polling And Update Detection

HKO webhooks are preferred if a reliable official event feed exists, but assume polling for v1.

Polling strategy:

- HKO AWS GIS actuals:
  - source is `latestReadings_AWS1_v2.txt`; parse only the `HKO` station row for the trading signal
  - baseline observed-reading poll schedule is every 5 minutes, matching the reading timestamps reported in the payload header
  - learned update minutes are stored as `aws_gis_actual` in `hko_source_update_minutes`
  - regular observed-reading minutes such as `:00`, `:05`, `:10`, and `:30` are polled aggressively every 10 seconds from 30 seconds before through 30 seconds after the scheduled minute
  - learned fetchable/publish minutes from HTTP `Last-Modified`, for example a `19:38` file publish for a `19:30` reading, are expanded into the matching 10-minute publish pattern and polled every 10 seconds with a wider 2-minute buffer on each side
  - AWS actual polling runs in a dedicated worker with its own SQLite connections so market discovery, orderbook refreshes, and trading decisions cannot delay actual ingestion inside active windows
  - every newly observed AWS payload timestamp should be logged as an actual reading time; HTTP `Last-Modified` should also be logged as a learned fetchable/publish minute when it differs from the payload timestamp
  - if AWS GIS fetch or parsing fails, the scheduler may store an `rhrread` fallback row for observation under `rhrread_actual`, but it must still log `aws_actual fetch failed` and must not mark the AWS polling window complete
- HKO since-midnight max/min CSV:
  - source updates extremely regularly every 10 minutes, typically near `:00`, `:09`, `:19`, `:29`, `:38`, `:48`, and `:58`
  - poll only during the Hong Kong weather day from 10:00 to 20:00 HKT as an observation/cross-check source
  - polling window: from 1 minute before each expected publication time through 2 minutes after it
  - cadence inside the window: every 10 seconds
  - if content hash changes, perform one confirmation fetch, then stop polling that window
  - if no change by the end of the window, log a warning and wait for the next expected publication time
  - outside 10:00-20:00 HKT: do not poll
- HKO AWS GIS station forecast feed:
  - source is `https://www.hko.gov.hk/wxinfo/awsgis/forecast/HKO.xml`
  - despite the `.xml` extension, it returns JSON with `DailyForecast` and `HourlyWeatherForecast`
  - the hourly forecast rows cover the full available station forecast horizon, not only D+0 or the next 24 hours
  - the scheduler fetches this feed before the OCF station fallback URL
- HKO OCF station forecast fallback:
  - observed payload `LastModified` cadence is irregular but roughly hourly; recent stored gaps have a median near 60 minutes, with common gaps around 40, 60, and 80 minutes
  - the hourly forecast table is a 24-hour path republished with each OCF payload version, not a source fetched only once per forecast hour
  - current scheduler cadence: poll learned OCF update windows plus a coarse discovery probe so newly observed update minutes can be learned
  - separate discovery sampler: run `sample-ocf --interval-minutes 10 --hours 24` to persist raw snapshots, normalized max/min forecast rows, and hourly temperature forecast rows for cadence analysis
  - automatic cadence discovery: when payload `LastModified` or HTTP `Last-Modified` reveals a new HKT minute-of-day, store that minute in `hko_source_update_minutes`; the scheduler adds those learned minutes as daily forecast polling windows
- Polymarket markets/orderbooks: monitor active target-day markets until the Hong Kong day ends.
- Future market discovery: after OCF forecast ingestion, discover HK highest-temperature markets for every OCF forecast date at or after the current HKT date. Fetch orderbooks for all discovered active HK high-temperature outcomes.
- Orderbook refresh must fetch independent Polymarket CLOB token books concurrently, with a bounded worker count, because YES/NO books across outcomes are independent HTTP reads. Persist snapshots through the scheduler's main SQLite connection after fetch completion so SQLite writes remain single-threaded and deterministic.
- Forecast-change trading can operate on future target-date markets when the OCF forecast high for that date changes and the corresponding market price is stale.
- AWS GIS actual-cross trading remains current-day only. Actual-temperature invalidation and hold-to-maturity checks must only apply to positions whose market target date equals the current HKT date.
- Resolution watcher: after the target day ends, check Polymarket once per day for final resolution.
- Final Daily Extract: since resolution uses finalized data only, final settlement audit is separate from since-midnight trading signals.
- Resolution-rule guard: every discovered HK highest-temperature event must include the expected HKO Daily Extract resolution wording. The first sentence date may vary, but the remainder must match the expected `Absolute Daily Max (deg. C)`, finalized Daily Extract, one-decimal precision, and no-post-finalization-revisions language. If the normalized text is missing or changed, print a critical terminal warning and persist a `risk_events` row before any trading decisions rely on that market.

Every HKO snapshot should produce a content hash. If the hash changes, the event bus emits `HKO_UPDATE_DETECTED`. OCF forecast event-time precedence is payload `LastModified`, then HTTP `Last-Modified`, then local fetch time. AWS GIS actual event-time precedence for trading is the payload header `Latest readings recorded at ... Hong Kong Time`, then local fetch time. AWS GIS scheduler learning should also use HTTP `Last-Modified` and first-seen timing as publish/fetchability evidence, because the reading timestamp can precede public availability by several minutes.

Rate-limit and failure backoff:

- On HTTP 429, timeout, DNS/network failure, or repeated non-2xx responses, immediately slow that source to a 10-second cadence.
- If failures continue, slow that source to a 60-second cadence.
- Emit a terminal warning when backoff starts, escalates, or clears.
- Freeze new entries if source freshness exceeds configured safety limits.
- Clear backoff after a successful fetch plus one additional successful confirmation fetch.

Required latency metrics:

- `fetched_at_utc`
- `hko_update_time` if present in payload
- `detected_delay_seconds`
- `parse_completed_at_utc`
- `signal_completed_at_utc`
- `market_snapshot_at_utc`

Scheduler safeguards:

- Use a single-process lock per database so two schedulers cannot double-trade the same signals.
- Dedupe HKO events by source, target date, old value, new value, and HKO update/detection time.
- Upsert/dedupe Polymarket events/outcomes.
- Treat unchanged HKO payload hashes as already seen and do not emit a new event.
- Default scheduler mode is paper trading; live mode must fail closed until separately enabled.
- On restart, load existing paper positions and continue from SQLite state.

## 6. Local Data Store

Use SQLite locally for v1. Keep the schema deployable to Postgres later.

Core tables:

- `raw_snapshots`
  - `id`
  - `source`
  - `endpoint`
  - `fetched_at_utc`
  - `content_hash`
  - `payload`

- `hko_forecasts`
  - `id`
  - `snapshot_id`
  - `source_type`
  - `forecast_date_hkt`
  - `forecast_max_c`
  - `weather_text`
  - `wind_text`
  - `psr`
  - `raw_forecast`

- `hko_current_observations`
  - `id`
  - `snapshot_id`
  - `observed_at_hkt`
  - `station`
  - `temperature_c`
  - `since_midnight_min_c`
  - `since_midnight_max_c`
  - `humidity_pct`
  - `rainfall_mm`
  - `raw_observation`

  `station='HKO'` identifies AWS GIS rows. `station='HK Observatory'` identifies CSDI since-midnight rows. `station='Hong Kong Observatory'` identifies `rhrread` current-weather rows. Dashboard and runner logic must not infer source type from numeric shape alone; use station/source evidence and update-minute labels.

- `hko_daily_actuals`
  - `date_hkt`
  - `station`
  - `max_temperature_c`
  - `source_snapshot_id`
  - `loaded_at_utc`

- `markets`
  - `id`
  - `polymarket_market_id`
  - `slug`
  - `question`
  - `target_date_hkt`
  - `status`
  - `resolution_source_text`
  - `raw_market`

- `outcomes`
  - `id`
  - `market_id`
  - `token_id`
  - `label`
  - `predicate_type`
  - `predicate_value_c`
  - `raw_outcome`

- `orderbook_snapshots`
  - `id`
  - `outcome_id`
  - `fetched_at_utc`
  - `best_bid`
  - `best_ask`
  - `mid`
  - `depth_json`

- `signals`
  - `id`
  - `created_at_utc`
  - `market_id`
  - `trigger_type`
  - `current_max_c`
  - `forecast_max_c`
  - `affected_outcomes_json`
  - `directional_impacts_json`
  - `pre_event_prices_json`
  - `post_event_prices_json`
  - `price_response_json`
  - `notes`

- `paper_orders`
  - `id`
  - `created_at_utc`
  - `signal_id`
  - `outcome_id`
  - `side`
  - `limit_price`
  - `size_usd`
  - `simulated_fill_price`
  - `simulated_fill_size_usd`
  - `status`
  - `reason`

- `paper_positions`
  - `outcome_id`
  - `net_shares`
  - `avg_price`
  - `realized_pnl`
  - `updated_at_utc`

- `risk_events`
  - `id`
  - `created_at_utc`
  - `event_type`
  - `severity`
  - `details_json`

## 7. Latency Signal Engine

This is not a probability model. It is an event detection and stale-price engine.

The core thesis is:

1. HKO publishes a forecast/current-observation update.
2. For each market outcome, classify whether the update should increase, decrease, or not materially change the outcome's chance of resolving YES.
3. Compare that directional impact with the outcome price change from the last pre-update market snapshot.
4. The bot buys the stale side.
5. Current scheduler-managed forecast trades are held until a later forecast change or same-day actual max update invalidates them; manual paper exit commands still support take-profit and max-hold checks.
6. For market-settling observation events, such as a temperature threshold already being reached, the bot may hold to maturity if resolution risk is low and the predicate mapping is confirmed.

Inputs:

- Latest current-day HKO forecast max from the OCF HKO station forecast feed.
- Latest current temperature observations.
- Current observed max since midnight if available from HKO/API page.
- Time of day in HKT.
- Remaining daylight/heat window.
- Forecast weather text.
- Warning state.
- Market predicates.
- Pre-update Polymarket price/orderbook snapshot.
- Post-update Polymarket price/orderbook snapshot.
- Recent movement in affected and neighboring outcomes.

Required output:

- Affected outcomes.
- Directional impact per outcome:
  - `INCREASES_YES_PROBABILITY`
  - `DECREASES_YES_PROBABILITY`
  - `NO_MATERIAL_IMPACT`
- Price-change status since the previous HKO reading:
  - `PRICE_MOVED_WITH_EVENT`
  - `PRICE_NOT_MOVED_WITH_EVENT`
- Directional action candidates: buy YES, buy NO, exit YES, exit NO, hold to maturity.
- Stale-price score based on price movement lag, spread, and executable depth.
- Explanation fields:
  - `hko_event_type`
  - `invalidated_outcomes`
  - `forecast_shift`
  - `current_temp_pressure`
  - `directional_impact`
  - `price_change_since_prior_reading`
  - `price_staleness`
  - `entry_price`
  - `target_exit_condition`
  - `hold_to_maturity_reason`
  - `data_freshness`

Initial deterministic rules should be deliberately simple and auditable:

- If the decimal effective OCF max crosses into a different integer market bucket, mark outcomes whose target values are near the old reading or new reading. Decimal moves inside the same bucket are recorded but do not create forecast-change entries.
- Forecast-change selection is directional and narrow:
  - forecast down: buy the new forecast bucket YES and buy NO on exact/GTE values above the new forecast;
  - forecast up: buy the new forecast bucket YES and buy NO on exact/bottom values below the new forecast.
- Exclude far-away long shots whose likelihood only changes trivially. Example: if HKO raises target-day forecast max from `28 C` to `29 C`, do not buy `35 C`.
- For every affected outcome, classify the update's directional impact before looking at the post-update price.
- If current/official observed max has already exceeded an exact outcome, mark existing positions in that outcome for immediate exit.
- If AWS GIS actual max crosses a greater-than/less-than target value on the same target date and the crossed bucket is not above the active same-day forecast signal, mark that outcome's YES side as repricing-critical. If the forecast signal is already higher than the crossed bucket, the cross should not buy that bucket's YES, because the forecast had already made the higher bucket relevant. Example: actual crosses into `29 C` while the active forecast peak is already `30 C`; do not buy `29 C YES` on the cross, but a `28 C NO` trade is a market-invalidation candidate.
- If the update increases YES likelihood and YES has not moved up materially since the prior HKO reading, create a buy-YES candidate.
- If an actual-cross invalidates a bucket, buy the now-settled side up to `0.99`; do not require stale-price movement lag for this invalidation case.
- Treat unchanged prices, prices that moved too little, and prices that moved against the event as `PRICE_NOT_MOVED_WITH_EVENT`.
- Forecast-change entries require the event-implied YES ask move to be no more than `0.20`; entry ask must be no more than `0.70` for D+0/D+1 and no more than `0.20` for D+2 or later.
- Generic entry protection rejects asks above `0.98`, caps the marketable limit to the latest best ask plus `0.05`, and requires at least `$25` of fill unless the requested size is smaller.
- Forecast-value entries can run even without a forecast change: for today and next-day markets, buy the forecast bucket YES when the YES ask is at or below `0.30` and the market favorite is below the forecast bucket. D+2 cheap forecast entries require a stricter YES ask of `0.20` or less and the market must not already have moved with the signal. Skip if the favorite is above a non-top forecast bucket, if the latest hourly forecast never reaches the bucket floor, or if the first hourly forecast breach of the bucket floor occurs from `21:00` through `23:00` HKT.
- Forecast-value buys only consume ask depth at or below `0.30` and may add to the same token until that token's position budget is reached.
- Actual-cross new-bucket YES entries use a relaxed stale-price movement guard of `0.10` and an entry cap of `0.70`. High-market YES entries can raise the entry cap to `0.75` if the actual cross is above the preceding hourly forecast peak, the cross occurs during that preceding forecast's peak-temperature hour, and every later same-day hour in the newest hourly forecast is below the actual cross value. This split matters because a post-cross forecast may update the current hour to match the actual reading; the preceding forecast is the basis for identifying the surprise, while the newest forecast confirms the remainder of the day is lower. This peak-hour sure-bet rule applies only to high-temperature markets. The inverse rule is intentionally not applied to minimum-temperature markets because overnight lows are not governed by a single comparable solar-heating mechanism.
- The peak-hour rule still requires the actual cross to be consistent with the highest active forecast value for the day. If actual crosses `29 C` at noon while a `30 C` peak at 14:00 was already registered, do not buy `29 C YES` solely from the cross.
- Exact-bucket actual-cross fast lane buys the crossed exact bucket's YES side and any now-invalidated NO side when the official actual max enters that bucket, the preceding/newest hourly forecast split agrees with the peak-hour sure-bet rule, and executable ask depth is available at or below `0.75` for YES. The exact-bucket YES fast lane bypasses the stale-price movement guard because forecast-timing confirmation is the primary safety guard. Invalidated NO entries continue to bypass the stale-price movement guard and use the invalidated-bucket cap.
- If the event is market-settling and the held token's predicate is already satisfied, allow hold-to-maturity instead of forcing take-profit exit.
- If spread or depth makes the apparent stale price non-executable, log a missed opportunity rather than a trade.
- Forecast invalidation exits use the decimal active forecast signal from the station forecast feed. AWS GIS `forecast/HKO.xml` has priority for every date/hour it covers; OCF is only a fallback. Sell YES when the decimal max no longer matches that bucket, sell NO when the decimal max now matches that bucket, and sell YES when the hourly path only first breaches the bucket floor from `21:00` through `23:00` HKT. If no decimal signal is available or the latest OCF sample for the target date is stale, skip the trading decision rather than falling back to the rounded/display daily max.
- Actual invalidation exits are scoped to the same market target date; previous-day actual max values cannot invalidate future/current-day positions.

## 8. Backtesting Harness

The reusable backtest harness replays stored HKO forecasts, OCF hourly samples, current observations, and Polymarket orderbook snapshots into a scratch SQLite DB.

CLI:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 backtest-day YYYY-MM-DD
```

Backtest behavior:

- Copies the source DB using SQLite's online backup API, then writes only to the replay DB.
- Default replay DB path: `/private/tmp/whenitrains-backtest-YYYY-MM-DD.sqlite3`.
- Clears paper/replay tables in the scratch DB only: HKO forecast/sample rows, observations, orderbooks, paper decisions, paper orders, positions, signals, and paper order exclusions.
- Re-ingests source rows as-of each replay tick, then runs the same paper tick code path as the scheduler.
- Stamps generated paper decisions/orders/signals back to the historical tick timestamp so dashboard/PnL inspection is chronological.
- Builds replay-local indexes for orderbooks, forecasts, observations, decision event keys, and positions.
- Outputs orders, positions, active ticks, and optional JSON.

Tick sources:

- `--tick-source scheduler`: use historical paper decision timestamps. This is the default and best for replaying what the scheduler would have evaluated.
- `--tick-source data`: use stored data fetch timestamps from HKO forecasts, OCF samples, observations, and optionally orderbooks.
- `--tick-source both`: combine scheduler and data timestamps.
- `--include-orderbook-ticks`: include orderbook snapshot timestamps for data-driven replays.
- `--max-ticks`: cap replay length for smoke tests.
- `--json`: emit machine-readable output for analysis.

Experimental backtesting:

- `experiment-backtest-day YYYY-MM-DD` runs an isolated strategy harness against the same historical source rows.
- Experimental strategies write only to `experiment_runs`, `experiment_decisions`, `experiment_orders`, `experiment_positions`, and `experiment_metrics` in the replay DB.
- The experimental harness must not mutate `paper_orders`, `paper_positions`, `paper_decisions`, or `signals`.
- Use this path for future policy variants and PnL comparisons before moving logic into the production paper scheduler.

## 9. Trade Candidate And Repricing Logic

No probability or fair-value estimate is required for v1.

For an entry candidate, compare the directional impact of the HKO event to the executable price change since the previous HKO reading:

- `event_relevance`: how directly the HKO update affects the outcome.
- `directional_impact`: whether the event increases, decreases, or does not materially change YES likelihood.
- `prior_price`: executable bid/ask immediately before the new HKO information.
- `current_price`: executable bid/ask after the new HKO information.
- `price_response`: whether price moved with the event enough to erase the latency opportunity.
- `price_lag`: affected outcome has not moved enough in the direction implied by the event.
- `spread_ok`: spread is tight enough to enter and later exit, unless the trade is explicitly hold-to-maturity after a settling observation.
- `depth_ok`: executable depth is economically meaningful; for the POC, take any visible depth whose expected gross edge is larger than expected transaction fees.
- `time_since_event`: still inside the stale-price window.

Stale-price window definition:

- The stale-price window starts when the bot detects and persists a new HKO event that materially affects one or more market outcomes.
- The event timestamp is the earliest trusted timestamp available in this order: HKO payload `updateTime`, HKO observation time, then local `fetched_at_utc`.
- The bot snapshots relevant Polymarket prices immediately before or at detection if available, then repeatedly checks current executable bid/ask during the window.
- During the window, an entry is valid only if the relevant YES/NO executable price has not moved in the event-implied direction by the configured minimum move.
- If the price has already moved with the event before the bot can enter, log a missed buy with reason `price_already_moved`.
- If no executable depth exists, log a missed buy with reason `no_executable_depth`.
- If visible depth is not economically meaningful after expected transaction fees, log a missed buy with reason `below_fee_threshold`.
- If the window expires without a fill, log a missed buy with reason `stale_window_expired`.
- The window is for opening a new latency trade only. Once a scheduler-managed paper position is opened, exit is governed by forecast invalidation, same-date actual invalidation, hold-to-maturity, or risk rules. Manual paper commands still support take-profit and max-hold checks.
- Initial POC default: 90 seconds after event detection, configurable.

Directional impact examples:

- Forecast high moves from `28 C` to `29 C`: increases YES likelihood for exact `29 C` and `>=29 C`; decreases YES likelihood for exact `28 C` and `<=28 C`.
- Forecast high moves from `30 C` to `29 C`: decreases YES likelihood for exact `30 C` and `>=30 C`; increases YES likelihood for exact `29 C` and nearby lower-value outcomes.
- Current observed max reaches `29.0 C`: increases YES likelihood for `>=29 C` to effectively settled; decreases YES likelihood for exact outcomes below `29 C`.
- Current observed max exceeds `29.0 C`: decreases YES likelihood for exact `29 C` if the market settles on exact integer max and the official value is already beyond it.

Affected-outcome selection:

- Primary targets are outcomes whose target values are near the new forecast/current reading.
- Exact and greater-than/less-than predicates are both valid if their target value is close enough to the new information.
- Ignore far-away outcomes unless the HKO event directly changes their settlement state.
- A forecast move from `28 C` to `29 C` can target exact `29 C`, `>=29 C`, exact `28 C`, `<=28 C`, and immediately nearby values. It should not target `35 C`.
- An observed max crossing `29.0 C` can target `>=29 C` and exact buckets below `29 C` because the event is market-settling or invalidating.

Example entry:

- HKO raises forecast high from `28 C` to `29 C`.
- `29 C` YES or `>=29 C` YES is still offered near its pre-update ask.
- The bot paper-buys YES.
- The scheduler holds the position until a later forecast change, same-date actual max update, hold-to-maturity condition, or risk rule resolves the trade.

Example NO entry:

- HKO lowers forecast high from `30 C` to `29 C`.
- `30 C` YES remains overpriced or `30 C` NO remains underpriced.
- The bot paper-buys NO where market structure supports it.

Use ask for buys and bid for sells. Mid prices are only for reporting.

Trade candidate requirements:

- Relevant HKO event detected.
- Directional impact has been classified for the outcome.
- Outcome target value is close enough to the new HKO information to be materially affected.
- Price movement lag >= configured stale-price threshold.
- Current data freshness within allowed limit.
- Market parser confidence is high.
- Outcome liquidity is sufficient for a fee-positive simulated fill; otherwise log a missed trade.
- Risk caps pass.

Default stale-price thresholds:

- Entry: affected outcome has moved less than 1-2 cents after a material HKO event while neighboring/related outcomes or market context imply repricing should occur.
- Forecast-change repricing guard: directional ask movement must be no more than `0.20`; entry ask must be no more than `0.70` for D+0/D+1 and no more than `0.20` for D+2 or later.
- Forecast-value cheap-bucket guard: current or next-day forecast bucket YES ask must be no more than `0.30`.
- Actual-cross new-bucket YES guard: ask movement must be less than `0.10`, entry ask must be no more than `0.70`, except the high-market peak-hour sure-bet path may enter up to `0.80`.
- Actual-cross invalidated-bucket guard: buy the now-settled side up to `0.99`; this does not require stale-price movement lag.
- OCF forecast freshness guard: forecast-value entries, forecast-change entries, and forecast-based exits require the latest OCF sample for the target date to be less than `90` minutes old.
- Manual paper commands still expose take-profit and max-hold calculations, but scheduler-managed positions no longer auto-exit on take-profit or 10-minute timeout.
- Hold to maturity: allowed when an official/current HKO observation has already satisfied the market predicate, the parser is high-confidence, and remaining resolution risk is mainly operational rather than meteorological.
- Live thresholds should start more conservative until paper data proves fill behavior.

Missed trade definitions:

- `buy_missed`: signal generated, but no executable depth, expected edge below transaction fee, spread/depth guard failed, risk cap rejected, duplicate signal rejected, stale data guard fired, or price already moved with the event.
- `sell_missed`: exit condition met, but no executable bid/depth, expected proceeds below transaction fee, stale orderbook, or risk/safety guard blocked execution.

## 10. Trading Scenarios

### Scenario A: Forecast Upgrade Mispricing

HKO raises target-day forecast max from `28 C` to `29 C`.

Expected behavior:

- Fetch updated HKO forecast.
- Detect changed forecast max.
- Identify affected outcomes near the old/new readings, including both exact and greater-than/less-than predicates.
- Do not target far-out long shots like `35 C`.
- Compare against order book.
- Paper-buy stale upgraded outcomes if their executable prices have not moved.
- Paper-exit positions invalidated by the upgrade.
- Paper-exit after repricing target, timeout, or invalidation.

### Scenario B: Forecast Downgrade Mispricing

HKO lowers forecast max from `30 C` to `29 C`.

Expected behavior:

- Identify nearby high exact bins and greater-than/less-than outcomes affected by the downgrade.
- Buy stale NO exposure on downgraded outcomes, or buy lower affected outcomes if their prices lag.
- Exit high-bin longs if the downgrade invalidates the original latency thesis.

### Scenario C: Actual Temperature Invalidates Existing Bet

The bot holds `max exactly 28 C`, and current/official intraday max reaches `29.0 C`.

Expected behavior:

- Mark `exact 28 C` as effectively dead, subject to exact market wording and source verification.
- Immediately attempt paper-exit using best bid if nonzero.
- Do not average down.
- Log a risk event explaining invalidation.
- Reallocate only if a fresh HKO event creates a separate stale-price opportunity and risk budget remains.

### Scenario D: Greater-Than/Less-Than Outcome Becomes Certain Or Near-Certain

Market includes `>=29 C`, and observed max reaches `29.0 C`.

Expected behavior:

- Reprice the affected greater-than/less-than outcome to near one.
- If market ask is stale after the target value has already been crossed, buy YES.
- If already long, either hold to maturity or sell only if the bid is high enough to justify removing settlement/operational risk.
- Stop buying once the stale price disappears or risk caps bind.

### Scenario E: Late-Day High Outcome Still Overpriced

At 16:30 HKT, observed max is `28.1 C`, forecast high was `30 C`, and weather has turned cloudy/rainy.

Expected behavior:

- Cap remaining upside.
- Sell/exit high-outcome longs in paper mode if bid remains attractive.
- Consider buying lower exact or greater-than/less-than outcomes if their target values are near the new information and the market has not adjusted.

### Scenario F: Market Creation / Early Liquidity

A new target-date market appears with thin liquidity and stale/naive prices.

Expected behavior:

- Parse market and outcomes.
- Do not trade until parser confidence and source-of-truth wording are verified.
- Once verified, seed paper orders only where a fresh HKO event and stale executable price are present.
- Use smaller size caps in first hour after market creation.

### Scenario G: Conflicting HKO Signals

Current-day webpage forecast says `29 C`, but current observations and weather warnings imply suppressed heating.

Expected behavior:

- Do not blindly trade the forecast point.
- Use deterministic uncertainty flags:
  - showers/thunderstorms
  - strong wind
  - warning changes
  - late-day observed max below trajectory
- For v1, conflicting signals should usually block new latency entries unless the market has clearly failed to reprice a concrete HKO update.

### Scenario H: Data Staleness Or Source Failure

HKO API fails, payload schema changes, or latest snapshot is too old.

Expected behavior:

- Freeze new trading.
- Continue risk-reducing exits only if Polymarket data is fresh and the invalidation condition is already known from prior trusted data.
- Emit risk event.

### Scenario I: Polymarket Liquidity Shock

Order book disappears, spreads widen, or best bid/ask jumps after HKO update.

Expected behavior:

- Recompute stale-price status using executable prices only.
- Do not simulate fills through unrealistic depth.
- Flag missed opportunity separately from traded opportunity.

### Scenario J: Resolution Day Closeout

Near final resolution, positions may be worth exiting even at small discount if uncertainty remains around official daily extract publication timing.

Expected behavior:

- Mark positions by latest executable bid/ask.
- Exit forecast-latency positions when the thesis has played out, when invalidated, or when closeout rules require reducing settlement-timing risk.
- Hold market-settling positions to maturity when the predicate is already satisfied and the expected gain from waiting is worth the remaining operational/resolution risk.
- Stop opening new positions after configured cutoff unless the outcome is already determined by observed max.

## 11. Risk Controls

Bankroll assumption:

- Starting bankroll: USD 5,000.

Hard limits:

- Max bet size: 5% of bankroll = USD 250 per single order/position increase.
- Max total exposure per market: proposed USD 750 until paper data justifies more.
- Max total exposure across same target date: proposed USD 1,000.
- Max daily drawdown for paper testing: 80% of bankroll = USD 4,000. Freeze new trades after this.
- Live trading must use a safer drawdown limit before enablement; the 80% limit is only for paper-mode stress testing.
- Max stale-data age for new trades: proposed 90 seconds during aggressive windows, 5 minutes otherwise.
- No live trading if market parser confidence is below threshold.
- No live averaging down after hard invalidation.

Soft controls:

- Reduce size by 50% when spread is wide.
- Reduce size by 50% during HKO warning/conflict states.
- Require a larger stale-price gap for newly created markets.
- Require a larger stale-price gap for thin order books.

Kill switches:

- Manual local kill switch file, e.g. `.kill-switch`.
- Automatic kill switch on repeated API errors.
- Automatic kill switch on unexpected schema parse failure.
- Automatic kill switch on daily drawdown breach.

## 12. Execution Design

Paper order simulation should support:

- marketable limit buy at ask
- marketable limit sell at bid
- fill through depth up to requested size
- partial fills
- rejected orders when depth is insufficient
- explicit slippage assumptions
- max-price and slippage-cap enforcement
- minimum-fill enforcement
- per-token position-budget enforcement for add-on buys

Paper trading realism limits:

- Paper fills use the visible CLOB book at the time of the snapshot.
- The simulator assumes our order can immediately take displayed liquidity at or within the calculated limit price.
- Real fills can differ due to queue priority, latency, hidden/changed liquidity, partial fills, and our own order moving the market.
- For larger paper orders, the simulator already walks visible depth, so it estimates direct market impact from the displayed book.
- It cannot estimate second-order impact: other traders reacting to our order, makers cancelling after seeing flow, or price movement between snapshot and order arrival.
- Paper PnL should be treated as an upper-bound or scenario estimate, not audited real PnL.
- Live readiness should compare paper fills to small live pilot fills before scaling size.

Live execution later should support:

- limit orders only by default
- immediate-or-cancel style behavior where possible
- cancel stale orders after short TTL
- no market orders unless explicitly enabled
- account/open-order reconciliation every loop
- separate `risk_check_before_order` and `risk_check_after_fill`

## 13. API Credentials Plan

Paper mode:

- No private key needed.
- Read-only Polymarket endpoints only.

Live mode:

- Use the official Polymarket Python CLOB client.
- Use a dedicated bot hot key stored in macOS Keychain, not SQLite or `.env`.
- Require pre-derived L2 credentials at runtime. Normal startup must not create or derive API credentials.
- Required env vars:
  - `WHENITRAINS_TRADING_MODE=live`
  - `POLYMARKET_FUNDER_ADDRESS`
  - `POLYMARKET_SIGNATURE_TYPE`
  - `POLYMARKET_API_KEY`
  - `POLYMARKET_API_SECRET`
  - `POLYMARKET_API_PASSPHRASE`
- Optional env vars:
  - `POLYMARKET_HOST`
  - `POLYMARKET_CHAIN_ID`
  - `WHENITRAINS_KEYCHAIN_SERVICE`
  - `WHENITRAINS_KEYCHAIN_ACCOUNT`
- Default Keychain service/account: `whenitrains-polymarket` / `bot-private-key`.
- Use `POLYMARKET_SIGNATURE_TYPE=3` for the dedicated Polymarket proxy-wallet path.

Never log secrets. Never store private keys in SQLite.

Large-wallet security requirements:

- Keep paper trading and read-only monitoring usable without any private key present.
- Load trading secrets only in live mode and fail closed if `WHENITRAINS_TRADING_MODE` is not explicitly `live`.
- Prefer a dedicated machine or hardened VPS user for live trading; do not run unrelated services in the same environment.
- Store secrets in an OS keychain, encrypted secret file, or managed secret store rather than plain `.env` once real funds are used.
- Do not print signed payloads, auth headers, API secrets, private keys, or raw environment dumps.
- Add a startup warning that displays the funder address, signature type, and configured exposure caps, then requires an explicit live-mode confirmation flag.
- Separate withdrawal/admin actions from trading code. The bot should place/cancel trades only; it should not implement withdrawals.
- Use limit orders only by default, with max order size, max market exposure, max target-date exposure, and max open-order count enforced before every order.
- Reconcile balances, positions, and open orders every loop; if reconciliation fails, cancel stale orders if possible and freeze new entries.
- Add an emergency kill switch that cancels open orders and disables new entries.
- Add external alerts for live-mode start, every order, every fill, rejected risk check, drawdown breach, stale data, and auth failure.
- Rotate or re-derive API credentials after any suspected exposure.
- Maintain an incident runbook covering key compromise, bad parser behavior, runaway order placement, CLOB outage, and HKO data failure.
- Before scaling size, add a second human-controlled withdrawal path or custody plan outside the bot process.

## 14. Deployment Shape

Local v1:

- Python service
- SQLite database
- CLI commands
- `.env` for local configuration
- logs to local files

VPS-ready later:

- same Python service
- systemd or Docker
- SQLite initially, Postgres optional
- process healthcheck
- log rotation
- alert channel

## 15. Alerts And Dashboard

Alerting is terminal/log-only for the POC.

Alert severity:

- `info`: HKO update ingested, market discovered, scheduler heartbeat.
- `trade`: paper buy/sell placed or missed.
- `warning`: parser miss, stale source data, no orderbook, duplicate scheduler lock attempt.
- `critical`: drawdown breach, auth failure, live kill switch, schema failure.

Alerting requirements:

- Alerts must be persisted in SQLite and visible in the dashboard.
- Repeated identical warnings should be throttled to avoid terminal spam.
- External alert channels are deferred until after the POC.

Dashboard commands:

- `dashboard`: terminal paper summary backed by SQLite.
- `dashboard-serve`: local HTTP web UI, defaulting to `http://127.0.0.1:8765/`.

Paper web dashboard:

- Route: `/`.
- APIs: `/api/stats`, `/api/forecast-panels?side=YES|NO`, `/api/pnl`, and legacy `/api/forecast-vs-actual`.
- Auto-refreshes every 15 seconds.
- Shows D+0, D+1, and D+2 forecast panels.
- All dashboard times are HKT and should render as `YYYY-MM-DD HH:MM:SS`.
- Forecast-panel x-axes are scoped to the selected HKT date and start at local midnight. They must not drift across midnight into prior-day ticks.
- D+0 shows the precise decimal bot signal used for trading, not rounded display values. It includes the active forecast/actual signal, hourly forecast where relevant, hourly actual temperature, hourly actual-minus-forecast hover values, since-midnight max/min, and current HKO temperature.
- D+1/D+2 show OCF forecast high and selected Polymarket token price series.
- Token-side selector switches between YES and NO token charts.
- Price series include trade markers for filled paper buys and sells.
- Bot signal updates are rendered as always-visible bubbles so update times are visible and hoverable. Hover text includes the signal type, decimal value, HKT timestamp, and nearby buy/sell details.
- Legend items can be toggled on/off.
- Tooltips are delayed to reduce accidental hover noise and include nearby trade details, relevant signal value, and signal timestamp.
- Modifier-wheel zoom and touch/pinch chart scaling are supported.
- Paper PnL chart replays filled paper orders against later executable bids to show realized, unrealized, and total estimates.
- Open positions, realized PnL, and unrealized PnL summary tiles are clickable. Clicking a tile replaces the chart area with the relevant table of paper buys/sells/positions so trading activity can be copied and audited quickly.
- Realized PnL tables show realized sell/close events, not buy events. Unrealized PnL uses current executable bids for still-open positions; realized PnL is proceeds minus average entry cost and should not change unless a closing trade or correction changes the realized ledger.

Live web dashboard:

- Route: `/live`.
- API: `/api/live/stats`.
- Shows live open positions, confirmed open exposure, realized PnL, live order counts by status, kill-switch settings, and recent live orders.
- Refreshes every 5 seconds.
- Reads only live tables and live settings; it must not mix paper positions with live positions.

## 16. Milestones

### Milestone 1: Project Skeleton

- Python package setup.
- Config loading.
- SQLite migrations.
- Structured logging.
- CLI entrypoint.

### Milestone 2: HKO Ingestion

- Fetch and store raw HKO forecast/current/warning snapshots.
- Normalize AWS GIS/OCF station forecast rows.
- Normalize current observations.
- Load daily max actuals for the HKO station.
- Detect HKO update changes by content hash.

### Milestone 3: Polymarket Ingestion

- Discover HK highest-temperature markets.
- Store markets/outcomes.
- Parse integer and threshold predicates.
- Fetch and store order books.

### Milestone 4: Latency Signal Engine

- HKO event classification.
- Affected-outcome mapping.
- Stale-price detection against executable bid/ask.
- Entry/exit trigger generation.
- Scenario-specific signal labels.
- Full audit trail.
- Current-day-only paper runner.
- Polling-window scheduler for HKO actuals and OCF forecast updates.
- Forecast-change paper entries.
- Market-settling actual-cross entries for `or higher` outcomes.
- Forecast and same-date actual invalidation exits.
- Manual take-profit and max-hold exit calculations remain available for operator-triggered paper commands.

### Milestone 5: Paper Trader

- Simulated fills.
- Position accounting.
- Mark-to-market PnL.
- Risk caps.
- Terminal dashboard with realized PnL, executable unrealized PnL, total profit estimate, worst-case open loss, and missed-trade counters.

### Milestone 6: Review Gate For Live Trading

- Paper-trading performance report.
- Parser audit.
- Risk-control test suite.
- Keychain hot-key setup.
- Pre-derived CLOB API credential setup.
- Live mode remains fail-closed until explicitly enabled by command flags, env gate, credentials, preflight, risk caps, and kill-switch state.

## 17. Open Questions

- HKO since-midnight max/min endpoint is confirmed: `https://data.weather.gov.hk/weatherAPI/hko_data/csdi/dataset/latest_since_midnight_maxmin_csdi_4.csv`.
- HKO AWS GIS station forecast feed is confirmed as the primary forecast source: `https://www.hko.gov.hk/wxinfo/awsgis/forecast/HKO.xml`; parse displayed max/min forecast rows from `DailyForecast` and retain raw hourly forecast rows from `HourlyWeatherForecast`. The older OCF URL `https://maps.weather.gov.hk/ocf/dat/HKO.xml` remains a same-shape fallback.
- Polymarket market semantics are confirmed from sampled May 4 and May 5, 2026 HK highest-temperature markets: final HKO Daily Extract `Absolute Daily Max (deg. C)`, one-decimal precision, no rounding for integer buckets, finalized data only, and no later revisions considered.
- Paper-mode daily drawdown is intentionally set to USD 4,000 / 80% for stress testing. Live-mode drawdown needs a safer value before enablement.
- Wallet plan for live mode: use a dedicated bot hot key and Polymarket proxy wallet funded only with the current live risk budget plus a small operational buffer. Real-auth smoke must confirm the account-specific signature type and funder value before any real order.

## 18. References

- HKO Open Data API documentation: https://data.weather.gov.hk/weatherAPI/doc/HKO_Open_Data_API_Documentation.pdf
- HKO Weather API endpoint: https://data.weather.gov.hk/weatherAPI/opendata/weather.php
- HKO daily maximum/mean/minimum dataset: https://data.gov.hk/en-data/dataset/hk-hko-rss-daily-temperature-info-hko
- HKO climatological information page: https://www.weather.gov.hk/en/cis/climat.htm
- Polymarket authentication documentation: https://docs.polymarket.com/api-reference/authentication
- Polymarket trading overview: https://docs.polymarket.com/trading/overview
- Polymarket CLOB documentation: https://docs.polymarket.com/developers/CLOB/trades/trades-data-api
- Polymarket Python CLOB client: https://github.com/Polymarket/py-clob-client
