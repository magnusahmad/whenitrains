# Low-Latency Readiness Audit

Last updated: 2026-05-11 HKT

## Objective

Implement the low-latency readiness roadmap so `whenitrains` can detect actionable HKO or Polymarket state changes quickly, make narrow deterministic decisions, submit live CLOB orders with fresh book state, and reconcile local live state without a manual repair loop.

## Completion Status

Local implementation is substantially complete and covered by targeted automated tests. The roadmap is not fully complete because the remaining exit criteria require evidence from the live environment:

- Live network smoke for Polymarket market/user WebSocket runtime and live scheduler ownership.
- Real-auth CLOB smoke with installed dependency and credentials.
- Minimum-size manual buy/sell and capped scheduler smoke with explicit approval.
- Production p50/p95/p99 evidence for DB-commit-to-decision, decision-to-submit, submit-to-fill/reject, and local-vs-CLOB drift.
- Real-account kill-switch and settlement validation against actual CLOB/onchain state.

The live log endpoint at `http://192.168.1.23:8765/` was retried again on 2026-05-11 HKT after CLI/test-harness cleanup and failed with `curl: (7) Failed to connect to 192.168.1.23 port 8765 after 1 ms: Couldn't connect to server`.

## Prompt-To-Artifact Checklist

### M0: Latency Instrumentation First

- `latency_trace_events` exists and stores structured trace rows.
- `fetch_response` and HKO raw snapshot storage persist fetch start, header receipt, payload receipt, and elapsed milliseconds.
- Event-keyed live buy/sell execution records `order_submitted`, `clob_ack`, `fill_matched`, `fill_confirmed`, and submitted-order terminal rejection/cancellation as `order_rejected`.
- `latency-report` summarizes p50/p95/p99 between named stages.
- `low-latency-readiness-report` prints the core latency stage pairs, submitted-order rejection timing, explicit evidence gates including commit-to-decision-completed, CLOB ack, fill-match, optional submit-to-reject evidence, WebSocket orderbook snapshot, orderbook-age-under-cap, user-channel-event, HKO public-availability fetch clustering, clear live CLOB drift scan, live-money-state-clear, and kill-switch-clear evidence, live money-state, and HKO source-timing evidence; `--require-evidence` exits nonzero when any measurable local evidence gate is missing.
- Compact fast-event latency lines are emitted during scheduler drain.
- Evidence: `src/whenitrains/storage.py`, `src/whenitrains/live.py`, `src/whenitrains/low_latency.py`, `src/whenitrains/cli.py`.
- Tests: `tests.test_low_latency`, `tests.test_latency_report`, `tests.test_recorded_fixtures`, focused live latency tests.
- Missing: production p50/p95/p99 from live DB rows.

### M1: DB-Change Driven Decisioning

- HKO actual ingestion enqueues `aws_actual_transition` events after commit.
- OCF forecast sample storage enqueues `forecast_sample_changed` events after commit.
- Polymarket market status updates enqueue `market_resolution_changed` events after commit.
- Scheduler loops share and drain a low-latency queue before watchdog decisions.
- `FastDecisionWorker` blocks on the queue with a separate SQLite connection.
- Source events route to narrow handlers and candidate execution preserves event/candidate idempotency.
- Evidence: `src/whenitrains/low_latency.py`, `src/whenitrains/scheduler.py`, `src/whenitrains/cli.py`, `src/whenitrains/runner.py`.
- Tests: `tests.test_low_latency`, `tests.test_scheduler`, `tests.test_runner`.
- Missing: live-machine proof that HKO row commit to decision start stays below 1 second.

### M2: Polymarket WebSocket Book Cache

- Market WebSocket client subscribes to active YES/NO token IDs.
- `OrderBookCache` applies `book`, `price_change`, `best_bid_ask`, and `last_trade_price`.
- Cache writes append-only SQLite snapshots with WebSocket metadata.
- Active token/condition subscription helpers support runtime resubscribe planning.
- Live tick receives a scheduler-owned cache and live buys reject missing/stale cache books when a cache is configured.
- `live-network-smoke --live --require-connected` starts and stops the scheduler-owned market/user WebSocket runtime without running trading decisions, reports per-client connection attempts, connected-once state, applied messages, and last error, and exits nonzero if fewer than the market/user clients are reported or any client never connected.
- `live-network-smoke --live --require-connected` records `live_network_smoke_ok`/`live_network_smoke_failed` evidence, and `low-latency-readiness-report --require-evidence` requires the latest network smoke event to be OK with both required WebSocket clients running and connected at least once.
- `low-latency-readiness-report --require-evidence` requires at least one persisted orderbook snapshot with `polymarket_market_websocket` metadata, usable bid/ask/mid prices, and non-empty bid/ask depth, so the production report cannot pass on connection liveness or placeholder snapshots alone.
- Evidence: `src/whenitrains/orderbook_cache.py`, `src/whenitrains/market_websocket.py`, `src/whenitrains/live_runtime.py`, `src/whenitrains/runner.py`.
- Tests: `tests.test_orderbook_cache`, `tests.test_market_websocket`, `tests.test_recorded_fixtures`, focused live runner tests.
- Missing: real Polymarket WebSocket smoke and observed live book age at submission.

### M3: Hot-Path Execution Engine

- Active ladder metadata precomputes token sides, book metadata, held positions, and remaining budgets.
- Actual-cross, actual low-cross, forecast-change, forecast-value, forecast-exit, and open-position exit paths use narrow handlers and planned candidate actions.
- `ExecutionScheduler` preserves deterministic ordering for conflicting token/position/risk keys.
- Fake-clock live benchmark verifies decision-to-submit under 100 ms excluding network.
- Evidence: `src/whenitrains/ladder_metadata.py`, `src/whenitrains/candidate_planner.py`, `src/whenitrains/execution_scheduler.py`, `src/whenitrains/runner.py`.
- Tests: `tests.test_ladder_metadata`, `tests.test_candidate_planner`, `tests.test_execution_scheduler`, `tests.test_runner`, focused live benchmark.
- Missing: production CPU/database timing evidence on live hardware.

### M4: User WebSocket Reconciliation

- User WebSocket client authenticates and applies order/trade events.
- `live_user_events` stores lifecycle events independently from final positions.
- Matched trade deltas are idempotent and can converge after restart.
- Startup and periodic watchdog reconcile pending live orders, rebuild positions, compare sellable balances, repair safe local-greater-than-CLOB drift, and freeze new entries when drift remains.
- Resolved/closed past-date markets locally settle remaining paper/live positions when stored target-date actuals identify the winning side.
- `low-latency-readiness-report --require-evidence` fails while live orders remain in unresolved `submitted`, `unknown_fill`, `open`, or `pending` states, or terminal problem `error`, `rejected`, `blocked`, or `failed` states.
- `low-latency-readiness-report --require-evidence` requires at least one stored `live_user_events` row so the production report cannot pass without authenticated user-channel evidence.
- `low-latency-readiness-report --require-evidence` requires at least one stored user-channel `trade` event with `applied_position_delta = 1`, order/token ids, matched/mined/confirmed status, side, and positive price/size, so order lifecycle messages or malformed trade placeholders cannot satisfy the matched-trade reconciliation requirement.
- `low-latency-readiness-report --require-evidence` requires at least one reconciled filled live order row with a CLOB order id and non-empty reconcile payload, so the production report cannot pass without archived `live-reconcile`/REST reconciliation evidence after live-money testing.
- `low-latency-readiness-report --require-evidence` requires at least one filled live settlement/market-resolution row with positive fill price and quantity, so local readiness cannot pass until a real resolved-market settlement has been observed and archived for live validation.
- `live-settlement-validate --live --order-id ... --reference ...` records explicit CLOB/onchain settlement validation evidence for a filled settlement row, and `low-latency-readiness-report --require-evidence` requires validation evidence that matches a filled settlement order row and includes a non-empty external reference.
- `low-latency-readiness-report --require-evidence` requires the latest stored live CLOB drift scan to be clear with explicit `drift_count=0`, so the production report cannot pass from stale or placeholder clear evidence, or from the absence of open/problem rows alone, without evidence that the live scheduler compared local positions to CLOB sellable balances.
- Evidence: `src/whenitrains/user_websocket.py`, `src/whenitrains/live_user_stream.py`, `src/whenitrains/live.py`, `src/whenitrains/runner.py`, `src/whenitrains/cli.py`.
- Tests: `tests.test_live_user_stream`, `tests.test_user_websocket`, `tests.test_live`, `tests.test_cli`, `tests.test_runner`.
- Missing: real user WebSocket smoke, recent-trades validation against the account, and live settlement validation against CLOB/onchain truth.

### M5: Polling Strategy Hardening

- Learned AWS GIS publish windows include sub-second burst cadence.
- Non-critical source backoff does not suppress AWS actual polling.
- HKO source timing is persisted for audit.
- `hko-source-timing-report` summarizes persisted HKO raw snapshot timings, response latency percentiles, fetch-second offsets, HTTP `Last-Modified` minute offsets, and fetch-to-public-availability offsets for live dry-run evidence; `low-latency-readiness-report --require-evidence` only counts HKO source-timing rows that include explicit fetch-start timing and response elapsed milliseconds.
- `low-latency-readiness-report --require-evidence` requires at least two HKO fetches within the configured burst window around observed public availability, so the production report cannot pass with arbitrary background timing rows alone.
- Live hot-path buys fail closed when configured WebSocket book cache is stale or missing.
- Evidence: `src/whenitrains/scheduler.py`, `src/whenitrains/hko.py`, `src/whenitrains/storage.py`, `src/whenitrains/runner.py`.
- Tests: `tests.test_scheduler`, `tests.test_storage`, focused live runner tests.
- Missing: captured live dry-run report output from the production DB showing the public-availability clustering gate passed and HKO fetch attempts were not blocked by orderbook work.

### M6: Operational Readiness

- Live scheduler takes a DB-specific exclusive lock.
- Startup health covers WebSocket runtime, REST fallback, credentials, balance/allowance, stale submitted orders, and local/CLOB drift.
- `live-auth-smoke --live` runs live preflight without placing orders, prints signer/funder, required balance, observed balance, allowance state, and reason, and records `live_auth_smoke_ok`/`live_auth_smoke_failed` evidence.
- `low-latency-readiness-report --require-evidence` requires the latest stored live auth smoke event to be OK and backed by signer/funder, allowance, and sufficient-balance details, so the production report cannot pass with stale auth evidence after a later failed credentials, balance, or allowance check, or with a placeholder OK row.
- `live-readiness-checklist` prints the ordered live evidence commands for network smoke, auth smoke, kill-switch status, minimum-size manual buy/sell, reconciliation with explicit REST/recent-trades evidence archiving, real-account kill-switch verification, capped scheduler smoke, live settlement validation, latency percentiles including commit-to-decision-completed, submit-to-ack, submit-to-match, submit-to-fill, and submit-to-reject, direct HKO source-timing evidence, and `low-latency-readiness-report --require-evidence`.
- `low-latency-archive-evidence --output-dir ... --require-evidence` writes latency stage reports, HKO source timing, readiness report output, and a manifest into a durable evidence directory, returning nonzero after writing when readiness gates are missing.
- `low-latency-verify-evidence-archive --input-dir ...` verifies the manifest identity header, required archive metadata keys and value formats, exactly one ordered `files:` and `checksums:` section, unique manifest metadata/readiness gate keys, exact required entries scoped to the `files:` section with no unexpected files, required non-blank report files with expected report headers/gate lines, the complete expected readiness gate set exactly once with passing archived statuses, latency reports with positive sample counts and numeric p50/p95/p99 seconds, observed HKO source timing rows with parseable response-millisecond percentiles and public-availability offset buckets, exact unique SHA-256 checksum entries scoped to the `checksums:` section with no unexpected targets, checksum digest format, checksum targets, checksum matches, exact `all_gates_passed=True`, and non-contradictory well-formed `missing_gates` without opening the trading database, and fails with the archived missing-gate list for incomplete evidence bundles.
- `low-latency-readiness-report --require-evidence` requires filled `manual_live` BUY and SELL order rows with positive fill size or shares, so scheduler fills or empty placeholder rows cannot substitute for the explicit minimum-size manual buy/sell smoke.
- A capped `live-scheduler --live --ticks N` records `live_scheduler_smoke_ok`/`live_scheduler_smoke_failed` evidence, and `low-latency-readiness-report --require-evidence` requires the latest scheduler smoke event to be OK with positive ticks and WebSocket runtime enabled.
- `live-kill-switch --block-new-entries` and `--allow-new-entries` record persistent kill-switch verification evidence, and `low-latency-readiness-report --require-evidence` requires the latest verification event to be allowed/clear with both kill-switch flags explicitly false.
- Health failures freeze new entries and can emit alerts.
- Trade alerts, source-freshness breach alerts, stalled-WebSocket freezes, stale submitted-order watchdog, persistent kill-switch exits, pending-order reconciliation, and live runbook are implemented.
- Evidence: `src/whenitrains/operational.py`, `src/whenitrains/alerting.py`, `src/whenitrains/live.py`, `src/whenitrains/cli.py`, `docs/low-latency-live-runbook.md`.
- Tests: `tests.test_operational_readiness`, `tests.test_alerting`, `tests.test_live`, `tests.test_cli`, `tests.test_scheduler`, `tests.test_latency_report`.
- Missing: manual live-auth smoke, minimum-size manual buy/sell, scheduler dry-run, capped live scheduler, and real-account kill-switch verification.

## Latest Verification

Combined verification after the latest local changes:

```bash
PYTHONTRACEMALLOC=5 PYTHONPATH=src python3 -m unittest tests.test_runner tests.test_live tests.test_cli tests.test_low_latency tests.test_storage tests.test_markets tests.test_orderbook_cache tests.test_recorded_fixtures tests.test_latency_report tests.test_scheduler tests.test_operational_readiness tests.test_alerting tests.test_live_user_stream tests.test_user_websocket tests.test_execution_scheduler tests.test_candidate_planner tests.test_ladder_metadata
git diff --check
```

All passed. The combined roadmap verification ran 310 tests under tracemalloc without the previous unclosed-SQLite ResourceWarning cascade.

## Next Steps

1. Restore or expose the live log endpoint on `192.168.1.23:8765`.
2. Run `live-network-smoke --live --require-connected` and capture the logs.
3. Run `live-auth-smoke --live` with credentials on the live machine.
4. With explicit approval, run minimum-size manual live buy/sell and kill-switch verification.
5. Run capped live scheduler and collect `latency-report` p50/p95/p99 evidence from the production DB.
6. Run `low-latency-archive-evidence --output-dir data/low-latency-evidence/<run-id> --require-evidence` on the production DB, verify it with `low-latency-verify-evidence-archive --input-dir data/low-latency-evidence/<run-id>`, and archive the generated output with the live scheduler logs.
