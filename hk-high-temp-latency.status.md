# HK High Temp Latency Status

Last updated: 2026-05-11 HKT

## Current State

Milestones 1-5 are implemented for local paper trading:

- Milestone 1: Python project skeleton, config, CLI, test setup.
- Milestone 2: HKO ingestion/parser layer for since-midnight CSV, the OCF HKO station forecast feed, and SQLite persistence.
- Milestone 3: Polymarket event/market parsing, CLOB orderbook parsing, and SQLite persistence.
- Milestone 4: Latency signal primitives for directional impact, price-response classification, and trade candidate generation.
- Milestone 5: Paper trader with executable-depth fills, position tracking, risk rejects, and CLI-ready local storage.

Live trading scaffolding is now implemented behind explicit fail-closed gates. Paper trading remains the default. Live mode has additive storage, Keychain hot-key setup, pre-derived credential loading, manual FAK buy/sell/cancel/reconcile commands, kill-switch settings, live tick/scheduler wiring, WebSocket market/user runtime wiring, and a live dashboard route. Real-auth smoke and real-money trading remain pending credentials, dependency validation, and explicit user approval. Low-latency readiness review now has a concrete roadmap in `docs/low-latency-readiness-roadmap.md`; the remaining readiness gaps are live network smoke, real-auth/manual-money smoke, and production proof of sub-second live DB-commit-to-decision latency.

Low-latency readiness M0/M1 groundwork is now implemented for AWS actual transitions, OCF forecast-sample changes, and market-resolution status changes. The storage layer can write append-only `latency_trace_events`, records orderbook state age on token-level paper decisions, and can enqueue max/min actual transition events immediately after the HKO current-temperature row commit, forecast-sample changed events immediately after OCF sample commits, and `market_resolution_changed` events after rediscovered Polymarket market status changes. Paper and live scheduler loops now create a shared low-latency queue for HKO/market ingestion and drain queued fast events before the normal watchdog tick, with latency stages for `db_committed`, `event_detected`, `decision_started`, and `decision_completed`; `FastDecisionWorker` provides a blocking queue worker with its own SQLite connection, and fast-event scheduler output includes compact per-event latency lines with event type, key, target date, and commit-to-detect timing. This does not yet complete the full roadmap: live network smoke, real-auth smoke, and production proof of sub-second DB-commit-to-decision latency remain pending.

Low-latency readiness M2 local scaffolding is implemented. `OrderBookCache` can apply Polymarket market-channel `book`, `price_change`, `best_bid_ask`, and `last_trade_price` fixture messages, persist append-only snapshots with WebSocket metadata, reject stale cached books at the configured 250 ms cap, and seed reconnect snapshots. Live buy execution now uses a fresh cache book before falling back to REST `/book`. Active-market YES/NO token listing and subscription-change payload planning are covered so market discovery can drive resubscribe messages without restarting the scheduler. `MarketWebSocketClient` can connect to the Polymarket market channel, send the active-token subscription payload, feed messages into the cache, and reconnect in a loop. `live-scheduler` now starts a scheduler-owned WebSocket runtime by default and passes its shared book cache into live tick and fast-event handlers. `live-network-smoke --live` can start and stop that runtime without trading and reports per-client connection attempts, connected-once state, applied message count, and last error. Live network smoke evidence remains pending.

Low-latency readiness M3 groundwork has standalone execution scheduler, candidate-planner primitives, and an active ladder metadata builder. Candidate actions declare conflict keys such as token, position, or shared risk budget keys; independent actions can run concurrently, while conflicting actions are serialized in deterministic input order. The actual-cross, actual low-cross, forecast-change, forecast-value, forecast-invalidation exit, and watchdog open-position exit hot paths now build planned candidate actions, preserve idempotent candidate keys/conflict keys, and execute them through `ExecutionScheduler` in SQLite-safe single-worker mode. `build_active_ladder_metadata` can precompute target-date token sides, latest book/tick/min-size metadata, held position state, and remaining budget; the actual max/min cross and forecast entry paths now reuse prefiltered highest/lowest outcome rows, and forecast/open-position exit handlers now use batched token-to-outcome row maps instead of per-position outcome lookups. A fake-clock live execution benchmark verifies decision-to-submit tracing under 100 ms excluding network.

Low-latency readiness M4 local scaffolding is implemented. `live_user_events` stores authenticated user-channel order/trade lifecycle events independently from final live position state, and `apply_user_channel_event` can map order lifecycle statuses, apply matched trade deltas to local positions exactly once, and converge a submitted order after a restart when a later user trade event arrives. `UserWebSocketClient` can authenticate to the Polymarket user channel, subscribe by active condition ID, apply incoming order/trade events, reconnect in a loop, and is owned by the live scheduler runtime with a separate SQLite connection. Startup and periodic live reconcile watchdogs now reconcile pending submitted/unknown-fill live orders through REST order/trade lookup, rebuild positions from filled live orders, compare open local live positions against CLOB sellable balances, repair the safe case where local shares exceed CLOB sellable shares by recording a local balance adjustment, and freeze new entries when unresolved drift remains. Resolved/closed past-date markets now settle remaining local paper/live positions at 1.0 or 0.0 once a stored target-date actual identifies the winning side, so missed same-day exit windows no longer leave unresolved local risk indefinitely. Real user-channel smoke, recent-trades validation, and live settlement validation remain pending.

Low-latency readiness M5 local scaffolding is implemented. Learned AWS actual publish windows now include a 10-second pre/post burst plan with 0.5-second cadence, while broader catchup polling remains. Scheduler source backoff can slow non-critical HKO sources without suppressing `aws_actual` polling. HKO raw snapshots now preserve fetch start time, header receipt time, payload receipt time, and response elapsed milliseconds for source-timing audits. `hko-source-timing-report` summarizes the persisted timing evidence for live dry-run review. Live hot-path buys now fail closed when a configured Polymarket WebSocket orderbook cache is missing or stale instead of silently falling back to REST.

Low-latency readiness M6 local scaffolding is implemented with a DB-specific exclusive live scheduler lock, stale submitted-order watchdog, structured startup-health evaluator, health-failure entry freeze, local/CLOB drift startup and periodic scans, persistent kill-switch exit enforcement, webhook alert transport, trade alerts, source-freshness breach alerts, stalled-WebSocket freezes, and live runbook. `live-scheduler` now fails closed if another process already holds the lock for the same SQLite database, and it freezes new entries when previously submitted live orders are older than the configured watchdog threshold. Startup health can now report disconnected market/user WebSockets, missing REST fallback, invalid credentials, insufficient balance/allowance, stale submitted orders, and local/CLOB drift as explicit fail-closed reasons; health failures can set `block_new_entries`, write a critical risk event, and emit an alert through `WHENITRAINS_ALERT_WEBHOOK_URL` when configured. When `cancel_open_orders_and_exit_positions` is enabled, live tick/scheduler startup and the live reconcile watchdog cancel all CLOB orders and attempt live exits for every locally open live position using the latest stored bid book. Filled live scheduler ticks emit trade alerts through the same sink, warmed-up scheduler loops emit critical alerts if required data freshness fails and decisions are skipped, and the live reconcile watchdog freezes entries if either scheduler-owned WebSocket worker is no longer alive. Manual live-auth, minimum-size order, capped scheduler, and real-account kill-switch validation remain pending.

Latency reporting can now summarize trace rows directly from the database. `latency-report <start_stage> <end_stage>` prints count plus p50/p95/p99 nearest-rank durations, while `low-latency-readiness-report` combines the core latency pairs, explicit evidence gates, live order/position counters, kill-switch state, and HKO source-timing evidence. Event-keyed live buy/sell execution records `order_submitted`, `clob_ack`, `fill_matched`, and `fill_confirmed` stages when the order fills, allowing live checks for HKO commit-to-decision and submit/fill timing.

The paper/live scheduler now performs a startup warmup loop before allowing trading decisions. On process start it may fetch HKO data, discover markets, and fetch orderbooks, but it skips the first trading tick so entries cannot be opened against a partially refreshed local data round.

Dashboard executable PnL now values open positions against only the latest orderbook snapshot. If the latest snapshot has no bid depth, older non-null bids are ignored so stale bids cannot create phantom unrealized gains.

Entry candidates now fail closed when the latest actual max/min has already invalidated the side being considered. This prevents forecast-change entries such as buying same-day `28°C YES` after the since-midnight max is already `29°C+`.

Same-day effective forecast values now combine actuals with remaining-hour forecast values: high uses `max(latest_since_midnight_max, forecast_hourly_max)` and low uses `min(latest_since_midnight_min, forecast_hourly_min)`. This prevents the bot from treating a lower remaining-day forecast as a lower full-day forecast after the actual high has already occurred.

Scheduler hot-read indexes are now additive migration state. The live `data/whenitrains.sqlite3` database was backed up before applying them, then migrated with indexes for latest orderbook snapshots, OCF forecast samples, HKO forecast rows, and HKO observation reads. `EXPLAIN QUERY PLAN` on the live DB confirms the latest orderbook, latest OCF sample, and latest HKO forecast reads use those indexes instead of scanning the append-only historical tables.

Scheduler readiness now fails closed on the current loop: if a due HKO fetch, market discovery, orderbook refresh, or startup actual warmup fetch fails, decisions are skipped for that loop. Startup warmup does not complete until the startup data path succeeds, and schedulers using the background AWS actual poller perform a synchronous startup actual fetch before trading is enabled.

Forecast-change and actual-cross events are retryable when orderbook prerequisites are missing. A decision row with an event key no longer counts as processed unless it is an explicit `EVENT` / `processed` marker, and those markers are written only after the event has enough market data to make a terminal trade/no-trade decision.

Actual-cross source preference now applies per value transition. AWS actual transitions are preferred when AWS provides a max/min transition; otherwise the runner falls back to the since-midnight observation stream instead of suppressing usable CSDI transitions merely because any AWS row exists.

Dashboard forecast-panel loading is optimized for the append-only live database. The page still loads stats, forecast panels, and PnL in parallel, but forecast panels no longer compute latest orderbooks by grouping the full `orderbook_snapshots` table for every panel. They now fetch the small set of market tokens first, read each token's latest ask through the existing latest-orderbook index, bucket orderbook chart history in SQL, parse OCF high/low series in one pass per panel, and skip duplicate OCF rows with the same HKO update timestamp before JSON parsing.

The scheduler orderbook refresh now fetches independent CLOB token books concurrently, then writes snapshots through the main SQLite connection in deterministic outcome order. The previous implementation fetched every outcome as serial `YES` then `NO` HTTP calls, so a 15-second refresh interval could still take close to a minute when many current/future HK outcomes were active. Individual token failures remain non-fatal warnings.

2026-05-10 live audit finding: the 15:50 HKT AWS actual max transition `25.6 -> 26.1` was first stored locally at 15:57:52 HKT, while the 26°C YES ask had already moved from about `0.14` at 15:46:52 to `0.95` at 15:57:48. The scheduler did observe the actual max change, but prior actual-cross logic did not emit exact-bucket YES candidates and the fallback forecast-value path skipped `26°C YES` because later hourly forecast values were below the bucket guard. Exact-bucket actual-cross fast-lane logic now includes both the crossed exact-bucket YES token and any NO token invalidated by the same official actual move, with a `0.75` YES cap. The surprise basis uses the preceding hourly forecast as of the actual observation, while the later-hours-lower confirmation uses the newest hourly forecast available at decision time.

2026-05-10 low-latency readiness review: audited latest commit `a80490c Speed up scheduler orderbook polling`, verified the concurrent orderbook fetch test and four exact-bucket fast-lane tests are green, and added `docs/low-latency-readiness-roadmap.md`. The exact-bucket actual-cross surprise is only one strategy family. The low-latency architecture must cover all alpha producers, including forecast changes, forecast-value mispricings, actual invalidation exits, lowest-temperature markets, different lead dates, and later city/market families. One source event may fan out into several actions, such as buying the crossed bin YES, selling an invalidated YES position already held, and buying the invalidated bin NO; independent alpha events in other markets or strategies must be able to dispatch orders concurrently subject to token, position, and risk-budget ordering. Current readiness gap is architectural: background AWS ingestion can write promptly, but trading decisioning still waits for the scheduler loop; live execution refreshes target books with REST in the hot path; and live order reconciliation uses REST/order-balance checks rather than Polymarket user WebSocket order/trade events. Research confirms Polymarket provides a market WebSocket for `book`, `price_change`, `last_trade_price`, and optional `best_bid_ask` events, plus an authenticated user WebSocket for order/trade lifecycle events. Roadmap priority is latency instrumentation, DB-change driven fast decisioning, WebSocket book cache, candidate fan-out and parallel execution, user WebSocket reconciliation, then HKO burst polling/backoff and operational fail-closed checks.

2026-05-11 low-latency implementation pass: added `src/whenitrains/low_latency.py`, `tests/test_low_latency.py`, append-only `latency_trace_events`, orderbook-age enrichment for paper decisions, scheduler fast-event queue draining, and CLI wiring so paper/live schedulers share the queue with AWS actual ingestion. New tests cover immediate enqueue after AWS actual commit, fake-clock decision start under 1 second after commit, orderbook age in decision details, and scheduler fast-queue precedence over the watchdog tick. Verified targeted suites: `PYTHONPATH=src python3 -m unittest tests.test_low_latency`, `tests.test_scheduler`, `tests.test_runner`, `tests.test_storage`, and two current-temperature CLI tests.

2026-05-11 forecast fast-event pass: OCF forecast sample storage now emits `forecast_sample_changed` events after commit when a target-date sample changes, records latency stages, and lets the paper scheduler default-dispatch those events to `process_forecast_entries` instead of the full watchdog tick. CLI paper/live scheduler forecast fetches pass the shared low-latency queue into OCF sample storage. Verification covers enqueue timing, default forecast-event dispatch, scheduler drain behavior, and existing forecast CLI/storage paths.

2026-05-11 market-resolution fast-event pass: Polymarket event parsing now preserves event status, market storage emits `market_resolution_changed` after status changes on rediscovery, and the low-latency dispatcher routes those events to the open-position exit handler. Scheduler market discovery passes the shared low-latency queue so resolution status changes can wake the fast path. Verification covers status parsing, enqueue timing, default dispatch, storage compatibility, and discovery behavior.

2026-05-11 compact latency-line pass: fast-event scheduler output now includes compact `latency_event=...` lines for each drained alpha event, including event kind, key, target date, commit-to-detect milliseconds, and transition name when available. Verification covers the formatter and scheduler output for drained fast events.

2026-05-11 blocking fast-worker pass: added `FastDecisionWorker`, which blocks on `LowLatencyEventQueue`, owns a separate SQLite connection, dispatches the same narrow fast-event handlers, and records decision start/completion stages without waiting for the scheduler loop. `paper-scheduler` starts this worker around the scheduled loop; live scheduler stays on explicit queue draining until live-client thread ownership is separately proven. Verification covers a worker processing a queued forecast event and writing latency stages from its own connection, plus paper scheduler worker lifecycle wiring.

2026-05-11 market WebSocket cache pass: added `src/whenitrains/orderbook_cache.py` with market subscription payloads, fixture-driven book cache updates, stale-cache rejection, snapshot persistence metadata, active token listing, and subscription-change detection. Live execution now accepts an optional book cache and avoids the hot-path REST `/book` fetch when a fresh cache book is available. Verified `PYTHONPATH=src python3 -m unittest tests.test_orderbook_cache` plus focused live runner tests.

2026-05-11 market WebSocket client pass: added `src/whenitrains/market_websocket.py`, declared the `websockets` dependency, and covered subscription send plus cache application with fake async connections. Scheduler process ownership and live smoke remain pending. Verified with `PYTHONPATH=src python3 -m unittest tests.test_market_websocket tests.test_orderbook_cache`.

2026-05-11 live WebSocket runtime wiring pass: added `src/whenitrains/live_runtime.py` and wired `live-scheduler` to start market/user WebSocket clients by default, pass the shared market book cache to normal and fast live tick handlers, and stop the runtime on scheduler exit. `--no-websockets` remains available as an explicit escape hatch. Verification covers runtime start/stop, separate user-channel DB ownership, CLI cache wiring, and user client connection close.

2026-05-11 execution scheduler pass: added `src/whenitrains/execution_scheduler.py` and `tests/test_execution_scheduler.py`. Verification covers independent candidate concurrency and deterministic serialization for conflicting token/risk keys. Runner integration and source-event candidate planning remain pending.

2026-05-11 candidate planner pass: added `src/whenitrains/candidate_planner.py` and `tests/test_candidate_planner.py`. Verification covers actual-cross fan-out ordering, idempotent candidate keys, and token/position/risk conflict keys for downstream execution scheduling.

2026-05-11 planner execution bridge pass: added `executable_candidate_actions` so planned candidate fan-out can feed `ExecutionScheduler` without losing candidate keys or conflict keys. Verification covers callable binding and result propagation.

2026-05-11 ladder metadata precompute pass: added `src/whenitrains/ladder_metadata.py` with target-date token-side metadata for active highest/lowest ladders, including latest book prices, tick size, min order size, open position state, and remaining budget. `store_orderbook` now persists tick/min-size metadata into snapshots so the precompute path can avoid hard-coded exchange defaults. Verification covers the metadata builder and latest-orderbook parsing.

2026-05-11 actual-cross metadata wiring pass: `process_actual_entries` now builds active ladder metadata once per target date and reuses a single prefiltered highest/lowest outcome row set across max/min actual-cross transition handlers. Verification covers the hot path without direct outcome-list reads plus existing actual-cross behavior tests.

2026-05-11 forecast metadata wiring pass: `process_forecast_entries` and `process_forecast_value_entry` now accept and reuse prefiltered highest/lowest outcome rows across forecast-change and forecast-value handlers. Verification covers the hot path without direct outcome-list reads plus focused forecast-change/value behavior tests.

2026-05-11 exit metadata wiring pass: forecast invalidation exits and watchdog open-position exits now accept precomputed token-to-outcome row maps and default to batched outcome reads instead of per-open-position `find_outcome_by_token` lookups. Verification patches that lookup to fail while exercising both exit paths, preserves the candidate execution bridge tests, and confirms actual-cross planner flushing preserves row-order decision writes.

2026-05-11 live log access attempt: attempted to fetch `http://192.168.1.23:8765/` from this machine for live smoke evidence, but the request timed out after 75 seconds without connecting. Live network smoke and production sub-second commit-to-decision proof remain unverified in this session.

2026-05-11 live log access retry: retried `curl -L --max-time 8 http://192.168.1.23:8765/`. The sandboxed request timed out after 8 seconds, and the approved LAN request failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765`. Live-log access is blocked on the LAN endpoint being reachable.

2026-05-11 live execution benchmark pass: added a fake-clock test that records `decision_started` then runs `execute_live_buy`, verifying `decision_started -> order_submitted` p95 is under 100 ms excluding network.

2026-05-11 actual-cross runner planner pass: actual max-cross candidate executions now flow through planned candidate actions and `ExecutionScheduler`, with single-worker in-thread execution to avoid SQLite cross-thread access. Verification covers fast-lane bridge use plus the focused actual-cross behavior matrix.

2026-05-11 forecast-change runner planner pass: forecast-change candidate buys now flow through planned candidate actions and `ExecutionScheduler`, preserving per-token and shared entry-budget conflict keys while keeping SQLite execution single-threaded. Verification covers the bridge-specific regression plus existing forecast-change behavior tests.

2026-05-11 forecast-value runner planner pass: forecast-value add-on buys now flow through planned candidate actions and `ExecutionScheduler`, preserving repeated-dip budget checks through the existing execution path. Verification covers the bridge-specific regression plus focused forecast-value budget/idempotency tests.

2026-05-11 actual low-cross runner planner pass: lowest-temperature actual-cross buys now flow through planned candidate actions and `ExecutionScheduler`, preserving per-candidate entry caps and shared entry-budget conflict keys. Verification covers the bridge-specific regression plus existing actual low-cross retry/fill tests.

2026-05-11 forecast-exit runner planner pass: forecast invalidation exits now flow through planned sell actions and `ExecutionScheduler`, preserving token and position conflict keys while keeping missing-book retry behavior inline. Verification covers the bridge-specific regression plus existing forecast invalidation sell behavior.

2026-05-11 open-position exit planner pass: watchdog-style actual/hourly invalidation exits now flow through planned sell actions and `ExecutionScheduler`, preserving historical success-note behavior while adding token and position conflict keys. Verification covers the bridge-specific regression plus existing invalidated-exit and no-depth behavior tests.

2026-05-11 user WebSocket reconciliation pass: added `src/whenitrains/live_user_stream.py`, additive `live_user_events` storage, and `tests/test_live_user_stream.py`. Verification covers `PLACEMENT`, `UPDATE`, `CANCELLATION`, `MATCHED`, `MINED`, `CONFIRMED`, `RETRYING`, and `FAILED` order fixtures, idempotent matched trade application, and crash/restart convergence from a submitted row plus a later user trade event.

2026-05-11 user WebSocket client pass: added `src/whenitrains/user_websocket.py` with authenticated subscription payloads, condition-ID filtering, event application, and reconnect-loop support. Verification covers fake async user-channel delivery into live position reconciliation plus existing live user event fixtures.

2026-05-11 active condition subscription pass: added `list_active_market_condition_ids` so the user WebSocket can subscribe from active market condition IDs rather than asset/token IDs. Verification covers filtering out past markets and preserves active token subscription tests.

2026-05-11 polling hardening pass: added sub-second burst cadence for learned AWS actual publish windows and non-critical source backoff isolation. Verified with `PYTHONPATH=src python3 -m unittest tests.test_scheduler`.

2026-05-11 HKO response timing pass: `fetch_response` now records fetch start, header receipt, payload receipt, and elapsed milliseconds, and all HKO raw snapshot writes persist those timings for later source-latency audits. Verification covers storage migration/insert behavior, `_fetch_current_temperature` persistence, and measured `fetch_response` timing.

2026-05-11 orderbook freshness gate pass: live hot-path buys with a configured `OrderBookCache` now skip trading when the cache is missing or stale, preserving REST fallback only when no WebSocket cache has been configured. Verified with focused live runner tests for stale cache skip, fresh cache no-REST execution, and no-cache REST refresh.

2026-05-11 operational lock pass: added `src/whenitrains/operational.py`, `tests/test_operational_readiness.py`, and live-scheduler lock wiring. Verification covers rejecting a second lock holder for the same DB and lock path selection.

2026-05-11 startup health evaluator pass: added structured `evaluate_live_startup_health` checks for market/user WebSocket connectivity, REST fallback, credentials, balance/allowance, stale submitted orders, and local/CLOB drift. Verification covers single and aggregate fail-closed reasons.

2026-05-11 health freeze pass: added `freeze_new_entries_for_health_failures`, which turns failed startup health into `block_new_entries` plus a critical `live_startup_health_failed` risk event. Verification covers both freeze and healthy no-op paths.

2026-05-11 live drift startup pass: added `find_live_position_drifts` to compare open local live positions against CLOB sellable balances, and wired `live-scheduler` startup health to freeze new entries when drift is detected. Verification covers matched balances, mismatches, unknown balances, and CLI startup freeze behavior.

2026-05-11 periodic reconcile watchdog pass: added a scheduler-level `reconcile_watchdog_fn` hook and wired live scheduler to run position drift scans before normal or fast decision handling. New drift during live operation freezes entries and records the critical startup-health risk event. Verification covers hook ordering and CLI watchdog freeze behavior.

2026-05-11 drift repair pass: added `repair_live_position_drifts` for the safe local-greater-than-CLOB case, recording a `RECONCILE_SELL` local balance adjustment before freezing. The live reconcile watchdog now attempts that repair and only freezes when drift remains unresolved. Verification covers direct repair accounting and CLI repair-before-freeze behavior.

2026-05-11 alerting pass: added `src/whenitrains/alerting.py` with alert message formatting, memory sink, webhook sink, and `WHENITRAINS_ALERT_WEBHOOK_URL` environment factory. Live scheduler startup and reconcile health freezes now emit critical alerts when a sink is configured. Verification covers formatting, webhook JSON payloads, env factory behavior, and freeze alert emission.

2026-05-11 trade alert pass: scheduler ticks with filled buys or sells now emit info alerts through the configured alert sink, aligned with the existing loud trade log. `live-scheduler` passes its alert sink into the scheduled loop. Verification covers scheduler alert payloads and CLI sink wiring.

2026-05-11 source freshness alert pass: warmed-up scheduler loops now emit a critical alert when a required HKO, market discovery, or orderbook data path fails and decisions are skipped. Verification covers orderbook refresh failure after warmup and preserves existing trade alert behavior.

2026-05-11 stalled WebSocket watchdog pass: `LiveWebSocketRuntime` now reports whether both market and user WebSocket workers are alive, and the live reconcile watchdog freezes entries through startup-health failure handling when the runtime stalls. Verification covers runtime liveness and CLI watchdog freeze behavior.

2026-05-11 live runbook pass: added `docs/low-latency-live-runbook.md` covering live scheduler start, stop, disabling entries, cancel-all, reconcile, crash restart, critical alerts, and return-to-normal criteria.

2026-05-11 latency reporting pass: added `latency_duration_summary` and `whenitrains latency-report`. Verification covers percentile calculation, ignoring incomplete events, and CLI output.

2026-05-11 stale live-order watchdog pass: added `freeze_new_entries_for_stale_submitted_orders` and live-scheduler startup wiring. The watchdog sets `block_new_entries`, records a critical `live_stale_submitted_orders` risk event, and still lets the scheduler continue in exit/reconcile-capable mode. Verified with `PYTHONPATH=src python3 -m unittest tests.test_live.LiveTests.test_stale_submitted_order_watchdog_freezes_new_entries`.

2026-05-11 live kill-switch exit pass: added `enforce_live_kill_switch_exits`, which honors the persistent `cancel_open_orders_and_exit_positions` setting by calling CLOB `cancel_all()` and attempting live FAK sells for each locally open live position from the latest stored bid book. `live-tick`, `live-scheduler` startup, and the live reconcile watchdog now invoke this enforcement path so the persistent setting has runtime effect beyond the manual CLI toggle. Verification covers direct cancel/sell behavior and scheduler startup/watchdog wiring.

2026-05-11 pending live-order reconcile pass: added `reconcile_pending_live_orders`, which reconciles pending/unknown-fill live order rows through existing CLOB order/trade lookup logic and rebuilds local live positions from filled orders. `live-reconcile`, `live-scheduler` startup, and the live reconcile watchdog now share this helper before drift scans. Verification covers fill application, position rebuild, and scheduler startup/watchdog wiring.

2026-05-11 resolved market settlement pass: open-position exit handling now recognizes resolved/closed past-date markets and locally settles remaining paper/live positions using the stored actual for the market target date. Winning-side positions settle at 1.0, losing-side positions settle at 0.0, and the accounting path records a settlement order plus realized PnL without requiring bid depth. Verification covers paper and live-local settlement.

2026-05-11 readiness audit pass: added `docs/low-latency-readiness-audit.md` and refreshed `docs/low-latency-readiness-roadmap.md` so the roadmap reflects current local implementation rather than the original pre-implementation audit. The audit maps M0-M6 deliverables to concrete files/tests and identifies the remaining live-environment proof points. Retried `curl -L --max-time 8 http://192.168.1.23:8765/`; the endpoint still failed with `curl: (7) Failed to connect to 192.168.1.23 port 8765`.

2026-05-11 HKO timing report pass: added `hko-source-timing-report`, which reads persisted `raw_snapshots` timing rows and prints response latency percentiles, fetch-second offsets, HTTP `Last-Modified` minute offsets, the covered window, and latest endpoint. This gives the live dry-run a direct report command once live DB/log access is restored. Verification covers AWS GIS timing rows and CLI output.

2026-05-11 readiness report pass: added `low-latency-readiness-report`, a read-only production evidence command that prints the core latency stage-pair percentiles, live order and open-position counters, kill-switch state, and embedded HKO source timing report. Verification covers representative trace rows, live order state, and CLI output.

2026-05-11 live network smoke command pass: added `live-network-smoke --live --seconds N`, which starts the scheduler-owned market/user WebSocket runtime, waits briefly, prints `websocket_all_running`, and stops the runtime without running trading decisions. Market/user WebSocket clients now expose connection attempts, connected-once state, applied message count, and last error so the smoke output proves more than thread liveness. The live runbook and readiness audit now point to this as the first no-trade network evidence step. Verification covers CLI wiring, runtime start/stop, connection status capture, and that `run_live_tick` is not called.

2026-05-11 live execution latency pass: event-keyed live buy/sell paths now record `order_submitted`, `clob_ack`, `fill_matched`, and `fill_confirmed` latency trace stages around FAK submit and local fill application. Verified with `PYTHONPATH=src python3 -m unittest tests.test_live.LiveTests.test_execute_live_buy_records_latency_stages_for_event_key` and `tests.test_latency_report`.

2026-05-11 completion audit/status cleanup pass: re-audited `docs/low-latency-readiness-roadmap.md` against `docs/low-latency-readiness-audit.md`, CLI tooling, tests, and the status file. The top-level current-state summary now consistently describes M2-M6 as local scaffolding that is implemented, while preserving the blocked live evidence requirements: live WebSocket smoke, real-auth/manual-money smoke, production latency percentiles, live settlement validation, capped scheduler evidence, and real-account kill-switch validation. Retried `curl -L --max-time 8 http://192.168.1.23:8765/`; it failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`.

2026-05-11 readiness evidence gate pass: `low-latency-readiness-report` now emits explicit evidence gates for HKO commit-to-decision under 1 second, observed decision-to-submit traces, observed submit-to-fill traces, and observed HKO source timing rows. This makes a captured live report easier to evaluate as pass/missing evidence instead of relying only on raw percentile lines. Verified with `PYTHONPATH=src python3 -m unittest tests.test_latency_report`.

Past-date unresolved local positions are now handled once the local market row is resolved/closed and a stored actual for that target date identifies the winning side. The remaining settlement evidence gap is live validation against real resolved CLOB/onchain state.

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

Primary station forecast feed: `https://www.hko.gov.hk/wxinfo/awsgis/forecast/HKO.xml`

Fallback station forecast feed: `https://maps.weather.gov.hk/ocf/dat/HKO.xml`

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

Current green run after adding the paper runner, dashboard, missed-decision logging, actual-cross entry handling, live scaffolding, AWS GIS actual priority, backtesting harnesses, scheduler logging fixes, and web UI updates:

```text
Ran 151 tests in 0.690s
OK
```

Startup warmup guard red/green:

```bash
PYTHONPATH=src python3 -m unittest tests.test_scheduler.SchedulerTests.test_scheduler_skips_trading_on_startup_warmup_tick
```

Red result: scheduler called the trading tick twice across two scheduler loops, including the first loop.

Green result after adding `SchedulerState.trading_warmed_up`:

```text
Ran 1 test in 0.022s
OK
```

Stale-bid dashboard valuation red/green:

```bash
PYTHONPATH=src python3 -m unittest tests.test_dashboard_server.DashboardServerTests.test_paper_trade_rows_do_not_use_stale_bid_after_latest_book_has_no_bid
```

Red result: paper trade rows used an older `0.50` bid even after the latest orderbook snapshot had no bid.

Green result after making latest-bid valuation use the newest snapshot regardless of bid nullability:

```text
Ran 1 test in 0.027s
OK
```

Actual-invalidation entry guard red/green:

```bash
PYTHONPATH=src python3 -m unittest tests.test_runner.RunnerTests.test_forecast_change_effective_high_includes_actual_max
```

Red result: forecast-change logic still treated hourly forecast `29.1 -> 28.8` as an effective high drop even though latest actual max was `29.6`.

Green result after folding same-day actual max into effective high:

```text
Ran 1 test in 0.019s
OK
```

The matching low-side test also passes:

```bash
PYTHONPATH=src python3 -m unittest tests.test_runner.RunnerTests.test_lowest_forecast_change_effective_low_includes_actual_min
```

Scheduler latency index red/green:

```bash
PYTHONPATH=src python3 -m unittest tests.test_storage.StorageTests.test_migrate_creates_scheduler_latency_indexes tests.test_storage.StorageTests.test_latest_scheduler_queries_use_latency_indexes
```

Red result: migration created no named scheduler latency indexes, and latest orderbook reads scanned `orderbook_snapshots` with a temp sort.

Green result after adding additive indexes in `storage.migrate`:

```text
Ran 2 tests in 0.012s
OK
```

Scheduler readiness red/green:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.test_scheduler.SchedulerTests.test_scheduler_does_not_warm_up_until_startup_fetches_succeed \
  tests.test_scheduler.SchedulerTests.test_scheduler_skips_decisions_when_due_orderbook_refresh_fails_after_warmup \
  tests.test_scheduler.SchedulerTests.test_scheduler_startup_warmup_fetches_actual_when_background_poller_is_enabled
```

Green result after adding per-loop data-failure gating and synchronous startup actual warmup:

```text
Ran 3 tests in 0.063s
OK
```

Retryable event prerequisite red/green:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.test_runner.RunnerTests.test_forecast_change_missing_orderbooks_is_retryable \
  tests.test_runner.RunnerTests.test_actual_cross_missing_orderbooks_is_retryable \
  tests.test_runner.RunnerTests.test_actual_low_cross_missing_orderbooks_is_retryable
```

Green result after delaying processed markers until orderbook prerequisites are available:

```text
Ran 3 tests in 0.099s
OK
```

AWS/CSDI transition preference red/green:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.test_runner.RunnerTests.test_actual_cross_falls_back_to_csdi_when_aws_has_no_max_transition \
  tests.test_runner.RunnerTests.test_actual_cross_prefers_aws_max_transitions_when_available
```

Green result after preferring AWS only when it provides a same-value transition:

```text
Ran 2 tests in 0.017s
OK
```

Concurrent orderbook refresh red/green:

```bash
PYTHONPATH=src python3 -m unittest tests.test_cli.CliDiscoveryTests.test_fetch_orderbooks_fetches_tokens_concurrently_and_stores_snapshots
```

Red result: `_fetch_orderbooks` rejected the test-only `max_workers` argument and still had no concurrent fetch path.

Green result after fetching token books in a `ThreadPoolExecutor` and storing snapshots sequentially:

```text
Ran 1 test in 0.042s
OK
```

Exact-bucket actual-cross fast-lane red/green:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.test_runner.RunnerTests.test_actual_cross_yes_allows_seventy_five_cent_entry_in_peak_hour_sure_bet \
  tests.test_runner.RunnerTests.test_actual_cross_yes_rejects_above_seventy_five_cents_in_peak_hour_sure_bet \
  tests.test_runner.RunnerTests.test_actual_cross_fast_lane_buys_exact_yes_and_invalidated_no_when_latest_forecast_agrees \
  tests.test_runner.RunnerTests.test_actual_cross_fast_lane_skips_exact_yes_when_latest_forecast_later_hour_reaches_actual
```

Red result: peak-hour actual-cross YES still allowed `0.76`, exact-bucket crosses did not buy the crossed exact YES token, and a failed newest-hourly-forecast confirmation produced no terminal missed decision.

Green result after lowering the peak-hour cap to `0.75`, adding exact-bucket YES side selection, bypassing stale-price movement only for confirmed exact-bucket fast-lane YES, and requiring the preceding hourly forecast to put the observed hour at the forecast peak while the newest hourly forecast keeps every later same-day hour below the actual cross:

```text
Ran 6 tests in 0.197s
OK
```

Forecast-timing refinement:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.test_runner.RunnerTests.test_actual_cross_fast_lane_uses_preceding_forecast_even_when_new_forecast_marks_current_hour_at_actual \
  tests.test_runner.RunnerTests.test_actual_cross_fast_lane_requires_preceding_forecast_basis
```

Green result after splitting the guard into a preceding hourly forecast selected by `fetched_at_utc <= observed_at_hkt` for the surprise basis and the newest hourly forecast for later-hour confirmation:

```text
Ran 2 tests
OK
```

Dashboard load-time red/green:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.test_dashboard_server.DashboardServerTests.test_latest_market_token_price_rows_uses_token_scoped_latest_reads \
  tests.test_dashboard_server.DashboardServerTests.test_bucketed_orderbook_ask_points_collapses_raw_snapshots_in_sql \
  tests.test_dashboard_server.DashboardServerTests.test_forecast_series_dedupes_ocf_samples_by_update_time
```

Red result: tests failed to import the new helper APIs before the optimization existed.

Green result after replacing full-table latest-orderbook grouping with token-scoped latest reads and SQL bucketed chart history:

```text
Ran 3 tests
OK
```

Dashboard regression run:

```bash
PYTHONPATH=src python3 -m unittest tests.test_dashboard_server
```

Green result:

```text
Ran 25 tests in 1.618s
OK
```

Full suite:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

Green result:

```text
Ran 195 tests in 12.708s
OK
```

Live-size local DB timing against `data/whenitrains.sqlite3` on 2026-05-08 HKT:

- Before optimization: `/api/forecast-panels?side=YES` measured `6.026s`, `11.846s`, and `24.527s` in one three-run pass.
- After token-scoped latest reads and SQL bucketing: `/api/forecast-panels?side=YES` measured about `2.8s` to `3.5s`.
- After one-pass OCF high/low parsing: `/api/forecast-panels?side=YES` measured about `1.6s` to `2.1s`; `/api/forecast-panels?side=NO` measured about `1.7s` to `2.1s`.
- After duplicate OCF update-time pruning: warm `/api/forecast-panels?side=YES` measured about `0.7s` to `1.1s` with cold/contention runs around `1.4s` to `2.6s`; warm `/api/forecast-panels?side=NO` measured about `0.65s` to `0.72s`.

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
- Applies entry max-price, best-ask-plus-slippage, and minimum-fill guards.
- Freezes new buys after the paper-mode 80% daily drawdown limit.
- Persists paper orders and paper positions keyed by CLOB token ID.
- Allows forecast-value add-on buys until the per-token position budget is reached.
- Calculates entry quote: limit price, average fill, shares, and cost.
- Calculates exit condition using current executable bid minus average entry price.
- Manual paper commands still expose take-profit and max-hold checks. The scheduler path now holds forecast positions until forecast or same-date actual invalidation instead of automatically exiting on take-profit or 10-minute timeout.
- Runs a local autonomous paper tick/loop that fetches HKO, discovers current/future OCF forecast-date markets, refreshes active orderbooks, detects HKO events, writes paper decisions, places paper buys, and exits invalidated open positions.
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

Paper-mode milestones 1-5 are complete as local building blocks, one-shot CLI commands, an autonomous local paper loop, and a polling-window scheduler. Remaining work is now the live-trading validation layer and production evidence:

- Restore or verify live-log LAN access from this development machine.
- Run real-auth CLOB smoke with installed dependency and credentials.
- Run explicit manual real-money buy/sell smoke before any scheduler use.
- Verify production p50/p95/p99 latency from HKO DB commit through decision start and live order submission.
- Verify live kill-switch behavior against the real account before scheduler use.
- Add integration tests using recorded HKO/Gamma/CLOB fixtures.

## Scheduler/Alert/Dashboard Decisions

Scheduler defaults for the POC:

- HKO AWS GIS actuals: `https://www.hko.gov.hk/wxinfo/awsgis/latestReadings_AWS1_v2.txt` is the priority D+0 actual source. Parse the `HKO` row and store decimal `TEMP`, `MAXTEMP`, and `MINTEMP` as current temperature, since-midnight max, and since-midnight min.
- HKO AWS GIS actuals: poll regular observed-reading slots every 5 minutes, with 10-second cadence from 30 seconds before through 30 seconds after each slot.
- HKO AWS GIS actuals: also learn fetchable/publish minutes from HTTP `Last-Modified` under `aws_gis_actual`; these learned publish minutes are expanded into the matching 10-minute publish pattern and use a wider 2-minute buffer on each side, still at 10-second cadence. Example: a payload reading labeled `19:30` first became fetchable after the file publish around `19:38`, so the scheduler should cover both the `19:30` observed-reading window and the learned `:08/:18/:28/:38/:48/:58` publish pattern.
- HKO AWS GIS actuals: a dedicated scheduler worker fetches current actuals with its own SQLite connections, so market discovery/orderbook refresh and paper/live decision work cannot delay actual ingestion inside active windows.
- AWS GIS failures: `rhrread` may be stored as an observation fallback under `rhrread_actual`, but the scheduler must still log `aws_actual fetch failed` and must not mark the AWS window complete.
- HKO since-midnight max/min CSV: source updates extremely regularly every 10 minutes, typically near `:00`, `:09`, `:19`, `:29`, `:38`, `:48`, and `:58`; poll from 10:00 to 20:00 HKT only.
- HKO since-midnight max/min CSV: for each expected publication time, poll from T-1m through T+2m every 10 seconds as an observation/cross-check source. If the content hash changes, perform one confirmation fetch, then stop polling that window.
- HKO AWS GIS station forecast feed: source is `https://www.hko.gov.hk/wxinfo/awsgis/forecast/HKO.xml`. It returns JSON despite the `.xml` extension, with `DailyForecast` and `HourlyWeatherForecast`.
- HKO AWS GIS station forecast feed: hourly forecast rows carry decimal temperatures when available and cover the full available station horizon, not only D+0 or the next 24 hours. The runner uses hourly-path min/max first for every covered date, then daily decimal max/min if no hourly rows are available.
- HKO OCF station forecast feed: `https://maps.weather.gov.hk/ocf/dat/HKO.xml` remains a same-shape fallback if AWS GIS forecast fetch or parse fails.
- Station forecast feed: stored payload `LastModified` changes are irregular but roughly hourly, with median observed gaps near 60 minutes and common gaps around 40, 60, and 80 minutes. The hourly forecast rows are a full path republished with each payload version.
- Station forecast feed: every fetch stores full response headers plus HTTP `Date`, HTTP `Last-Modified`, and `ETag`. Raw snapshots are no longer deduped by content hash because unchanged payloads can still provide useful response metadata.
- Station forecast feed: payload `LastModified` and HTTP `Last-Modified` are converted to HKT minute-of-day entries in `hko_source_update_minutes`. The scheduler includes those learned minutes as daily forecast poll windows while keeping the coarse 10-minute discovery probe.
- Polymarket/orderbooks: monitor target-day markets until the Hong Kong day ends.
- Future-date forecast trading: market discovery now runs for every OCF forecast date at or after the current HKT date. Orderbook polling covers all discovered HK high-temperature outcomes. Forecast-change entries are evaluated per target date.
- Current-day actual trading: AWS GIS actual-cross entries, actual invalidation, and hold-to-maturity logic remain current-day only. Future-date positions can still exit by forecast invalidation or risk rule, but are not invalidated by today's actual max.
- Current scheduler implementation: `paper-scheduler` runs a dedicated AWS actual polling worker, evaluates other HKO source windows every loop, refreshes all discovered HK high-temperature orderbooks on a separate 15-second cadence, discovers markets for all current/future OCF forecast dates on a 5-minute cadence, and runs the paper decision pass every loop.
- Orderbook refresh now runs concurrent per-token CLOB fetches with a default worker cap of 16, then persists snapshots sequentially. This keeps SQLite single-threaded while avoiding the previous full-sweep latency from serial YES/NO HTTP calls.
- Scheduler output is quiet by default: orderbook-only/no-op ticks are suppressed. It prints when HKO is fetched, a signal/trade/missed-trade occurs, a non-noop decision is made, or AWS actual fetch fails.
- Use `paper-scheduler --verbose` to restore noisy output: every scheduler tick plus all orderbook bid/ask lines.
- HKO source polling respects the in-window 10-second cadence; unchanged HKO payloads no longer print every scheduler tick.
- AWS actual windows may overlap; overlap is intentional and increases coverage without causing multiple AWS fetches in a single scheduler tick.
- Individual Polymarket CLOB orderbook fetch failures are logged as warnings and do not crash the scheduler.
- Low-latency roadmap: latency instrumentation, DB-change driven AWS actual, OCF forecast, and Polymarket market-resolution eventing, Polymarket market WebSocket book cache, candidate fan-out, user WebSocket reconciliation, and HKO burst/backoff hardening are implemented as local/tested scaffolding. Remaining proof points are live network smoke, real-auth/manual-money smoke, kill-switch verification against the real account, and production p50/p95/p99 latency evidence.
- Polymarket market discovery validates resolution text against the expected HKO Daily Extract `Absolute Daily Max (deg. C)` wording. Date changes in the first sentence are allowed. Any missing/changed resolution logic prints `🚨🚨🚨 RESOLUTION RULES WARNING ... 🚨🚨🚨` and persists a critical `risk_events` row.
- Forecast-change and actual-cross trading events are keyed and processed once. Repeated scheduler ticks no longer create duplicate missed buys for the same HKO event; duplicate open-position attempts are logged as ignored rather than missed.
- Forecast-change entries use the decimal effective OCF max and only fire when that max crosses into a different integer market bucket. Same-bucket decimal wiggles are recorded but do not create entries.
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
- Expiry means no new entry from that HKO event; scheduler-managed positions still use forecast invalidation, same-date actual invalidation, hold-to-maturity, or risk rules. Manual paper commands still support take-profit and max-hold checks.

Missed trade definitions:

- `buy_missed`: price already moved, no executable depth, below fee threshold, spread/depth guard failed, risk cap rejected, duplicate signal rejected, or stale data guard fired.
- `sell_missed`: exit condition met but no executable bid/depth, below fee threshold, stale orderbook, or risk/safety guard blocked execution.

Alerts:

- Terminal/log-only first.
- Severity levels: info, trade, warning, critical.
- Repeated identical warnings should be throttled.

Dashboard:

- Terminal command `dashboard` prints the current paper summary backed by SQLite.
- Browser command `dashboard-serve` starts the local web UI at `http://127.0.0.1:8765/`.
- Paper route `/` shows D+0/D+1/D+2 forecast panels, precise decimal bot signals, AWS GIS/OCF station forecast highs for covered horizons, D+0 AWS GIS actual readings, D+0 actual-minus-forecast hover values, since-midnight max/min, current HKO temperature, YES/NO token price series, visible signal bubbles, filled trade markers, and paper PnL.
- Paper UI controls include manual refresh, YES/NO token-side selector, auto-refresh every 15 seconds, legend toggles, delayed crosshair tooltips, and modifier-wheel/touch chart zoom. Times render in HKT as `YYYY-MM-DD HH:MM:SS`, and chart x-axes are scoped to the selected HKT date starting at midnight.
- Open positions, realized PnL, and unrealized PnL summary tiles are clickable and replace charts with the relevant paper trade/activity table. Realized PnL tables show close/sell events, while unrealized PnL uses current executable bids for open positions.
- Paper API routes are `/api/stats`, `/api/forecast-panels?side=YES|NO`, `/api/pnl`, and legacy `/api/forecast-vs-actual`.
- Live route `/live` shows live open positions, open exposure, realized PnL, live order counts by status, kill-switch settings, and recent live orders.
- Live API route is `/api/live/stats`; it refreshes every 5 seconds and reads live tables/settings only.
- The dashboard still tracks unique HKO forecasts, latest since-midnight max, current OCF forecast max by day, discovered markets/outcomes, latest bid/ask, buys/sells placed, buys/sells missed, open positions, realized PnL, executable unrealized PnL, total profit, worst-case open loss, source freshness, decision counters, last scheduler run, and recent errors where available.

## OCF Forecast Source Update - May 4, 2026

Discovery:

- The rendered OCF page is `https://maps.weather.gov.hk/ocf/text_e.html?mode=0&station=HKO`.
- Its JavaScript fetches station data from `https://www.hko.gov.hk/wxinfo/awsgis/forecast/HKO.xml`, falling back to the older OCF URL if needed.
- The station feed returned `LastModified: 20260504131147`, `StationCode: HKO`, `DailyForecast`, and `HourlyWeatherForecast` during the smoke test.
- The daily max/min table display can be reproduced from `DailyForecast[].ForecastMaximumTemperature` and `ForecastMinimumTemperature`; for example, raw `27.1` becomes displayed high `27`.
- The sampler stores raw decimal daily values and hourly table rows in `ocf_forecast_samples`, while `hko_forecasts` stores the displayed integer forecast high for display/audit.
- Trading decisions now use a decimal effective OCF max: latest hourly-path max first, raw decimal daily max second. If no decimal OCF max is available, trading skips instead of falling back to the rounded/display daily max.
- Position invalidation therefore does not keep a `25°C YES` position alive just because the rounded daily max is `25` if the hourly max is only `24.7`.
- The sampler stores response headers in `raw_snapshots`. In the smoke test, HTTP `Last-Modified: Mon, 04 May 2026 05:12:19 GMT` produced learned minute `13:12` HKT, and payload `LastModified: 20260504131147` produced learned minute `13:11` HKT.

Commands:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 sample-ocf --interval-minutes 10 --hours 24
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 sample-ocf --ticks 1 --interval-minutes 0
```

Verification:

- Parser/storage tests cover OCF daily max/min parsing and hourly temperature sample persistence.
- Runner tests cover hourly-forecast invalidation of existing positions.
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

May 8 update:

- Lowered the forecast-change executable entry cap for D+0/D+1 from `0.70` to `0.40` after local historical orderbook review found no D+1/D+2 forecast bucket asks reaching `0.70`.
- D+2 or later remains capped at `0.20`.
- Added D+1 regression coverage for rejecting a `0.41` entry and allowing a `0.40` entry.

Rule:

- Forecast-change latency entries now require both:
  - directional YES-ask movement with the event is `<= 0.20`, and
  - executable entry ask for the token being bought is `<= 0.40` for D+0/D+1, or `<= 0.20` for D+2 or later.
- This is intended to avoid buying after the market has already repriced, e.g. `23°C YES` at `0.93` after a downside forecast update.
- Actual-cross new-bucket YES trades use ask movement `< 0.10` and an entry cap of `0.70`, except the high-market peak-hour sure-bet path may buy up to `0.80`.
- Actual-cross invalidated-bucket trades buy the now-settled side up to `0.99` without requiring stale-price movement lag.
- Forecast-value, forecast-change, and forecast-based exits now skip when the latest OCF sample for the target date is at least `90` minutes old.

Implementation:

- Added `Settings.forecast_change_max_price_move = 0.20`.
- Added `Settings.forecast_change_max_entry_price = 0.40`.
- Added `Settings.forecast_change_d2_max_entry_price = 0.20`.
- Forecast-change candidate generation skips outcomes whose directional move exceeds `0.20`.
- Forecast-change order execution only sweeps ask depth at or below the lead-time-specific cap.

Verification:

- Added regression tests for:
  - skipping a forecast-change trade after a `0.21` directional move,
  - allowing a `0.20` directional move when entry is still below the cap,
  - rejecting a D+2 forecast-change entry above `0.20`,
  - allowing a D+2 forecast-change entry at `0.20`,
  - rejecting a near-repriced `23°C YES` at `0.93`.
- Full test suite: `PYTHONPATH=src python3 -m unittest discover -s tests` -> `Ran 115 tests ... OK`.

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

## Backtesting Harness - May 6, 2026

Purpose:

- Replays stored HKO forecast rows, OCF hourly samples, current observations, and orderbook snapshots into a scratch SQLite DB.
- Runs the same paper tick path as the scheduler so policy changes can be evaluated against historical source/orderbook timing.
- Keeps the production-like `data/whenitrains.sqlite3` read-only during replay.

CLI:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 backtest-day 2026-05-06
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 backtest-day 2026-05-06 --json
```

Options:

- `--replay-db PATH`: override the scratch DB path.
- `--tick-source scheduler|data|both`: choose historical paper-decision timestamps, stored data fetch timestamps, or both.
- `--include-orderbook-ticks`: include orderbook snapshot times for denser data-driven replays.
- `--max-ticks N`: cap replay length for smoke tests.
- `--json`: emit machine-readable orders, positions, and active tick summaries.

Implementation notes:

- Uses SQLite's online backup API to copy the source DB safely before replay.
- Clears replay and paper tables in the scratch DB only.
- Re-ingests rows as-of each tick and stamps newly generated paper rows to the historical tick time.
- Adds replay-local indexes for orderbooks, forecasts, OCF samples, observations, paper decisions, and positions.

Verification:

- `PYTHONPATH=src python3 -m unittest tests.test_backtest` covers replaying a forecast-change entry against as-of orderbooks.
- Full test suite: `PYTHONPATH=src python3 -m unittest discover -s tests` -> `Ran 115 tests ... OK`.

## Future Research Queue: Probabilistic Error Model - May 6, 2026

Goal:

- Move beyond pure latency trading into a calibrated uncertainty model around HKO max-temperature forecasts.
- The model should estimate whether the HKO point estimate is a narrow-error "sure bet" or a wide-error "coinflip" conditional on live weather regime, forecast age, season, and market threshold distance.
- This model should eventually feed the trading bot as a second decision layer, while preserving the current latency strategy as a separate signal family.

Variable sense-check:

- Nowcast cloud cover and rainfall vs forecast:
  - High priority. This is probably the strongest non-price live alpha source.
  - Key feature is not just "rain/cloud now", but mismatch vs forecast near the remaining heating window.
  - Needs radar/satellite/cloud product ingestion plus OCF/hourly forecast alignment.
- Actual vs forecast hourly trends intraday:
  - High priority and already partially prepared by `research-hourly-accuracy`.
  - Use signed error at `h+1`, `h+2`, rolling intraday bias, and slope of actual temperature vs OCF expected path.
- Seasonality bias:
  - High priority as a baseline calibration feature.
  - Use week-of-year sin/cos, month, monsoon season flags, and possibly separate models by season/regime if sample size permits.
- Dewpoint trajectory:
  - Good candidate, but frame it as wet-bulb / humidity / dewpoint depression rather than "dewpoint approaches forecast max" literally.
  - Dewpoint near air temperature implies saturation/cloud/rain potential; dewpoint itself does not cap dry-bulb max at the dewpoint.
  - Useful features: dewpoint depression, RH trend, wet-bulb temperature, dewpoint anomaly vs recent hours.
- Wind direction shifts vs forecast:
  - High priority for coastal Hong Kong.
  - Need observed station wind direction/speed and forecast wind text/vector classification.
  - Features should distinguish onshore/offshore/northeasterly/sea-breeze regimes and timing mismatch.
- Frontal timing error:
  - High value but more complex.
  - Start with proxy flags from HKO text/radar/rainband/wind-shift observations before building upstream-station front tracking.
- Sea surface temperature anomaly:
  - Medium priority for coastal/onshore regimes.
  - Slow-moving feature, likely useful as interaction with wind direction rather than standalone daily signal.
- Recent forecast revision velocity:
  - High priority and easy from existing OCF snapshots.
  - Features: last revision direction, size over 6/12/24/48h, number of revisions, and whether revision is accelerating.
  - Hypothesis: forecasters/models under-correct in dynamic regimes; test before trading.
- Time-of-max-temperature distribution:
  - High priority for D+0 trading.
  - Features: local historical peak-time distribution by season/regime, current hour, remaining insolation window, and whether current actual has already exceeded forecast.
- 850mb temperature advection:
  - Good meteorological feature, but heavier lift operationally.
  - Start later unless a reliable free/low-latency gridded source is selected. It may matter most for non-HK expansion and strong advection/front days.

Variables to add:

- Lead time and forecast age:
  - Same-day 10:11 forecast vs stale overnight forecast should not share one error distribution.
- Solar radiation / insolation proxy:
  - Cloud/rain matters because it changes radiation. If direct solar observations are available, use them.
- Warning/regime flags:
  - Thunderstorm, rainstorm, tropical cyclone, monsoon, cold front, hot weather warnings.
- Inter-source spread:
  - Later add AccuWeather/Weather.com/other models as disagreement features once collection is stable.
- Market microstructure features:
  - Price age, orderbook depth, spread, recent price move with/against signal. These should affect execution confidence, not weather probability.
- Threshold distance:
  - Always include distance from forecast distribution to each market bucket/threshold. Alpha is highest when the threshold lies within roughly one forecast-error standard deviation.

Variables to defer or treat carefully:

- Long-shot bucket drift:
  - Do not trade far-away buckets just because the forecast moved slightly. Keep relevance tied to buckets near the forecast or held positions.
- 850mb advection and upstream fronts:
  - Valuable, but defer until core HKO/radar/hourly actuals pipeline is stable.
- SST:
  - Include later as slow regime context, not a primary intraday trigger.

Modelling strategy:

1. Build a research dataset with one row per forecast snapshot and target bucket:
   - `target_date_hkt`, `snapshot_time_hkt`, `lead_hours`, `forecast_max_c`, `forecast_age_minutes`.
   - Bucket metadata: label, side, predicate, distance from forecast to bucket boundary/value.
   - Weather features available at snapshot time only.
   - Market features available at snapshot time only.
   - Outcomes: final bucket hit, final actual max, forecast error, and per-bucket resolution.
2. Start with calibration, not complex ML:
   - Baseline empirical error distribution by lead time and season.
   - Add revision velocity and intraday trend features.
   - Measure calibration curves and Brier/log loss by bucket.
3. Move to probabilistic models:
   - Quantile regression for final max error bands.
   - Logistic models or gradient boosting for per-bucket win probability.
   - Use time-series validation only; never shuffle.
4. Add conformal calibration:
   - Convert model residuals into calibrated intervals by lead-time/regime bucket.
   - Track coverage and interval width separately.
5. Feed into trading as a separate signal layer:
   - Current latency strategy remains event-driven.
   - Model strategy emits probability/edge estimates for eligible buckets.
   - Trade only when model implied probability differs from market price by a configured margin after spread/slippage.
   - Execution should still obey bankroll, max position, drawdown, liquidity, stale-price, and kill-switch controls.
6. Decision arbitration:
   - If latency and model agree, allow larger paper confidence score.
   - If latency says buy but model says market already fair or adverse, reduce size or skip.
   - If model wants a position and later forecast/actual invalidates it, existing forced-exit logic should still dominate.

Initial implementation milestones:

- Extend collection:
  - Continue storing OCF snapshots, current HKO temperature, since-midnight max, orderbooks, paper decisions.
  - Add radar/rain/cloud source discovery before implementation.
  - Add observed humidity/dewpoint/wind extraction if available from HKO current weather APIs.
- Research reports:
  - Hourly actual-vs-forecast error by lead hour.
  - Revision velocity vs final max error.
  - Peak-time distribution by season and weather regime.
  - Same-day bucket hit rate conditional on distance from forecast bucket and current market price.
- Bot integration:
  - Add a model-signal table separate from latency signals.
  - Add paper-only model decisions first.
  - Add dashboard panels for model probability vs market implied probability once paper signals are stable.
