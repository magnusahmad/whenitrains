# HK High Temp Latency Status

Last updated: 2026-05-11 HKT

## Current State

Milestones 1-5 are implemented for local paper trading:

- Milestone 1: Python project skeleton, config, CLI, test setup.
- Milestone 2: HKO ingestion/parser layer for since-midnight CSV, the OCF HKO station forecast feed, and SQLite persistence.
- Milestone 3: Polymarket event/market parsing, CLOB orderbook parsing, and SQLite persistence.
- Milestone 4: Latency signal primitives for directional impact, price-response classification, and trade candidate generation.
- Milestone 5: Paper trader with executable-depth fills, position tracking, risk rejects, and CLI-ready local storage.

Live trading scaffolding is now implemented behind explicit fail-closed gates. Paper trading remains the default. Live mode has additive storage, Keychain hot-key setup, pre-derived credential loading, manual FAK buy/sell/cancel/reconcile commands, kill-switch settings, live tick/scheduler wiring, WebSocket market/user runtime wiring, live dashboard reporting, readiness evidence gates, and durable evidence archiving. Low-latency readiness review now has a concrete roadmap in `docs/low-latency-readiness-roadmap.md`; the remaining readiness gaps are live-environment evidence: live WebSocket smoke, real-auth/manual-money smoke, capped scheduler smoke, real-account kill-switch and settlement validation, production p50/p95/p99 latency, and local-vs-CLOB drift proof.

Low-latency readiness M0/M1 groundwork is now implemented for AWS actual transitions, OCF forecast-sample changes, and market-resolution status changes. The storage layer can write append-only `latency_trace_events`, records orderbook state age on token-level paper decisions, and can enqueue max/min actual transition events immediately after the HKO current-temperature row commit, forecast-sample changed events immediately after OCF sample commits, and `market_resolution_changed` events after rediscovered Polymarket market status changes. Paper and live scheduler loops now create a shared low-latency queue for HKO/market ingestion and drain queued fast events before the normal watchdog tick; scheduler sleep is interrupted by new queue arrivals so background AWS events do not wait for the full sleep interval. Latency stages cover `db_committed`, `event_detected`, `decision_started`, and `decision_completed`; `FastDecisionWorker` provides a blocking queue worker with its own SQLite connection, and fast-event scheduler output includes compact per-event latency lines with event type, key, target date, and commit-to-detect timing. This does not yet complete the full roadmap: production p50/p95/p99 latency evidence from the live DB remains pending.

Low-latency readiness M2 local scaffolding is implemented. `OrderBookCache` can apply Polymarket market-channel `book`, `price_change`, `best_bid_ask`, and `last_trade_price` fixture messages, persist append-only snapshots with WebSocket metadata, reject stale cached books at the configured 250 ms cap, and seed reconnect snapshots. Live buy execution now uses a fresh cache book before falling back to REST `/book`. Active-market YES/NO token listing and subscription-change payload planning are covered so market discovery can drive resubscribe messages without restarting the scheduler. `MarketWebSocketClient` can connect to the Polymarket market channel, send the active-token subscription payload, feed messages into the cache, and reconnect in a loop. `live-scheduler` now starts a scheduler-owned WebSocket runtime by default and passes its shared book cache into live tick and fast-event handlers. `live-network-smoke --live` can start and stop that runtime without trading and reports per-client connection attempts, connected-once state, applied message count, and last error. Live network smoke evidence remains pending.

Low-latency readiness M3 groundwork has standalone execution scheduler, candidate-planner primitives, and an active ladder metadata builder. Candidate actions declare conflict keys such as token, position, or shared risk budget keys; independent actions can run concurrently, while conflicting actions are serialized in deterministic input order. The actual-cross, actual low-cross, forecast-change, forecast-value, forecast-invalidation exit, and watchdog open-position exit hot paths now build planned candidate actions, preserve idempotent candidate keys/conflict keys, and execute them through `ExecutionScheduler` in SQLite-safe single-worker mode. `build_active_ladder_metadata` can precompute target-date token sides, latest book/tick/min-size metadata, held position state, and remaining budget; the actual max/min cross and forecast entry paths now reuse prefiltered highest/lowest outcome rows, and forecast/open-position exit handlers now use batched token-to-outcome row maps instead of per-position outcome lookups. A fake-clock live execution benchmark verifies decision-to-submit tracing under 100 ms excluding network.

Low-latency readiness M4 local scaffolding is implemented. `live_user_events` stores authenticated user-channel order/trade lifecycle events independently from final live position state, and `apply_user_channel_event` can map order lifecycle statuses, apply matched trade deltas to local positions exactly once, and converge a submitted order after a restart when a later user trade event arrives. `UserWebSocketClient` can authenticate to the Polymarket user channel, subscribe by active condition ID, apply incoming order/trade events, reconnect in a loop, and is owned by the live scheduler runtime with a separate SQLite connection. Startup and periodic live reconcile watchdogs now reconcile pending submitted/unknown-fill live orders through REST order/trade lookup, rebuild positions from filled live orders, compare open local live positions against CLOB sellable balances, repair the safe case where local shares exceed CLOB sellable shares by recording a local balance adjustment, and freeze new entries when unresolved drift remains. Resolved/closed past-date markets now settle remaining local paper/live positions at 1.0 or 0.0 once a stored target-date actual identifies the winning side, so missed same-day exit windows no longer leave unresolved local risk indefinitely. Real user-channel smoke, recent-trades validation, and live settlement validation remain pending.

Low-latency readiness M5 local scaffolding is implemented. Learned AWS actual publish windows now include a 10-second pre/post burst plan with 0.5-second cadence, while broader catchup polling remains. Scheduler source backoff can slow non-critical HKO sources without suppressing `aws_actual` polling. HKO raw snapshots now preserve fetch start time, header receipt time, payload receipt time, and response elapsed milliseconds for source-timing audits. `hko-source-timing-report` summarizes the persisted timing evidence for live dry-run review. Live hot-path buys now fail closed when a configured Polymarket WebSocket orderbook cache is missing or stale instead of silently falling back to REST.

Low-latency readiness M6 local scaffolding is implemented with a DB-specific exclusive live scheduler lock, stale submitted-order watchdog, structured startup-health evaluator, health-failure entry freeze, local/CLOB drift startup and periodic scans, persistent kill-switch exit enforcement, webhook alert transport, trade alerts, source-freshness breach alerts, stalled-WebSocket freezes, live runbook, generated readiness checklist, and durable evidence archive command. `live-scheduler` now fails closed if another process already holds the lock for the same SQLite database, and it freezes new entries when previously submitted live orders are older than the configured watchdog threshold. Startup health can now report disconnected market/user WebSockets, missing REST fallback, invalid credentials, insufficient balance/allowance, stale submitted orders, and local/CLOB drift as explicit fail-closed reasons; health failures can set `block_new_entries`, write a critical risk event, and emit an alert through `WHENITRAINS_ALERT_WEBHOOK_URL` when configured. When `cancel_open_orders_and_exit_positions` is enabled, live tick/scheduler startup and the live reconcile watchdog cancel all CLOB orders and attempt live exits for every locally open live position using the latest stored bid book. Filled live scheduler ticks emit trade alerts through the same sink, warmed-up scheduler loops emit critical alerts if required data freshness fails and decisions are skipped, and the live reconcile watchdog freezes entries if either scheduler-owned WebSocket worker is no longer alive. Manual live-auth, minimum-size order, capped scheduler, real-account kill-switch validation, and live settlement validation remain pending.

Latency reporting can now summarize trace rows directly from the database. `latency-report <start_stage> <end_stage>` prints count plus p50/p95/p99 nearest-rank durations, while `low-latency-readiness-report` combines the core latency pairs, live CLOB drift-scan timing, explicit evidence gates, live order/position counters, kill-switch state, and HKO source-timing evidence. `low-latency-archive-evidence --output-dir ... --require-evidence` writes the latency reports, HKO source timing report, readiness output, SHA-256 checksums, and a manifest for production evidence capture; `low-latency-verify-evidence-archive --input-dir ...` verifies the manifest identity header, required archive metadata keys and value formats, exactly one ordered `files:` and `checksums:` section, unique metadata/gate keys, an exact required manifest file list scoped to the `files:` section, non-blank report files with expected report headers/gate lines, latency p50/p95/p99 fields, and HKO public-availability offsets, passing archived readiness gate statuses, exact unique SHA-256 checksum entries scoped to the `checksums:` section, checksum digest format, checksum targets, checksum matches, exact `all_gates_passed=True`, and non-contradictory well-formed `missing_gates` without opening the trading database. Event-keyed live buy/sell execution records `order_submitted`, `clob_ack`, `fill_matched`, `fill_confirmed`, and terminal `order_rejected` stages, allowing live checks for HKO commit-to-decision and submit/fill-or-reject timing.

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

2026-05-11 readiness evidence gate pass: `low-latency-readiness-report` now emits explicit evidence gates for HKO commit-to-decision under 1 second, observed decision-to-submit traces, observed submit-to-fill traces, and observed HKO source timing rows. `--require-evidence` exits `2` when any measurable local evidence gate is missing, so a capped live validation run can fail closed instead of relying only on manual review of raw percentile lines. Verified with `PYTHONPATH=src python3 -m unittest tests.test_latency_report`.

2026-05-11 network smoke fail-closed pass: `live-network-smoke --live --require-connected` now exits `2` unless the scheduler-owned WebSocket runtime is alive, at least two market/user clients are reported, and every reported client has connected at least once. The runbook and readiness audit now use this stricter no-trade smoke command for live network evidence. Verification covers the previously successful no-trade smoke path, the failed connected-once gate, and the missing-client-count gate.

2026-05-11 WebSocket orderbook evidence gate pass: `low-latency-readiness-report --require-evidence` now requires at least one persisted orderbook snapshot whose metadata source is `polymarket_market_websocket`, so production readiness cannot pass from WebSocket thread liveness alone without market-channel book data. Verified with `PYTHONPATH=src python3 -m unittest tests.test_latency_report`.

2026-05-11 orderbook-age evidence gate pass: `low-latency-readiness-report` now summarizes recorded `orderbook_state_age_seconds` decision details and includes an `orderbook_age_under_cap` gate using `Settings.live_orderbook_cache_max_age_seconds`. `--require-evidence` now fails if no book-age rows are present or the observed p95 exceeds the live cache cap, so capped live evidence must include fresh book state. Verified with `PYTHONPATH=src python3 -m unittest tests.test_latency_report`.

2026-05-11 live money-state evidence gate pass: `low-latency-readiness-report --require-evidence` now fails when live state still has unresolved submitted/unknown-fill/open/pending orders, terminal problem error/rejected/blocked/failed orders, or open positions without a latest bid. This turns the operational readiness requirement for unambiguous money state into an objective report gate. Verified with `PYTHONPATH=src python3 -m unittest tests.test_latency_report`.

2026-05-11 user-channel evidence gate pass: `low-latency-readiness-report --require-evidence` now requires at least one stored `live_user_events` row, so a production readiness report cannot pass without evidence that the authenticated user-channel/recent order lifecycle path observed live account events. Verified with `PYTHONPATH=src python3 -m unittest tests.test_latency_report`.

2026-05-11 CLOB lifecycle evidence gate pass: `low-latency-readiness-report --require-evidence` now requires observed `order_submitted -> clob_ack` and `order_submitted -> fill_matched` stage pairs in addition to submit-to-fill-confirmed timing, so the production report proves the intermediate CLOB lifecycle stages named by the roadmap. Verified with `PYTHONPATH=src python3 -m unittest tests.test_latency_report`.

2026-05-11 decision-completion evidence gate pass: `low-latency-readiness-report --require-evidence` now requires `db_committed -> decision_completed` p95 to stay under 1 second, so production evidence must prove fast decisions finish rather than only start quickly. Verified with `PYTHONPATH=src python3 -m unittest tests.test_latency_report`.

2026-05-11 kill-switch evidence gate pass: `low-latency-readiness-report --require-evidence` now fails while `block_new_entries` or `cancel_open_orders_and_exit_positions` is enabled, so readiness evidence cannot pass with an emergency kill-switch state still active. Verified with `PYTHONPATH=src python3 -m unittest tests.test_latency_report`.

2026-05-11 HKO public-availability cluster gate pass: `hko-source-timing-report` now includes signed fetch-to-HTTP-`Last-Modified` offsets, and `low-latency-readiness-report --require-evidence` now requires at least two HKO fetch attempts within the 20-second public-availability burst window. This makes the M5 dry-run requirement objective in the production report instead of only proving generic HKO timing rows exist. Verified with `PYTHONPATH=src python3 -m unittest tests.test_latency_report tests.test_cli.CliDiscoveryTests.test_hko_source_timing_report_summarizes_aws_fetch_attempts`.

2026-05-11 live log access retry: `curl -L --max-time 10 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live network smoke, production readiness report output, and production latency evidence remain blocked on endpoint availability.

2026-05-11 live auth evidence output pass: `live-auth-smoke --live` now prints the required scheduler balance threshold alongside signer/funder, observed balance, allowance state, and reason, and the low-latency live runbook includes the auth smoke before kill-switch status and scheduler start. Verification covers refusal without `--live` and archived threshold output.

2026-05-11 live readiness checklist pass: added read-only `live-readiness-checklist`, which renders the exact command sequence for live network smoke, auth smoke, kill-switch status, minimum-size manual buy/sell with explicit approval flags, reconcile checks, capped scheduler smoke, latency reports, and `low-latency-readiness-report --require-evidence`. This makes the remaining live evidence run repeatable once the live endpoint is reachable. Verified with `PYTHONPATH=src python3 -m unittest tests.test_cli`.

2026-05-11 live reconcile evidence gate pass: `low-latency-readiness-report --require-evidence` now requires at least one live order with `reconciled_at_utc`, so production readiness cannot pass unless the live-money path has archived REST/order reconciliation evidence after manual or capped live testing. Verified with `PYTHONPATH=src python3 -m unittest tests.test_latency_report`.

2026-05-11 user trade evidence gate pass: `low-latency-readiness-report --require-evidence` now requires a stored user-channel `trade` event with `applied_position_delta = 1`, so order lifecycle messages alone cannot satisfy M4 matched-trade evidence. Verified with `PYTHONPATH=src python3 -m unittest tests.test_latency_report`.

2026-05-11 checklist kill-switch/settlement pass: `live-readiness-checklist` now includes the persistent kill-switch verification sequence and an explicit live settlement validation reminder for the first resolved live market, so the generated evidence plan covers the remaining M6 proof points as well as network/auth/manual-money/capped-scheduler evidence. Verified with `PYTHONPATH=src python3 -m unittest tests.test_cli.CliDiscoveryTests.test_live_readiness_checklist_prints_ordered_evidence_commands`.

2026-05-11 submit-to-reject latency pass: submitted live orders that reconcile to a non-fill terminal status now record an `order_rejected` latency stage, and `low-latency-readiness-report` prints `order_submitted -> order_rejected` percentiles plus optional submit-to-reject evidence. This lets production evidence answer the roadmap's submit-to-fill/reject timing requirement instead of fill-only timing. Verified with `PYTHONPATH=src python3 -m unittest tests.test_live.LiveTests.test_execute_live_buy_records_rejected_terminal_latency_stage tests.test_latency_report`.

2026-05-11 live settlement evidence gate pass: `low-latency-readiness-report --require-evidence` now requires a filled live `SETTLEMENT`/`market_resolution` order row, so readiness cannot pass until a resolved-market live settlement has been observed and archived for validation against CLOB/onchain truth. Verified with `PYTHONPATH=src python3 -m unittest tests.test_latency_report`.

2026-05-11 live CLOB drift scan evidence gate pass: the live scheduler now records `live_clob_drift_scan_clear` risk events when startup/watchdog local-vs-CLOB sellable-balance scans return clean, and `low-latency-readiness-report --require-evidence` requires that clear scan evidence. This makes local-vs-CLOB drift proof explicit instead of inferred from the absence of unresolved/problem orders. Verified with `PYTHONPATH=src python3 -m unittest tests.test_latency_report tests.test_cli.CliDiscoveryTests.test_live_scheduler_reconciles_pending_orders_before_watchdog_drift_scan`.

2026-05-11 live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` timed out after 8003 ms with `curl: (28) Connection timed out after 8003 milliseconds`, so live scheduler log capture remains blocked on endpoint availability.

2026-05-11 latest live drift scan gate pass: the CLOB drift readiness gate now evaluates the latest `live_clob_drift_scan_clear`/`live_clob_drift_scan_drift` event instead of accepting any historical clear scan, so a later local-vs-CLOB drift scan blocks `low-latency-readiness-report --require-evidence` until a new clean scan is recorded. Verified with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_readiness_report_fails_when_latest_drift_scan_has_drift`.

2026-05-11 live auth smoke evidence gate pass: `live-auth-smoke --live` now records `live_auth_smoke_ok` or `live_auth_smoke_failed` risk events with signer/funder, required balance, observed balance, allowance state, and reason. `low-latency-readiness-report --require-evidence` now requires the latest live auth smoke event to be OK, so stale credential evidence cannot satisfy readiness after a later failed auth/balance/allowance check. Verified with `PYTHONPATH=src python3 -m unittest tests.test_cli.CliDiscoveryTests.test_live_auth_smoke_prints_required_balance_threshold tests.test_latency_report.LatencyReportTests.test_low_latency_readiness_report_fails_when_latest_auth_smoke_failed`.

2026-05-11 live network smoke evidence gate pass: `live-network-smoke --live --require-connected` now records `live_network_smoke_ok` or `live_network_smoke_failed` risk events with runtime liveness, connected-once status, client count, required client count, per-client attempts/messages/errors, and any command error. `low-latency-readiness-report --require-evidence` now requires the latest live network smoke event to be OK. Verified with `PYTHONPATH=src python3 -m unittest tests.test_cli.CliDiscoveryTests.test_live_network_smoke_starts_and_stops_websocket_runtime_without_trading tests.test_cli.CliDiscoveryTests.test_live_network_smoke_require_connected_fails_when_client_never_connected tests.test_latency_report.LatencyReportTests.test_low_latency_readiness_report_fails_when_latest_network_smoke_failed`.

2026-05-11 manual live order evidence gate pass: `low-latency-readiness-report --require-evidence` now requires filled `manual_live` BUY and SELL order rows, so the explicit minimum-size manual buy/sell smoke cannot be satisfied by scheduler-generated live fills. Verified with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_readiness_report_fails_without_manual_live_sell`.

2026-05-11 capped live scheduler evidence gate pass: capped `live-scheduler --live --ticks N` runs now record `live_scheduler_smoke_ok` on normal loop completion and `live_scheduler_smoke_failed` if the loop raises. `low-latency-readiness-report --require-evidence` now requires the latest scheduler smoke event to be OK. Verified with `PYTHONPATH=src python3 -m unittest tests.test_cli.CliDiscoveryTests.test_live_scheduler_starts_websocket_runtime_and_passes_book_cache_to_ticks tests.test_latency_report.LatencyReportTests.test_low_latency_readiness_report_fails_when_latest_scheduler_smoke_failed`.

2026-05-11 kill-switch verification evidence gate pass: `live-kill-switch --block-new-entries` and `--allow-new-entries` now record `live_kill_switch_blocked`/`live_kill_switch_allowed` risk events, and `low-latency-readiness-report --require-evidence` now requires the latest persistent kill-switch verification event to be allowed/clear. Verified with `PYTHONPATH=src python3 -m unittest tests.test_cli.CliDiscoveryTests.test_live_kill_switch_records_block_and_allow_verification tests.test_latency_report.LatencyReportTests.test_low_latency_readiness_report_fails_when_latest_kill_switch_verification_blocked`.

2026-05-11 live settlement validation evidence gate pass: added `live-settlement-validate --live --order-id ... --reference ...`, which records `live_settlement_validation_ok` evidence for a filled live settlement row with the supplied CLOB/onchain reference. `low-latency-readiness-report --require-evidence` now requires this validation evidence in addition to the settlement row itself. Verified with `PYTHONPATH=src python3 -m unittest tests.test_cli.CliDiscoveryTests.test_live_settlement_validate_records_validation_evidence tests.test_latency_report.LatencyReportTests.test_low_latency_readiness_report_fails_without_settlement_validation`.

2026-05-11 live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` timed out after 8003 ms with `curl: (28) Connection timed out after 8003 milliseconds`, so live scheduler log capture, live WebSocket/auth/manual-money evidence, and production readiness artifacts remain blocked on endpoint availability.

2026-05-11 recorded fixture integration pass: added recorded HKO AWS GIS, Gamma event, and CLOB book fixtures plus `tests.test_recorded_fixtures`, covering parser-to-storage integration for the low-latency data sources without live network access. Verified with `PYTHONPATH=src python3 -m unittest tests.test_recorded_fixtures`.

2026-05-11 evidence archive command pass: added `low-latency-archive-evidence --output-dir ... --require-evidence`, which writes latency reports, HKO source timing, readiness output, and a manifest into a durable evidence directory. The command writes artifacts before returning `2` when readiness gates are missing. Verified with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_archive_evidence_writes_reports tests.test_latency_report.LatencyReportTests.test_low_latency_archive_evidence_require_evidence_returns_missing_status_after_writing`.

2026-05-11 post-archive live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` timed out after 8003 ms with `curl: (28) Connection timed out after 8003 milliseconds`, so the archive command is ready but production evidence capture still cannot proceed from this development machine until the LAN log endpoint is reachable.

2026-05-11 current production-like archive check: ran `low-latency-archive-evidence --output-dir /private/tmp/whenitrains-low-latency-current-evidence --require-evidence` against `data/whenitrains.sqlite3`. It wrote the manifest and report files, then exited `2` as expected. Current DB evidence has `hko_source_timing_observed=pass` and `hko_public_availability_cluster_observed=pass`, but latency traces are still absent (`db_committed -> decision_started count=0`) and all live WebSocket/user/reconcile/settlement/auth/network/scheduler/manual-money gates are missing. This confirms the remaining work is live evidence capture, not local archive/report plumbing.

2026-05-11 evidence archive verifier pass: added `low-latency-verify-evidence-archive --input-dir ...`, which checks the manifest, required report files, required SHA-256 checksum entries, checksum targets, checksum matches, and `all_gates_passed=True`, while surfacing archived `missing_gates` when evidence is incomplete. The verifier now runs before opening any SQLite DB, so checking an archive cannot create or mutate a database path. Verified with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_passes_complete_manifest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_does_not_touch_database tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_missing_gates tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_missing_checksums tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_checksum_mismatch tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_missing_checksum_file`.

2026-05-11 checksum archive snapshot: regenerated `/private/tmp/whenitrains-low-latency-current-evidence` with `low-latency-archive-evidence --require-evidence`; the manifest now includes SHA-256 entries for every report. `low-latency-verify-evidence-archive --input-dir /private/tmp/whenitrains-low-latency-current-evidence` returned `2` only because the archived `missing_gates` list still contains the live latency, WebSocket, user-channel, reconcile, settlement, drift-scan, auth, network, scheduler, kill-switch, and manual-money evidence gates.

2026-05-11 post-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` timed out after 8010 ms with `curl: (28) Connection timed out after 8010 milliseconds`. The checksum archive tooling and verifier are ready, but live/account evidence capture remains blocked until the LAN log endpoint is reachable.

2026-05-11 CLI SQLite lifecycle cleanup pass: added a regression test proving `latency-report` closes the CLI-owned SQLite connection on early return, wrapped connected CLI command dispatch in `try/finally`, and closed direct test fixture connections flagged by tracemalloc. Verified with `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_latency_report tests.test_cli`, which now passes without SQLite `ResourceWarning` output.

2026-05-11 post-cleanup live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 1 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 combined roadmap verification cleanup pass: added per-test SQLite cleanup hooks for runner, live, low-latency, storage, and orderbook-cache fixture suites, and closed a latency-report setup connection before invoking the CLI. The combined roadmap verification now passes as one process under tracemalloc: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` ran 286 tests without the previous unclosed-SQLite descriptor cascade.

2026-05-11 checklist submit-to-reject evidence pass: tightened `live-readiness-checklist` so the production evidence plan includes `latency-report order_submitted order_rejected` alongside submit-to-fill timing, matching the roadmap requirement to capture submit-to-fill/reject latency. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_cli.CliDiscoveryTests.test_live_readiness_checklist_prints_ordered_evidence_commands`.

2026-05-11 checklist CLOB lifecycle latency pass: tightened `live-readiness-checklist` again so the production evidence plan explicitly captures `latency-report order_submitted clob_ack` and `latency-report order_submitted fill_matched`, matching the readiness gates for intermediate CLOB lifecycle timing instead of relying on the readiness report alone. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_cli.CliDiscoveryTests.test_live_readiness_checklist_prints_ordered_evidence_commands`.

2026-05-11 post-checklist live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live network/auth/manual-money/capped-scheduler/kill-switch/settlement evidence remains blocked on endpoint availability.

2026-05-11 checklist decision-completion latency pass: tightened `live-readiness-checklist` so the production evidence plan explicitly captures `latency-report db_committed decision_completed`, matching the readiness gate that decisions must finish within the configured threshold rather than only start quickly. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_cli.CliDiscoveryTests.test_live_readiness_checklist_prints_ordered_evidence_commands`.

2026-05-11 post-decision-completion-checklist live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 1 ms: Couldn't connect to server`. The remaining roadmap exit criteria still require live/account evidence that cannot be captured from this machine while the endpoint is unreachable.

2026-05-11 checklist HKO timing evidence pass: tightened `live-readiness-checklist` so the production evidence plan explicitly runs `hko-source-timing-report`, matching the M5 live dry-run requirement to capture HKO source timing/public-availability evidence directly rather than relying only on the readiness/archive outputs. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_cli.CliDiscoveryTests.test_live_readiness_checklist_prints_ordered_evidence_commands`.

2026-05-11 post-HKO-checklist live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account proof remains blocked on endpoint availability.

2026-05-11 checklist reconcile evidence pass: tightened `live-readiness-checklist` so each manual live-money reconciliation step explicitly tells the operator to archive `live-reconcile` output as REST/recent-trades validation evidence, matching the M4 account-side validation gap. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_cli.CliDiscoveryTests.test_live_readiness_checklist_prints_ordered_evidence_commands`.

2026-05-11 post-reconcile-checklist live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live WebSocket/auth/manual-money/reconcile/settlement evidence remains blocked on endpoint availability.

2026-05-11 evidence archive non-empty verifier pass: tightened `low-latency-verify-evidence-archive` so required report files must be non-empty even when their SHA-256 checksums match the manifest. This prevents an empty report placeholder from passing final evidence verification. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_empty_report_file`.

2026-05-11 evidence archive non-blank verifier pass: tightened `low-latency-verify-evidence-archive` again so required report files must contain non-whitespace text, not merely bytes, even when their SHA-256 checksums match the manifest. Verified red/green by changing `tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_empty_report_file` to use a whitespace-only report.

2026-05-11 post-blank-archive-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 1 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 evidence archive exact gate verifier pass: tightened `low-latency-verify-evidence-archive` so `all_gates_passed` must parse exactly as `True`; malformed values such as `True-ish` no longer satisfy the final readiness archive gate. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_requires_exact_passed_gate`.

2026-05-11 post-exact-archive-gate live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 1 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 evidence archive duplicate checksum verifier pass: tightened `low-latency-verify-evidence-archive` so duplicate checksum entries for the same report file are rejected instead of silently accepting the last value. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_duplicate_checksum_entry`.

2026-05-11 post-duplicate-checksum-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 1 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 evidence archive duplicate manifest verifier pass: tightened `low-latency-verify-evidence-archive` so duplicate report entries in the manifest `files:` list are rejected instead of being collapsed into a set. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_duplicate_manifest_entry`.

2026-05-11 post-duplicate-manifest-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 1 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 evidence archive duplicate gate-key verifier pass: tightened `low-latency-verify-evidence-archive` so duplicate readiness gate keys such as `all_gates_passed` are rejected instead of accepting the first value in an ambiguous manifest. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_duplicate_gate_status`.

2026-05-11 post-duplicate-gate-key-verifier verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 291 tests.

2026-05-11 post-duplicate-gate-key-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 evidence archive checksum digest verifier pass: tightened `low-latency-verify-evidence-archive` so checksum entries for existing files must contain 64-character lowercase SHA-256 hex digests instead of falling through to a generic mismatch. Missing checksum targets still report as missing files first. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_malformed_checksum_digest`.

2026-05-11 post-checksum-digest-verifier verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 292 tests.

2026-05-11 post-checksum-digest-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 evidence archive exact manifest verifier pass: tightened `low-latency-verify-evidence-archive` so the manifest `files:` list must contain exactly the expected report files and cannot include ignored extra artifacts. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_unexpected_manifest_entry`.

2026-05-11 post-exact-manifest-verifier verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 293 tests.

2026-05-11 post-exact-manifest-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 roadmap/status consistency pass: refreshed the roadmap live-endpoint blocker, audit verification count, and top-level status summary so the documentation matches the current archive verifier behavior and latest 293-test roadmap verification.

2026-05-11 post-roadmap-status-consistency live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 evidence archive exact checksum verifier pass: tightened `low-latency-verify-evidence-archive` so checksum entries must exactly match the expected report file set and cannot include ignored extra checksum targets. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_unexpected_checksum_entry`.

2026-05-11 post-exact-checksum-verifier verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 294 tests.

2026-05-11 post-exact-checksum-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 evidence archive report-shape verifier pass: tightened `low-latency-verify-evidence-archive` so required reports must contain the expected latency, HKO timing, and readiness report headers rather than only non-blank text with matching checksums. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_malformed_report_content`.

2026-05-11 post-report-shape-verifier verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 295 tests.

2026-05-11 post-report-shape-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 evidence archive manifest-header verifier pass: tightened `low-latency-verify-evidence-archive` so the manifest must begin with the expected `low latency evidence archive` identity header before the remaining fields can satisfy verification. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_missing_manifest_header`.

2026-05-11 post-manifest-header-verifier verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 296 tests.

2026-05-11 post-manifest-header-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 evidence archive manifest-metadata verifier pass: tightened `low-latency-verify-evidence-archive` so the manifest must include the run metadata keys emitted by `low-latency-archive-evidence`: `created_at_utc`, `db_path`, `hko_endpoint_contains`, and `hko_limit`. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_missing_manifest_metadata`.

2026-05-11 post-manifest-metadata-verifier verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 297 tests.

2026-05-11 post-manifest-metadata-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 1 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 evidence archive manifest-metadata-value verifier pass: tightened `low-latency-verify-evidence-archive` so manifest metadata must be well-formed: `created_at_utc` parses as ISO datetime, `db_path` is non-blank, and `hko_limit` is a positive integer. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_invalid_manifest_metadata`.

2026-05-11 post-manifest-metadata-value-verifier verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 298 tests.

2026-05-11 post-manifest-metadata-value-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 evidence archive duplicate metadata verifier pass: tightened `low-latency-verify-evidence-archive` so duplicate metadata keys such as `created_at_utc` are rejected instead of accepting the first value in an ambiguous manifest. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_duplicate_manifest_metadata`.

2026-05-11 post-duplicate-metadata-verifier verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 299 tests.

2026-05-11 post-duplicate-metadata-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 evidence archive manifest-section verifier pass: tightened `low-latency-verify-evidence-archive` so the manifest must include explicit `files:` and `checksums:` section headers instead of accepting orphan list/checksum lines. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_missing_manifest_sections`.

2026-05-11 post-manifest-section-verifier verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 300 tests.

2026-05-11 post-manifest-section-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 evidence archive file-section verifier pass: tightened `low-latency-verify-evidence-archive` so report file entries only count when they appear inside the manifest `files:` section, bounded before `checksums:`. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_ignores_file_entries_outside_files_section`.

2026-05-11 post-file-section-verifier verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 301 tests.

2026-05-11 post-file-section-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 evidence archive checksum-section verifier pass: tightened `low-latency-verify-evidence-archive` so SHA-256 entries only count when they appear inside the manifest `checksums:` section. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_ignores_checksum_entries_outside_checksums_section`.

2026-05-11 post-checksum-section-verifier verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 302 tests.

2026-05-11 post-checksum-section-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 evidence archive duplicate-section verifier pass: tightened `low-latency-verify-evidence-archive` so duplicate manifest section headers such as repeated `files:` are rejected. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_duplicate_manifest_section`.

2026-05-11 post-duplicate-section-verifier verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 303 tests.

2026-05-11 post-duplicate-section-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 evidence archive section-order verifier pass: tightened `low-latency-verify-evidence-archive` so the manifest reports a direct structural error if `files:` appears after `checksums:`. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_reversed_manifest_sections`.

2026-05-11 post-section-order-verifier verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 304 tests.

2026-05-11 post-section-order-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 evidence archive gate-consistency verifier pass: tightened `low-latency-verify-evidence-archive` so a manifest cannot claim `all_gates_passed=True` while also listing `missing_gates`. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_conflicting_gate_status`.

2026-05-11 post-gate-consistency-verifier verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 305 tests.

2026-05-11 post-gate-consistency-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 evidence archive missing-gates verifier pass: tightened `low-latency-verify-evidence-archive` so `missing_gates` must be a well-formed comma-separated list with no empty entries. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_malformed_missing_gates`.

2026-05-11 post-missing-gates-verifier verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 306 tests.

2026-05-11 post-missing-gates-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 evidence archive readiness-report verifier pass: tightened `low-latency-verify-evidence-archive` so `readiness_report.txt` must include at least one actual `gate ...` evidence line rather than only report headers. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_readiness_report_without_gate_lines`.

2026-05-11 post-readiness-report-verifier verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 307 tests.

2026-05-11 post-readiness-report-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 1 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 evidence archive readiness-gate-status verifier pass: tightened `low-latency-verify-evidence-archive` so archived readiness report gate lines must all have `=pass` status when the archive claims all gates passed. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_readiness_report_missing_gate`.

2026-05-11 post-readiness-gate-status-verifier verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 308 tests.

2026-05-11 post-readiness-gate-status-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 1 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 evidence archive latency-percentile verifier pass: tightened `low-latency-verify-evidence-archive` so latency report files must include p50, p95, and p99 percentile fields, matching the roadmap evidence requirement. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_latency_report_without_p99`.

2026-05-11 post-latency-percentile-verifier verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 309 tests.

2026-05-11 post-latency-percentile-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 1 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 evidence archive HKO-public-offset verifier pass: tightened `low-latency-verify-evidence-archive` so `hko_source_timing_report.txt` must include `public_availability_fetch_offsets_seconds`, matching the M5 public-availability timing evidence requirement. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_hko_report_without_public_offsets`.

2026-05-11 post-HKO-public-offset-verifier verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 310 tests.

2026-05-11 post-HKO-public-offset-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 evidence archive HKO-observed-rows verifier pass: tightened `low-latency-verify-evidence-archive` so `hko_source_timing_report.txt` must report a positive observed row count and a real `public_availability_fetch_offsets_seconds` bucket, rejecting zero-row `none` evidence even when the checksum matches. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_hko_report_without_observed_rows`.

2026-05-11 post-HKO-observed-rows-verifier verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 311 tests.

2026-05-11 post-HKO-observed-rows-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 evidence archive HKO-offset-bucket verifier pass: tightened `low-latency-verify-evidence-archive` so `public_availability_fetch_offsets_seconds` must be a parseable comma-separated `seconds:count` bucket summary with positive counts, rejecting arbitrary non-empty offset text even when the checksum matches. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_malformed_hko_public_offsets`.

2026-05-11 post-HKO-offset-bucket-verifier verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 312 tests.

2026-05-11 post-HKO-offset-bucket-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 evidence archive HKO-response-percentile verifier pass: tightened `low-latency-verify-evidence-archive` so `response_ms` in `hko_source_timing_report.txt` must include numeric non-negative `p50`, `p95`, and `p99` millisecond values instead of accepting placeholder text. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_malformed_hko_response_percentiles`.

2026-05-11 post-HKO-response-percentile-verifier verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 313 tests.

2026-05-11 post-HKO-response-percentile-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 evidence archive latency-sample verifier pass: tightened `low-latency-verify-evidence-archive` so latency report files must have a positive `count` and numeric non-negative `p50`, `p95`, and `p99` second values, rejecting zero-sample placeholder reports even when the checksum matches. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_latency_report_without_samples`.

2026-05-11 post-latency-sample-verifier verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 314 tests.

2026-05-11 post-latency-sample-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 settlement-validation matching gate pass: tightened `low-latency-readiness-report --require-evidence` so `live_settlement_validated` only passes when `live_settlement_validation_ok` references an actual filled settlement order row. Stale validation events for unrelated order IDs no longer satisfy the live settlement proof gate. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_readiness_report_fails_when_settlement_validation_is_stale`.

2026-05-11 post-settlement-validation-matching verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 315 tests. The run still emitted scheduler-test unclosed-SQLite ResourceWarnings, but completed successfully.

2026-05-11 post-settlement-validation-matching live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 settlement-validation reference gate pass: tightened `low-latency-readiness-report --require-evidence` so matching `live_settlement_validation_ok` evidence must include a non-empty external CLOB/onchain reference. A validation row that names the settlement order but omits the reference no longer satisfies `live_settlement_validated`. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_readiness_report_fails_when_settlement_validation_lacks_reference`.

2026-05-11 post-settlement-validation-reference verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 316 tests.

2026-05-11 post-settlement-validation-reference live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 manual-live positive-fill gate pass: tightened `low-latency-readiness-report --require-evidence` so manual live BUY/SELL evidence only counts filled `manual_live` rows with positive fill size or shares. Placeholder filled rows without executed quantity no longer satisfy the minimum-size manual-money smoke gate. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_readiness_report_fails_when_manual_live_fill_has_no_size`.

2026-05-11 post-manual-live-positive-fill verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 317 tests.

2026-05-11 post-manual-live-positive-fill live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 auth-smoke detail gate pass: tightened `low-latency-readiness-report --require-evidence` so `live_auth_smoke_ok` evidence only counts OK events with signer/funder addresses, allowance OK, and observed balance at or above the required threshold. Placeholder auth OK events no longer satisfy the live-auth gate. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_readiness_report_fails_when_auth_smoke_ok_lacks_preflight_details`.

2026-05-11 post-auth-smoke-detail verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 318 tests.

2026-05-11 post-auth-smoke-detail live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 network-smoke detail gate pass: tightened `low-latency-readiness-report --require-evidence` so `live_network_smoke_ok` evidence only counts OK events where all workers were running, all required clients connected at least once, and `client_count >= required_clients >= 2`. One-client placeholder network smoke rows no longer satisfy the live WebSocket smoke gate. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_readiness_report_fails_when_network_smoke_ok_lacks_required_clients`.

2026-05-11 post-network-smoke-detail verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 319 tests.

2026-05-11 post-network-smoke-detail live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 scheduler-smoke detail gate pass: tightened `low-latency-readiness-report --require-evidence` so `live_scheduler_smoke_ok` evidence only counts OK events with positive capped tick count and WebSocket runtime enabled. Zero-tick or no-websocket scheduler placeholders no longer satisfy the capped live scheduler smoke gate. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_readiness_report_fails_when_scheduler_smoke_ok_lacks_ticks`.

2026-05-11 post-scheduler-smoke-detail verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 320 tests.

2026-05-11 post-scheduler-smoke-detail live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 1 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 kill-switch verification detail gate pass: tightened `low-latency-readiness-report --require-evidence` so `live_kill_switch_allowed` evidence only counts events whose stored details explicitly show `block_new_entries=false` and `exit_on_kill_switch=false`. Contradictory allowed rows no longer satisfy the real-account kill-switch verification gate. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_readiness_report_fails_when_kill_switch_allowed_has_blocking_details`.

2026-05-11 post-kill-switch-verification-detail verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 321 tests.

2026-05-11 post-kill-switch-verification-detail live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 1 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 live-reconcile payload gate pass: tightened `low-latency-readiness-report --require-evidence` so `live_reconcile_observed` only counts filled live orders with a CLOB order id and non-empty reconcile payload. Empty reconcile placeholders no longer satisfy the REST/account reconciliation evidence gate. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_readiness_report_require_evidence_fails_with_empty_live_reconcile`.

2026-05-11 post-live-reconcile-payload verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 322 tests.

2026-05-11 post-live-reconcile-payload live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 user-trade detail gate pass: tightened `low-latency-readiness-report --require-evidence` so `user_channel_trade_applied` only counts applied user-channel trade rows with order id, token id, matched/mined/confirmed status, side, and positive price/size. Malformed trade placeholders with only `applied_position_delta=1` no longer satisfy the user WebSocket reconciliation evidence gate. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_readiness_report_require_evidence_fails_with_malformed_user_trade_event`.

2026-05-11 post-user-trade-detail verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 323 tests.

2026-05-11 post-user-trade-detail live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 websocket-orderbook-detail gate pass: tightened `low-latency-readiness-report --require-evidence` so `websocket_orderbook_snapshots_observed` only counts Polymarket WebSocket snapshots with usable bid/ask/mid prices and non-empty bid/ask depth arrays. Source-only placeholder snapshots no longer satisfy the WebSocket book-cache evidence gate. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_readiness_report_fails_on_placeholder_websocket_orderbook`.

2026-05-11 post-websocket-orderbook-detail verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 324 tests.

2026-05-11 post-websocket-orderbook-detail live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 HKO-source-timing detail gate pass: tightened `low-latency-readiness-report --require-evidence` so `hko_source_timing_observed` only counts HKO raw snapshots with explicit `fetch_started_at_utc` and `response_elapsed_ms` timing fields. Untimed raw snapshots no longer satisfy the HKO source-timing evidence gate. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_readiness_report_require_evidence_fails_with_untimed_hko_snapshot`.

2026-05-11 post-HKO-source-timing-detail verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 325 tests.

2026-05-11 post-HKO-source-timing-detail live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 drift-scan detail gate pass: tightened `low-latency-readiness-report --require-evidence` so `live_clob_drift_scan_clear` only counts clear scan events with explicit `drift_count=0`. Placeholder clear events without a drift count no longer satisfy the local-vs-CLOB drift evidence gate. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_readiness_report_fails_when_clear_drift_scan_lacks_zero_count`.

2026-05-11 post-drift-scan-detail verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 326 tests.

2026-05-11 post-drift-scan-detail live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 settlement-observed quantity gate pass: tightened `low-latency-readiness-report --require-evidence` so `live_settlement_observed` only counts filled settlement/market-resolution rows with positive fill price and positive USD size or shares. Placeholder settlement rows without executed quantity no longer satisfy the live settlement observation gate. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_readiness_report_fails_when_settlement_has_no_fill_quantity`.

2026-05-11 post-settlement-observed-quantity verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 327 tests.

2026-05-11 post-settlement-observed-quantity live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 archive readiness-gate completeness pass: tightened `low-latency-verify-evidence-archive` so `readiness_report.txt` must include the full expected readiness gate set, not just one or more passing gate lines. Omitted gates now fail archive verification explicitly. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_readiness_report_omitted_gate`.

2026-05-11 post-archive-readiness-gate-completeness verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 328 tests.

2026-05-11 post-archive-readiness-gate-completeness live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 archive duplicate-readiness-gate verifier pass: tightened `low-latency-verify-evidence-archive` so `readiness_report.txt` rejects duplicate readiness gate lines instead of accepting ambiguous repeated evidence. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_duplicate_readiness_gate`.

2026-05-11 post-archive-duplicate-readiness-gate verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 329 tests.

2026-05-11 post-archive-duplicate-readiness-gate live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 archive unexpected-readiness-gate verifier pass: tightened `low-latency-verify-evidence-archive` so `readiness_report.txt` rejects unexpected readiness gate names instead of accepting unknown extra evidence lines. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_unexpected_readiness_gate`.

2026-05-11 post-archive-unexpected-readiness-gate verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 330 tests.

2026-05-11 post-archive-unexpected-readiness-gate live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 archive readiness-gate section-scope verifier pass: tightened `low-latency-verify-evidence-archive` so readiness gate lines only count inside the `evidence gates:` section before `live:`. Gate lines outside that section no longer satisfy missing/duplicate/unexpected/status checks. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_ignores_gate_lines_outside_evidence_section`.

2026-05-11 post-archive-readiness-gate-section-scope verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 331 tests.

2026-05-11 post-archive-readiness-gate-section-scope live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 archive readiness-section verifier pass: tightened `low-latency-verify-evidence-archive` so `readiness_report.txt` must contain exactly one `evidence gates:` marker before exactly one `live:` marker. Duplicate readiness section markers now make the report malformed instead of being ignored by gate parsing. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_duplicate_readiness_sections`.

2026-05-11 archive readiness-missing-footer verifier pass: tightened `low-latency-verify-evidence-archive` so `readiness_report.txt` cannot contain a `readiness evidence missing:` footer when the archive claims all readiness gates passed. Contradictory archived readiness reports now fail as malformed even when their checksum matches. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_readiness_report_with_missing_footer tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_passes_complete_manifest`.

2026-05-11 live log retry: `curl -L --max-time 8 http://192.168.1.23:8765/` still returns `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`, so live scheduler/account evidence cannot be inspected from this workstation yet.

2026-05-11 archive readiness-latency-section verifier pass: tightened `low-latency-verify-evidence-archive` so `readiness_report.txt` must include exactly one ordered `latency:`, `evidence gates:`, and `live:` section. Archived readiness reports can no longer satisfy gate checks while omitting the latency evidence section entirely. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_readiness_report_without_latency_section tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_duplicate_readiness_sections tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_passes_complete_manifest`.

2026-05-11 post-archive-readiness-latency-section live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 archive readiness-latency-summary verifier pass: tightened `low-latency-verify-evidence-archive` so `readiness_report.txt` must include the complete core readiness latency summary lines with positive counts and numeric p50/p95/p99 values, not just an empty `latency:` marker. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_readiness_report_without_latency_summaries tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_readiness_report_without_latency_section tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_passes_complete_manifest`.

2026-05-11 archive missing-gate-name verifier pass: tightened `low-latency-verify-evidence-archive` so manifest `missing_gates` entries must be non-empty, non-duplicate, and part of the known readiness gate set. Unknown gate names can no longer be surfaced as if they were valid roadmap evidence gaps. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_unknown_missing_gate_name tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_malformed_missing_gates tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_missing_gates`.

2026-05-11 post-archive-missing-gate-name live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 archive missing-gates-consistency verifier pass: tightened `low-latency-verify-evidence-archive` so manifest `missing_gates` must match the non-passing readiness gates archived in `readiness_report.txt`. Incomplete archives can no longer list one valid missing gate while the readiness report shows a different failed gate. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_manifest_report_missing_gate_mismatch tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_missing_gates tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_conflicting_gate_status tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_passes_complete_manifest`.

2026-05-11 post-archive-missing-gates-consistency live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 archive incomplete-readiness-report verifier pass: adjusted `low-latency-verify-evidence-archive` so generated incomplete evidence archives can keep their `readiness evidence missing:` footer and zero/`n/a` readiness latency summaries without making `readiness_report.txt` structurally malformed. Passing archives still reject that footer and still require positive readiness latency summaries. Verified with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_missing_gates tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_readiness_report_with_missing_footer tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_passing_archive_with_zero_readiness_latency tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_passes_complete_manifest`.

2026-05-11 post-archive-incomplete-readiness-report live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 archive endpoint-metadata verifier pass: tightened `low-latency-verify-evidence-archive` so manifest `hko_endpoint_contains` must be nonblank, matching the archive metadata value-format requirement. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_blank_hko_endpoint_metadata tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_invalid_manifest_metadata tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_passes_complete_manifest`.

2026-05-11 post-archive-endpoint-metadata live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 archive timestamp-metadata verifier pass: tightened `low-latency-verify-evidence-archive` so manifest `created_at_utc` must parse as a timezone-aware ISO datetime, not a naive timestamp. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_naive_created_at_metadata tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_invalid_manifest_metadata tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_passes_complete_manifest`.

2026-05-11 post-archive-timestamp-metadata live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 archive UTC-timestamp verifier pass: tightened `low-latency-verify-evidence-archive` so manifest `created_at_utc` must use a zero UTC offset, not merely any timezone-aware ISO timestamp. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_non_utc_created_at_metadata tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_naive_created_at_metadata tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_passes_complete_manifest`.

2026-05-11 post-archive-UTC-timestamp live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 archive checksum-line verifier pass: tightened `low-latency-verify-evidence-archive` so malformed `sha256 ...` lines in the manifest checksum section are rejected instead of ignored. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_malformed_checksum_line tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_malformed_checksum_digest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_passes_complete_manifest`.

2026-05-11 post-archive-checksum-line live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 archive file-line verifier pass: tightened `low-latency-verify-evidence-archive` so malformed non-empty lines inside the manifest `files:` section are rejected instead of ignored. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_malformed_manifest_file_entry tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_unexpected_manifest_entry tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_passes_complete_manifest`.

2026-05-11 post-archive-file-line live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 1 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 archive readiness-gate-line verifier pass: tightened `low-latency-verify-evidence-archive` so malformed `gate ...` lines inside `readiness_report.txt` are rejected explicitly instead of being misclassified as unexpected readiness gate names. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_malformed_readiness_gate_line tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_unexpected_readiness_gate tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_passes_complete_manifest`.

2026-05-11 post-archive-readiness-gate-line live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 1 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 archive duplicate-percentile verifier pass: tightened `low-latency-verify-evidence-archive` so duplicate percentile fields in latency reports and HKO response-millisecond summaries are rejected instead of letting the last value win. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_latency_report_duplicate_percentile tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_duplicate_hko_response_percentile tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_passes_complete_manifest`.

2026-05-11 post-archive-duplicate-percentile live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 1 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 archive readiness-gate-field verifier pass: tightened `low-latency-verify-evidence-archive` so duplicate detail fields inside `readiness_report.txt` gate lines are rejected instead of letting ambiguous values pass. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_duplicate_readiness_gate_field tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_malformed_readiness_gate_line tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_passes_complete_manifest`.

2026-05-11 post-archive-readiness-gate-field live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 archive bare-readiness-gate verifier pass: tightened `low-latency-verify-evidence-archive` so `readiness_report.txt` gate lines must include at least one key/value detail field after the pass/missing status. A bare `gate name=pass` assertion no longer counts as archived readiness evidence. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_bare_passing_readiness_gate tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_duplicate_readiness_gate_field tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_passes_complete_manifest`.

2026-05-11 post-archive-bare-readiness-gate live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 archive readiness-gate-count verifier pass: tightened `low-latency-verify-evidence-archive` so `count=` detail fields inside `readiness_report.txt` gate lines must be nonnegative integers. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_non_numeric_readiness_gate_count tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_bare_passing_readiness_gate tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_passes_complete_manifest`.

2026-05-11 post-archive-readiness-gate-count live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 archive readiness-gate-duration verifier pass: tightened `low-latency-verify-evidence-archive` so second-valued readiness gate fields such as `p95` and `threshold` must parse as nonnegative seconds or `n/a`. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_non_numeric_readiness_gate_duration tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_non_numeric_readiness_gate_count tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_passes_complete_manifest`.

2026-05-11 post-archive-readiness-gate-duration live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 archive readiness-gate-zero-count verifier pass: tightened `low-latency-verify-evidence-archive` so `pass count=0` readiness gates are rejected unless the gate is the optional `submit_to_reject_observed` with `evidence=not_observed`. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_non_optional_pass_gate_with_zero_count tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_passes_complete_manifest tests.test_latency_report.LatencyReportTests.test_low_latency_readiness_report_prints_evidence_gates`.

2026-05-11 post-archive-readiness-gate-zero-count live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 archive readiness-gate-boolean verifier pass: tightened `low-latency-verify-evidence-archive` so boolean readiness gate fields such as `block_new_entries` and `exit_on_kill_switch` must be `True` or `False`. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_malformed_readiness_gate_boolean tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_non_optional_pass_gate_with_zero_count tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_passes_complete_manifest`.

2026-05-11 post-archive-readiness-gate-boolean live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 archive readiness-gate-latest verifier pass: tightened `low-latency-verify-evidence-archive` so `latest=` readiness gate fields must be one of the statuses emitted by the readiness report. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_malformed_readiness_gate_latest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_malformed_readiness_gate_boolean tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_passes_complete_manifest`.

2026-05-11 post-archive-readiness-gate-latest live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 archive readiness-gate-drift-count verifier pass: tightened `low-latency-verify-evidence-archive` so `latest_drift_count=` readiness gate fields must be a nonnegative integer or `n/a`. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_malformed_readiness_gate_drift_count tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_malformed_readiness_gate_latest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_passes_complete_manifest`.

2026-05-11 post-archive-readiness-gate-drift-count live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 1 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 archive live-money-counter verifier pass: tightened `low-latency-verify-evidence-archive` so live money-state readiness counters such as `unresolved_orders`, `problem_orders`, `submitted`, `error`, and `missing_bid_positions` must be nonnegative integers. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_malformed_live_money_state_counter tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_malformed_readiness_gate_drift_count tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_passes_complete_manifest`.

2026-05-11 post-archive-live-money-counter live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 archive optional-evidence verifier pass: tightened `low-latency-verify-evidence-archive` so `evidence=` readiness gate fields must be either `observed` or `not_observed`. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_malformed_optional_gate_evidence tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_passes_complete_manifest tests.test_latency_report.LatencyReportTests.test_low_latency_readiness_report_prints_evidence_gates`.

2026-05-11 post-archive-optional-evidence live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 archive readiness-gate-field-allowlist verifier pass: tightened `low-latency-verify-evidence-archive` so readiness gate lines reject unsupported detail fields instead of accepting arbitrary metadata. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_unknown_readiness_gate_field tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_malformed_optional_gate_evidence tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_passes_complete_manifest`.

2026-05-11 post-archive-readiness-gate-field-allowlist live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 post-archive-readiness-latency-summary live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 post-archive-readiness-section verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 332 tests.

2026-05-11 post-archive-readiness-section live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 post-empty-archive-verifier live log endpoint retry: `curl -L --max-time 8 http://192.168.1.23:8765/` failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 0 ms: Couldn't connect to server`. Live/account evidence remains blocked on endpoint availability.

2026-05-11 archive incomplete-manifest verifier pass: fixed `low-latency-verify-evidence-archive` so generated incomplete evidence bundles do not misclassify the valid manifest gate metadata lines `all_gates_passed=False` and `missing_gates=...` as malformed file entries. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_missing_gates tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_malformed_manifest_file_entry tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_passes_complete_manifest`, and rechecked `/private/tmp/whenitrains-low-latency-archive-smoke` still exits `2` with missing readiness gates and malformed empty source reports but without false malformed manifest metadata errors.

2026-05-11 post-archive-incomplete-manifest verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 357 tests.

2026-05-11 post-archive-incomplete-manifest live log endpoint retry: sandboxed `curl -L --max-time 8 http://192.168.1.23:8765/` timed out after 8 seconds; the approved LAN retry failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 1 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 disposable readiness audit check: initialized `/private/tmp/whenitrains-roadmap-audit.sqlite3` and ran `PYTHONPATH=src python3 -m whenitrains.cli --db /private/tmp/whenitrains-roadmap-audit.sqlite3 low-latency-readiness-report --require-evidence`. The command exited `2` as expected, with no production DB access, and reported the expected missing evidence gates for latency traces, HKO timing/public-availability clustering, WebSocket orderbook snapshots, user-channel trade application, live reconcile/settlement validation, drift scan, auth/network/scheduler smoke, kill-switch verification, and manual live buy/sell. Empty live money state and clear kill-switch sanity gates passed.

2026-05-11 production DB read-only evidence audit: added `low-latency-readiness-db-audit`, a pre-migration read-only command for checking whether a DB has the evidence categories needed before attempting final readiness/archive commands. Verified red/green with empty and seeded disposable DB tests, then ran `PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 low-latency-readiness-db-audit`. The command exited `2` and reported 22,372 HKO `raw_snapshots` rows and 637,810 `orderbook_snapshots` rows, but zero `latency_trace_events`, zero timed HKO raw rows with both `fetch_started_at_utc` and `response_elapsed_ms`, zero WebSocket orderbook snapshots, zero `paper_decisions` rows with `orderbook_state_age_seconds`, zero manual live buy/sell orders, zero reconciled live orders, zero live user events, and zero live network/auth/scheduler/kill-switch/drift/settlement-validation risk-event records. The approved LAN retry for `curl -L --max-time 8 http://192.168.1.23:8765/` still failed with connection refused, so the live evidence exit criteria remain unmet.

2026-05-11 post-readiness-db-audit verification: `PYTHONPATH=src python3 -m unittest tests.test_cli` passed with 30 tests, and `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 359 tests.

2026-05-11 granular readiness DB audit pass: split `low-latency-readiness-db-audit` output and pass criteria so the read-only audit reports manual live buy/sell orders, reconciled live orders, live network/auth/scheduler smoke records, kill-switch verification records, live CLOB drift scan records, and settlement-validation records separately instead of one combined risk-event smoke count. Verified with focused red/green CLI tests, `PYTHONPATH=src python3 -m unittest tests.test_cli`, and the 359-test roadmap suite under tracemalloc.

2026-05-11 old-schema readiness DB audit pass: hardened `low-latency-readiness-db-audit` so it remains safe before migrations on older DB schemas. If an evidence table exists but a newer evidence column is missing, the read-only audit now reports that evidence count as `0` instead of crashing. Verified with a hand-built old-schema SQLite regression, `PYTHONPATH=src python3 -m unittest tests.test_cli` passing 31 tests, and the full roadmap suite passing 360 tests under tracemalloc. The production DB read-only audit still exits `2` with the same missing live evidence categories.

2026-05-11 latency-pair readiness DB audit pass: expanded `low-latency-readiness-db-audit` so it reports concrete latency stage-pair counts for `db_committed -> decision_started`, `db_committed -> decision_completed`, `decision_started -> order_submitted`, `order_submitted -> clob_ack`, `order_submitted -> fill_matched`, and `order_submitted -> fill_confirmed`, plus filled settlement live orders and applied user trade events. The production DB audit still exits `2`: all latency pair counts are `0`, timed HKO rows are `0`, WebSocket orderbook snapshots are `0`, orderbook-age decisions are `0`, live orders/user events are `0`, and all live smoke/verification records are `0`.

2026-05-11 readiness DB audit missing-list pass: `low-latency-readiness-db-audit` now prints `missing_evidence=` with the exact zero-count required evidence keys that make the command exit `2`. Verified with focused CLI tests, `PYTHONPATH=src python3 -m unittest tests.test_cli` passing 31 tests, and the full 360-test roadmap suite under tracemalloc. Running the command on `data/whenitrains.sqlite3` still exits `2` and lists all required latency-pair, timed-HKO, WebSocket orderbook, orderbook-age, live-order, live-user, smoke, drift, kill-switch, and settlement-validation evidence keys as missing.

2026-05-11 archive DB-audit artifact pass: `low-latency-archive-evidence` now writes `readiness_db_audit.txt` into each durable evidence archive, includes it in the manifest and checksums, and `low-latency-verify-evidence-archive` requires and validates the report shape/count fields without opening the trading database. Verified with `PYTHONPATH=src python3 -m unittest tests.test_latency_report` passing 103 tests, the full 360-test roadmap suite under tracemalloc, and a disposable `/private/tmp/whenitrains-archive-db-audit-smoke` archive. The smoke archive command exited `2` after writing because readiness evidence is absent, listed `readiness_db_audit.txt` in the written files, and the verifier rejected only the expected empty latency/HKO reports and missing readiness gates, not the DB audit artifact.

2026-05-11 archive DB-audit consistency pass: tightened `low-latency-verify-evidence-archive` so an archive with `all_gates_passed=True` is rejected if `readiness_db_audit.txt` still reports `readiness_db_audit=missing_evidence`. Verified red/green with `PYTHONPATH=src python3 -m unittest tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_fails_passing_archive_with_missing_db_audit tests.test_latency_report.LatencyReportTests.test_low_latency_verify_evidence_archive_passes_complete_manifest` and `PYTHONPATH=src python3 -m unittest tests.test_latency_report` passing 104 tests.

2026-05-11 archive DB-audit zero-count consistency pass: tightened `low-latency-verify-evidence-archive` so `readiness_db_audit=evidence_present` is only valid when all required DB-audit evidence counts are positive; a zero required count now makes `readiness_db_audit.txt` malformed. Verified with `PYTHONPATH=src python3 -m unittest tests.test_latency_report` passing 105 tests and the full roadmap suite passing 362 tests under tracemalloc.

2026-05-11 archive DB-audit missing-list consistency pass: tightened `low-latency-verify-evidence-archive` so `readiness_db_audit=missing_evidence` is valid only when the archived `missing_evidence=` list exactly matches the zero-count required evidence keys in `readiness_db_audit.txt`. Verified with `PYTHONPATH=src python3 -m unittest tests.test_latency_report` passing 106 tests and the full roadmap suite passing 363 tests under tracemalloc.

2026-05-11 readiness DB audit no-create regression: added a CLI regression proving `low-latency-readiness-db-audit` exits `2` and does not create a missing SQLite DB path. Verified with `PYTHONPATH=src python3 -m unittest tests.test_cli` passing 32 tests and the full roadmap suite passing 364 tests under tracemalloc. The full suite still emits existing unclosed-SQLite `ResourceWarning`s from older scheduler/live tests but exits successfully.

2026-05-11 runbook read-only audit update: updated `docs/low-latency-live-runbook.md` so the return-to-normal checklist explicitly runs `low-latency-readiness-db-audit` before final readiness/archive commands and requires nonzero evidence counts across latency traces, timed HKO raw snapshots, WebSocket orderbook snapshots, orderbook-age decisions, live orders, live user events, and risk-event smoke records.

2026-05-11 readiness test cleanup pass: scheduler, operational-readiness, and live-user-stream tests now register SQLite connections for cleanup, removing the unclosed-connection `ResourceWarning`s seen in the full roadmap suite. Verified with `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_scheduler tests.test_operational_readiness tests.test_live_user_stream` passing 44 tests and the full roadmap suite passing 364 tests under tracemalloc without the prior SQLite handle warnings.

2026-05-11 post-readiness-cleanup live log endpoint retry: sandboxed and approved LAN `curl -L --max-time 8 http://192.168.1.23:8765/` both timed out after 8 seconds. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 post-cleanup read-only production DB audit: reran `PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 low-latency-readiness-db-audit`. It exited `2` with the same production evidence gap: 22,372 HKO raw snapshots and 637,810 orderbook snapshots exist, but all required latency pairs, timed HKO raw rows, WebSocket orderbook snapshots, orderbook-age decisions, live orders/user events, and live smoke/verification records remain zero.

2026-05-11 low-latency scheduler wake pass: `LowLatencyEventQueue` now notifies waiters when a new unique event is enqueued, and the scheduler waits through the queue when present instead of always sleeping for the full scheduler interval. This keeps live execution on the scheduler thread while allowing background AWS actual events to wake the next drain promptly. Verified with `PYTHONPATH=src python3 -m unittest tests.test_low_latency tests.test_scheduler` passing 45 tests and the full roadmap suite passing 366 tests under tracemalloc.

2026-05-11 queue wake regression hardening: tightened the low-latency queue test so it verifies `wait_for_event_or_stop` wakes when an event is enqueued after the wait begins, not only when the queue is already non-empty. Reverified `PYTHONPATH=src python3 -m unittest tests.test_low_latency tests.test_scheduler` and the full 366-test roadmap suite under tracemalloc.

2026-05-11 drift-scan latency evidence pass: live scheduler startup and reconcile-watchdog CLOB drift scans now record `live_clob_drift_scan_started -> live_clob_drift_scan_completed` latency stages. Readiness reports, readiness gates, evidence archives, archive verification, DB audit, and the live readiness checklist now include that pair so the roadmap's local-vs-CLOB drift p50/p95/p99 requirement has a concrete artifact. Verified `PYTHONPATH=src python3 -m unittest tests.test_cli tests.test_latency_report` passing 138 tests, the full 366-test roadmap suite under tracemalloc, and a read-only `data/whenitrains.sqlite3` DB audit that still exits `2` with zero production evidence including `latency_live_clob_drift_scan_pairs=0`.

2026-05-11 drift archive smoke: generated `/private/tmp/whenitrains-low-latency-drift-archive-smoke` with `low-latency-archive-evidence --require-evidence`. The command exited `2` after writing the full expanded file set, including submit-to-ack, submit-to-match, submit-to-fill, submit-to-reject, and live CLOB drift-scan latency reports. `low-latency-verify-evidence-archive` rejected the bundle as expected because the production-like DB still has zero latency/readiness samples and missing live gates, including `live_clob_drift_scan_latency_observed`.

2026-05-11 post-drift-archive live log endpoint retry: sandboxed and approved LAN `curl -L --max-time 8 http://192.168.1.23:8765/` both timed out after 8 seconds. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 M3 concurrency audit note: rechecked runner integration for the roadmap's independent-candidate concurrency requirement. `ExecutionScheduler` itself has unit coverage for concurrent independent candidates and deterministic serialization of conflicts, and runner hot paths build `PlannedCandidateAction` objects through the candidate bridge. Actual runner execution remains `max_workers=1` in local code because the active SQLite connection is not thread-shareable; live-account proof of safe independent CLOB candidate concurrency remains a production evidence gap.

2026-05-11 live concurrency evidence checklist pass: updated `live-readiness-checklist` and `docs/low-latency-live-runbook.md` so capped live scheduler smoke requires archived scheduler logs showing either independent candidate actions progressing concurrently or that no independent-candidate opportunity occurred. This makes the M3 concurrency proof gap explicit in the operator evidence path.

2026-05-11 live concurrency evidence checklist verification: red/green checked `tests.test_cli.CliDiscoveryTests.test_live_readiness_checklist_prints_ordered_evidence_commands`, then verified `PYTHONPATH=src python3 -m unittest tests.test_cli` passing 32 tests and `git diff --check` passing.

2026-05-11 post-live-concurrency-checklist live log endpoint retry: sandboxed `curl -L --max-time 8 http://192.168.1.23:8765/` timed out after 8 seconds; the approved LAN retry failed immediately with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 1 ms: Couldn't connect to server`. Live/account evidence capture remains blocked on endpoint availability.

2026-05-11 compact latency event detail pass: tightened `compact_latency_event_line` so scheduler-drained fast-event logs include forecast raw max/min changes and market status transitions in addition to actual transition details. Verified red/green with focused `tests.test_low_latency.LowLatencyReadinessTests` formatter tests, then ran `PYTHONPATH=src python3 -m unittest tests.test_low_latency tests.test_scheduler` passing 46 tests and `git diff --check` passing.

2026-05-11 roadmap endpoint status refresh: updated `docs/low-latency-readiness-roadmap.md` so the current live-log endpoint blocker matches the latest retry result: sandbox timeout and approved LAN connection refused.

2026-05-11 post-roadmap-refresh production DB audit: reran `PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 low-latency-readiness-db-audit` read-only. It exited `2` with 22,372 HKO raw snapshots and 637,810 orderbook snapshots present, but all required readiness evidence counts still zero: latency stage pairs, timed HKO rows, WebSocket orderbook snapshots, orderbook-age decisions, live/manual/reconciled/settlement orders, user-channel events, live network/auth/scheduler/kill-switch/drift/settlement-validation records. The production readiness gates remain blocked on live evidence capture.

2026-05-11 post-compact-latency full roadmap verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 367 tests.

2026-05-11 readiness audit status refresh: updated `docs/low-latency-readiness-audit.md` so its endpoint blocker, read-only production DB audit counts, and latest verification section match the current command evidence instead of the earlier drift-scan checkpoint.

2026-05-11 SDK split-order audit: checked the repo `.venv` live CLOB client dependency for the roadmap's FAK/FOK and pre-built/pre-signed order items. `py_clob_client_v2` exposes `create_order`, `create_market_order`, and `post_order`; live v2 buy/sell submission now creates/signs first and posts FAK explicitly. Verified red/green with focused `tests.test_live.LiveTests` split-order tests, then ran `PYTHONPATH=src python3 -m unittest tests.test_live` passing 43 tests and `git diff --check` passing.

2026-05-11 post-split-order full roadmap verification: `PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata` passed with 368 tests after separating v2 live order create/sign from FAK posting.

2026-05-11 readiness audit latest-verification refresh: updated `docs/low-latency-readiness-audit.md` so its latest verification section reports the current 368-test suite after the split-order implementation.

2026-05-11 venv live SDK verification: reran live tests with the repo venv that has `py_clob_client_v2` installed. Direct module-name unittest invocation failed under Python 3.14 because `tests` is not an importable package there, then `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_live.py'` passed 43 tests.

2026-05-11 live readiness venv command pass: tightened `live-readiness-checklist` and `docs/low-latency-live-runbook.md` so live evidence commands use `PYTHONPATH=src .venv/bin/python -m whenitrains.cli`, ensuring real-auth smoke and live scheduler commands run in the environment where `py_clob_client_v2` is installed. Verified red/green with `tests.test_cli.CliDiscoveryTests.test_live_readiness_checklist_prints_ordered_evidence_commands`, then ran `PYTHONPATH=src python3 -m unittest tests.test_cli` passing 32 tests and `git diff --check` passing.

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
- Run capped live scheduler smoke and archive `live_scheduler_smoke_ok` evidence.
- Validate the first resolved live settlement against CLOB/onchain truth.
- Verify production p50/p95/p99 latency from HKO DB commit through decision, live order submission, fill/reject, and local-vs-CLOB drift scan.
- Verify live kill-switch behavior against the real account before scheduler use.
- Run `low-latency-archive-evidence --output-dir data/low-latency-evidence/<run-id> --require-evidence` on the production DB, verify it with `low-latency-verify-evidence-archive --input-dir data/low-latency-evidence/<run-id>`, and archive the generated artifacts with scheduler logs.

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
