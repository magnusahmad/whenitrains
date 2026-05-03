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

Use HKO APIs first.

- Since-midnight max/min actuals for the Hong Kong Observatory automatic weather station: `https://data.weather.gov.hk/weatherAPI/hko_data/csdi/dataset/latest_since_midnight_maxmin_csdi_4.csv`
- 9-day forecast, updated at noon and midnight HKT: `https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=fnd&lang=en`
- General local forecast summary, updated hourly: `https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=flw&lang=en`
- Current weather report: `https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=rhrread&lang=en`
- Warning summary: `https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=warnsum&lang=en`
- Warning details: `https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=warningInfo&lang=en`
- Special weather tips: `https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=swt&lang=en`
- Historical daily max actuals: HKO/data.gov.hk daily maximum temperature dataset, specifically the Hong Kong Observatory station.

Parsing rules:

- 9-day forecast: read `weatherForecast[].forecastDate` and `weatherForecast[].forecastMaxtemp.value`.
- General local forecast: parse `forecastDesc` for `between {min} and {max} degrees`. Emit a warning log if the pattern is not found.
- Since-midnight actuals: use the Hong Kong Observatory automatic weather station row. This is the station that resolves the target markets.

Store every fetched HKO response as raw JSON/CSV plus normalized rows. The raw snapshot is part of the audit trail.

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

Later mode behind explicit config.

Required safeguards before enabling:

- `TRADING_MODE=live`
- configured wallet/funder/signature type
- max position and loss limits
- order-size caps
- kill switch
- dry-run parity tests passing
- manually reviewed market parser for the target market family

## 5. Polling And Update Detection

HKO webhooks are preferred if a reliable official event feed exists, but assume polling for v1.

Polling strategy:

- Baseline: every 60 seconds while relevant markets are open.
- Aggressive windows around expected HKO update times: every 5-15 seconds.
- Extra aggressive polling during live market-critical periods:
  - shortly before/after forecast issuance windows
  - late day when current observed max approaches key thresholds
  - after warning changes
  - after sudden weather changes from current report

Every HKO snapshot should produce a content hash. If the hash changes, the event bus emits `HKO_UPDATE_DETECTED`.

Required latency metrics:

- `fetched_at_utc`
- `hko_update_time` if present in payload
- `detected_delay_seconds`
- `parse_completed_at_utc`
- `signal_completed_at_utc`
- `market_snapshot_at_utc`

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
  - `forecast_min_c`
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
5. For forecast-latency trades, the bot exits when the market reprices, when the trade is invalidated, or when a timeout/risk rule triggers.
6. For market-settling observation events, such as a temperature threshold already being reached, the bot may hold to maturity if resolution risk is low and the predicate mapping is confirmed.

Inputs:

- Latest HKO 9-day forecast max for target date.
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

- If HKO forecast max changes by at least 1 C, mark outcomes whose target values are near the old reading or new reading.
- Treat exact outcomes and greater-than/less-than outcomes equally; the filter is proximity to the new information, not predicate type.
- Exclude far-away long shots whose likelihood only changes trivially. Example: if HKO raises target-day forecast max from `28 C` to `29 C`, do not buy `35 C`.
- For every affected outcome, classify the update's directional impact before looking at the post-update price.
- If current/official observed max has already exceeded an exact outcome, mark existing positions in that outcome for immediate exit.
- If current/official observed max has crossed a greater-than/less-than target value, mark that outcome's YES side as repricing-critical.
- If the update increases YES likelihood and YES has not moved up materially since the prior HKO reading, create a buy-YES candidate.
- If the update decreases YES likelihood and NO has not moved up materially since the prior HKO reading, create a buy-NO candidate.
- Treat unchanged prices, prices that moved too little, and prices that moved against the event as `PRICE_NOT_MOVED_WITH_EVENT`.
- If the market reprices by the configured take-profit amount, create a paper exit candidate.
- If the event is market-settling and the held token's predicate is already satisfied, allow hold-to-maturity instead of forcing take-profit exit.
- If the stale-price window expires before repricing, exit or cancel according to strategy config.
- If spread or depth makes the apparent stale price non-executable, log a missed opportunity rather than a trade.

## 8. Trade Candidate And Repricing Logic

No probability or fair-value estimate is required for v1.

For an entry candidate, compare the directional impact of the HKO event to the executable price change since the previous HKO reading:

- `event_relevance`: how directly the HKO update affects the outcome.
- `directional_impact`: whether the event increases, decreases, or does not materially change YES likelihood.
- `prior_price`: executable bid/ask immediately before the new HKO information.
- `current_price`: executable bid/ask after the new HKO information.
- `price_response`: whether price moved with the event enough to erase the latency opportunity.
- `price_lag`: affected outcome has not moved enough in the direction implied by the event.
- `spread_ok`: spread is tight enough to enter and later exit, unless the trade is explicitly hold-to-maturity after a settling observation.
- `depth_ok`: executable size exists at or near the stale price.
- `time_since_event`: still inside the latency capture window.

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
- The bot watches for repricing and paper-sells after price moves by the configured take-profit amount.

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
- Outcome liquidity supports the simulated fill.
- Risk caps pass.

Default stale-price thresholds:

- Entry: affected outcome has moved less than 1-2 cents after a material HKO event while neighboring/related outcomes or market context imply repricing should occur.
- Take profit: exit after 2-5 cents favorable movement, configurable per market liquidity.
- Stop/timeout: exit or cancel if repricing does not occur within the configured window.
- Hold to maturity: allowed when an official/current HKO observation has already satisfied the market predicate, the parser is high-confidence, and remaining resolution risk is mainly operational rather than meteorological.
- Live thresholds should start more conservative until paper data proves fill behavior.

## 9. Trading Scenarios

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

9-day forecast says `29 C`, but current observations and weather warnings imply suppressed heating.

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

## 10. Risk Controls

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

## 11. Execution Design

Paper order simulation should support:

- marketable limit buy at ask
- marketable limit sell at bid
- fill through depth up to requested size
- partial fills
- rejected orders when depth is insufficient
- explicit slippage assumptions

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

## 12. API Credentials Plan

Paper mode:

- No private key needed.
- Read-only Polymarket endpoints only.

Live mode:

- Use official Polymarket CLOB SDK.
- Derive L2 API credentials from wallet signing.
- If using the existing MetaMask/browser-wallet Polymarket account, use signature type `GNOSIS_SAFE` (`2`) and the Polymarket proxy wallet address as `POLY_FUNDER_ADDRESS`.
- Planned live setup: continue using the same Polymarket wallet/account even if account equity grows materially.
- Because the same wallet may hold a large balance, the bot must treat signing key exposure as the highest operational risk.
- Required env vars will likely include:
  - `POLY_PRIVATE_KEY`
  - `POLY_FUNDER_ADDRESS`
  - `POLY_SIGNATURE_TYPE`
  - `POLY_API_KEY`
  - `POLY_API_SECRET`
  - `POLY_API_PASSPHRASE`
  - `TRADING_MODE`

Never log secrets. Never store private keys in SQLite.

Large-wallet security requirements:

- Keep paper trading and read-only monitoring usable without any private key present.
- Load trading secrets only in live mode and fail closed if `TRADING_MODE` is not explicitly `live`.
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

## 13. Deployment Shape

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

## 14. Milestones

### Milestone 1: Project Skeleton

- Python package setup.
- Config loading.
- SQLite migrations.
- Structured logging.
- CLI entrypoint.

### Milestone 2: HKO Ingestion

- Fetch and store raw HKO forecast/current/warning snapshots.
- Normalize 9-day forecast rows.
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

### Milestone 5: Paper Trader

- Simulated fills.
- Position accounting.
- Mark-to-market PnL.
- Risk caps.
- Kill switch.

### Milestone 6: Review Gate For Live Trading

- Paper-trading performance report.
- Parser audit.
- Risk-control test suite.
- Wallet/API credential setup.
- Live mode remains disabled until explicitly enabled.

## 15. Open Questions

- HKO since-midnight max/min endpoint is confirmed: `https://data.weather.gov.hk/weatherAPI/hko_data/csdi/dataset/latest_since_midnight_maxmin_csdi_4.csv`.
- HKO 9-day forecast endpoint is confirmed: `https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=fnd&lang=en`; read `weatherForecast[].forecastMaxtemp.value`.
- HKO hourly local forecast summary endpoint is confirmed: `https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=flw&lang=en`; parse `forecastDesc` for `between {min} and {max} degrees`.
- Polymarket market semantics are confirmed from sampled May 4 and May 5, 2026 HK highest-temperature markets: final HKO Daily Extract `Absolute Daily Max (deg. C)`, one-decimal precision, no rounding for integer buckets, finalized data only, and no later revisions considered.
- Paper-mode daily drawdown is intentionally set to USD 4,000 / 80% for stress testing. Live-mode drawdown needs a safer value before enablement.
- Wallet plan for live mode: continue using the same Polymarket wallet/account even at larger account sizes. Existing MetaMask/browser-wallet Polymarket accounts generally use `GNOSIS_SAFE` signature type `2` with the Polymarket proxy wallet as funder. Security plan must assume the funder may hold materially more than the bot's daily trading risk.

## 16. References

- HKO Open Data API documentation: https://data.weather.gov.hk/weatherAPI/doc/HKO_Open_Data_API_Documentation.pdf
- HKO Weather API endpoint: https://data.weather.gov.hk/weatherAPI/opendata/weather.php
- HKO daily maximum/mean/minimum dataset: https://data.gov.hk/en-data/dataset/hk-hko-rss-daily-temperature-info-hko
- HKO climatological information page: https://www.weather.gov.hk/en/cis/climat.htm
- Polymarket authentication documentation: https://docs.polymarket.com/api-reference/authentication
- Polymarket trading overview: https://docs.polymarket.com/trading/overview
- Polymarket CLOB documentation: https://docs.polymarket.com/developers/CLOB/trades/trades-data-api
- Polymarket Python CLOB client: https://github.com/Polymarket/py-clob-client
