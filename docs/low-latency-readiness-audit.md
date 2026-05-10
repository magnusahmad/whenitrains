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

The live log endpoint at `http://192.168.1.23:8765/` was retried on 2026-05-11 HKT and failed with `curl: (7) Failed to connect to 192.168.1.23 port 8765`.

## Prompt-To-Artifact Checklist

### M0: Latency Instrumentation First

- `latency_trace_events` exists and stores structured trace rows.
- `fetch_response` and HKO raw snapshot storage persist fetch start, header receipt, payload receipt, and elapsed milliseconds.
- Event-keyed live buy/sell execution records `order_submitted`, `clob_ack`, `fill_matched`, and `fill_confirmed`.
- `latency-report` summarizes p50/p95/p99 between named stages.
- `low-latency-readiness-report` prints the core latency stage pairs, explicit evidence gates including commit-to-decision-completed, CLOB ack, fill-match, orderbook-age-under-cap, and live-money-state-clear evidence, live money-state, and HKO source-timing evidence; `--require-evidence` exits nonzero when any measurable local evidence gate is missing.
- Compact fast-event latency lines are emitted during scheduler drain.
- Evidence: `src/whenitrains/storage.py`, `src/whenitrains/live.py`, `src/whenitrains/low_latency.py`, `src/whenitrains/cli.py`.
- Tests: `tests.test_low_latency`, `tests.test_latency_report`, focused live latency tests.
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
- Evidence: `src/whenitrains/orderbook_cache.py`, `src/whenitrains/market_websocket.py`, `src/whenitrains/live_runtime.py`, `src/whenitrains/runner.py`.
- Tests: `tests.test_orderbook_cache`, `tests.test_market_websocket`, focused live runner tests.
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
- Evidence: `src/whenitrains/user_websocket.py`, `src/whenitrains/live_user_stream.py`, `src/whenitrains/live.py`, `src/whenitrains/runner.py`, `src/whenitrains/cli.py`.
- Tests: `tests.test_live_user_stream`, `tests.test_user_websocket`, `tests.test_live`, `tests.test_cli`, `tests.test_runner`.
- Missing: real user WebSocket smoke, recent-trades validation against the account, and live settlement validation against CLOB/onchain truth.

### M5: Polling Strategy Hardening

- Learned AWS GIS publish windows include sub-second burst cadence.
- Non-critical source backoff does not suppress AWS actual polling.
- HKO source timing is persisted for audit.
- `hko-source-timing-report` summarizes persisted HKO raw snapshot timings, response latency percentiles, fetch-second offsets, and HTTP `Last-Modified` minute offsets for live dry-run evidence.
- Live hot-path buys fail closed when configured WebSocket book cache is stale or missing.
- Evidence: `src/whenitrains/scheduler.py`, `src/whenitrains/hko.py`, `src/whenitrains/storage.py`, `src/whenitrains/runner.py`.
- Tests: `tests.test_scheduler`, `tests.test_storage`, focused live runner tests.
- Missing: captured live dry-run report output showing actual fetch attempts clustered around learned public availability and not blocked by orderbook work.

### M6: Operational Readiness

- Live scheduler takes a DB-specific exclusive lock.
- Startup health covers WebSocket runtime, REST fallback, credentials, balance/allowance, stale submitted orders, and local/CLOB drift.
- Health failures freeze new entries and can emit alerts.
- Trade alerts, source-freshness breach alerts, stalled-WebSocket freezes, stale submitted-order watchdog, persistent kill-switch exits, pending-order reconciliation, and live runbook are implemented.
- Evidence: `src/whenitrains/operational.py`, `src/whenitrains/alerting.py`, `src/whenitrains/live.py`, `src/whenitrains/cli.py`, `docs/low-latency-live-runbook.md`.
- Tests: `tests.test_operational_readiness`, `tests.test_alerting`, `tests.test_live`, `tests.test_cli`, `tests.test_scheduler`.
- Missing: manual live-auth smoke, minimum-size manual buy/sell, scheduler dry-run, capped live scheduler, and real-account kill-switch verification.

## Latest Verification

Fresh-process verification after the latest local changes:

```bash
PYTHONPATH=src python3 -m unittest tests.test_runner
PYTHONPATH=src python3 -m unittest tests.test_live
PYTHONPATH=src python3 -m unittest tests.test_cli tests.test_low_latency
PYTHONPATH=src python3 -m unittest tests.test_storage tests.test_markets tests.test_orderbook_cache
PYTHONPATH=src python3 -m unittest tests.test_cli.CliDiscoveryTests.test_hko_source_timing_report_summarizes_aws_fetch_attempts
git diff --check
```

All passed. A single larger combined multi-module run still hit the repository's existing unclosed-SQLite-connection file-descriptor cascade, so fresh-process runs are the reliable local verification until that test harness issue is fixed.

## Next Steps

1. Restore or expose the live log endpoint on `192.168.1.23:8765`.
2. Run `live-network-smoke --live --require-connected` and capture the logs.
3. Run `live-auth-smoke --live` with credentials on the live machine.
4. With explicit approval, run minimum-size manual live buy/sell and kill-switch verification.
5. Run capped live scheduler and collect `latency-report` p50/p95/p99 evidence from the production DB.
6. Run `low-latency-readiness-report --require-evidence` on the production DB and archive the output with the live scheduler logs.
