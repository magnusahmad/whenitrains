# Low-Latency Readiness Roadmap

Last updated: 2026-05-11

## Objective

Make `whenitrains` fast enough to beat passive bots that react to the same HKO and Polymarket signals. The near-term target is to detect an actionable HKO DB change about 1 second after it lands locally, make a deterministic trading decision immediately, submit an executable CLOB order with fresh book state, and reconcile local state to CLOB/onchain truth without waiting for a manual repair loop.

## Current Readiness

The local roadmap implementation is substantially complete. See `docs/low-latency-readiness-audit.md` for the prompt-to-artifact checklist and latest verification evidence.

- HKO and Polymarket storage commits can enqueue narrow low-latency events for AWS actual transitions, OCF forecast-sample changes, and market-resolution status changes.
- Paper scheduler starts a blocking `FastDecisionWorker`; live scheduler drains the shared fast-event queue before watchdog ticks, and scheduler sleep is interrupted by new queue arrivals while live-client ownership stays inside the scheduler thread.
- Live execution can use a scheduler-owned Polymarket WebSocket orderbook cache and fails closed when a configured cache is stale or missing.
- Market and user WebSocket clients, live runtime ownership, authenticated user-event storage/application, pending-order reconciliation, sellable-balance drift repair/freeze, and resolved-market local settlement are implemented with fixture and scheduler tests.
- Candidate planning, ladder metadata, and execution scheduling are wired into actual-cross, lowest-temperature actual-cross, forecast-change, forecast-value, forecast-exit, and open-position exit paths.
- Operational safeguards now include a DB-specific live scheduler lock, startup health freeze, stale submitted-order freeze, persistent kill-switch exit enforcement, alerts, stalled-WebSocket freeze, source-freshness alerts, and a live runbook.
- Readiness reports and evidence archives include p50/p95/p99 latency files for commit-to-decision, decision-to-submit, submit-to-ack, submit-to-match, submit-to-fill/reject, and live CLOB drift-scan timing.

Remaining readiness gaps require live-environment evidence rather than more local scaffolding:

- Live network smoke for the market/user WebSocket runtime.
- Real-auth CLOB smoke with installed dependency and credentials.
- Minimum-size manual buy/sell and capped scheduler smoke with explicit approval.
- Real-account kill-switch and settlement validation.
- Production p50/p95/p99 evidence for DB commit to decision, decision to submit, submit to fill/reject, and local-vs-CLOB drift.

The live-log endpoint moved to `http://192.168.1.49:8765/` and was reachable earlier on 2026-05-11 HKT, but this workstation is no longer on the same LAN as the live machine. The last downloaded log, `live-scheduler-20260511-071055.log`, shows live scheduler startup and repeated decision loops with no buys or sells, but the remaining live network smoke, auth smoke, manual-money, settlement, and production readiness report evidence is still not captured. `live-readiness-checklist --live-log-url ...` now allows the operator to generate evidence commands for a reachable log endpoint, while non-LAN runs must collect `live-scheduler.log` on the live machine or copy it into the evidence directory by another secure channel.

## Research Findings

- Polymarket documents a market WebSocket at `wss://ws-subscriptions-clob.polymarket.com/ws/market` that streams `book`, `price_change`, `last_trade_price`, and optional `best_bid_ask` events for subscribed token IDs. This is the right primary source for low-latency orderbook state; REST `/book` should become startup/backfill/fallback.
- Polymarket documents a user WebSocket at `wss://ws-subscriptions-clob.polymarket.com/ws/user` for authenticated `trade` and `order` events. This is the right primary source for live fill/order state; REST reconciliation should become a watchdog and restart repair path.
- Polymarket CLOB REST limits are high enough for fallback polling, including `/book` at 1,500 requests per 10 seconds and `/books` at 500 requests per 10 seconds, but docs say Cloudflare throttles by delaying/queueing over-limit calls. Latency-sensitive code should avoid living near those limits.
- Polymarket FAK is explicitly documented as immediate partial fill plus cancellation of unfilled remainder. That matches the current live execution intent.
- HK Open Data lists the regional 1-minute air temperature and since-midnight max/min sources as every 10 minutes, while the current code has correctly learned AWS GIS publish/fetchable minutes from observed headers. The competitive edge is therefore not a lower official cadence; it is discovering the public availability minute and reacting before passive pollers.

Sources:

- https://docs.polymarket.com/market-data/websocket/market-channel
- https://docs.polymarket.com/market-data/websocket/user-channel
- https://docs.polymarket.com/trading/orders/create
- https://docs.polymarket.com/api-reference/rate-limits
- https://www.hko.gov.hk/en/abouthko/opendata_intro.htm

## Roadmap

### M0: Latency Instrumentation First

Goal: prove where time is going before optimizing further.

Deliverables:

- Add monotonic timestamp columns or structured trace rows for `fetch_started`, `payload_received`, `db_committed`, `event_detected`, `decision_started`, `decision_completed`, `orderbook_state_age`, `order_submitted`, `clob_ack`, `fill_matched`, and `fill_confirmed`.
- Add per-token orderbook age to every filled/missed decision.
- Emit a compact latency line whenever any alpha event is detected, including actual crosses, forecast changes, forecast-value opportunities, invalidation exits, and later strategy families.

Verification:

- Unit test that an inserted AWS actual transition records all expected latency stages.
- Integration test with fake clock proving decision start is under 1 second after the HKO row commit.

Exit criteria:

- We can answer p50/p95/p99 for HKO commit to decision, decision to submit, submit to fill/reject, and local-vs-CLOB position drift.

### M1: DB-Change Driven Decisioning

Goal: stop waiting for the next scheduler loop when HKO data already landed.

Deliverables:

- Introduce an internal event queue fed by HKO ingestion commits. For SQLite, use a writer-side enqueue call after successful commit rather than polling the DB for changes.
- Run fast decision workers that block on the queue and process `aws_actual_transition`, `forecast_sample_changed`, `market_resolution_changed`, and strategy-specific alpha events immediately.
- Allow one source event to fan out into several candidate actions. Example: an actual-cross surprise may buy the crossed bin YES, sell an invalidated YES position already held, and buy the invalidated bin NO.
- Allow independent source events to execute concurrently across markets, sides, lead dates, and strategy families. Example: a same-day actual-cross fast lane should not block a forecast-change opportunity on D+1, a lowest-temperature market, or a later city/market family.
- Keep the existing 1-second scheduler loop only as a watchdog/retry path.
- Make event and candidate keys idempotent so queue redelivery cannot double-trade, while still allowing distinct candidates from the same source event to each reach a terminal action.

Verification:

- Fake-clock test inserts an AWS actual max transition and asserts the fast worker calls the narrower event handler before the watchdog tick.
- Fan-out test inserts one actual-cross surprise and confirms all expected candidate actions are emitted: crossed-bin YES entry, invalidated held-position exit, and invalidated-bin NO entry.
- Parallelism test inserts independent alpha events for different markets/strategy families and confirms eligible orders are dispatched concurrently subject to configured risk limits.
- Idempotency test replays the same queued event twice and confirms one terminal decision/order per distinct candidate key.

Exit criteria:

- HKO row commit to decision start is consistently below 1 second on the live machine.

### M2: Polymarket WebSocket Book Cache

Goal: remove REST orderbook polling from the hot path.

Deliverables:

- Add a market WebSocket client that subscribes to all active YES/NO token IDs with `custom_feature_enabled: true`.
- Maintain an in-memory book cache plus append-only SQLite snapshots for `book`, `price_change`, `best_bid_ask`, and `last_trade_price`.
- On market discovery changes, resubscribe without restarting the scheduler.
- REST `/books` or `/book` becomes startup snapshot/backfill and reconnect recovery only.
- Decisioning reads the in-memory book first and rejects or refreshes only when the book age exceeds a tight threshold.

Verification:

- WebSocket fixture tests for snapshot application, level removal when size is `0`, best bid/ask updates, reconnect, and stale-cache rejection.
- End-to-end fake source test proving a fast-lane decision does not call REST `/book` when WebSocket book state is fresh.

Exit criteria:

- CLOB book age at order submission is normally below 250 ms and never silently older than the configured cap.

### M3: Hot-Path Execution Engine

Goal: submit as little work as possible after alpha is detected.

Deliverables:

- Precompute candidate token IDs, sides, max prices, neg-risk flags, tick sizes, min sizes, held-position state, and position budgets for each active ladder and strategy family.
- Split the runner into narrow event handlers so source events run only the relevant strategy families instead of the full paper/live tick.
- Add a candidate planner that turns source events into ordered action sets. Example actual-cross surprise action set: sell invalidated YES positions already held, buy crossed-bin YES, and buy invalidated-bin NO when risk and book state allow.
- Add an execution scheduler that runs independent action sets concurrently while serializing conflicting operations on the same token, position, or shared risk budget.
- Keep FAK as default for immediate liquidity capture; optionally add FOK for strategies where partial fills are worse than no fill.
- Add a pre-signed or pre-built order path if the SDK supports separating create/sign from submit safely for short-lived orders.

Verification:

- Unit test that each source event invokes only relevant strategy handlers and targeted token checks.
- Parallel execution test proving independent candidates submit concurrently, while conflicting candidates for the same token/position are ordered deterministically.
- Latency benchmark with fake CLOB client proving decision-to-submit under 100 ms excluding network.

Exit criteria:

- Hot-path CPU/database work for each candidate is dominated by one event lookup, one book-cache read, one risk check, and one submit call; independent candidates can progress in parallel.

### M4: User WebSocket Reconciliation

Goal: make scheduler state converge to CLOB/onchain state automatically.

Deliverables:

- Add authenticated user WebSocket client for `order` and `trade` events.
- Store order lifecycle states independently from final position application.
- Apply matched trade deltas from user events, then confirm or repair with REST order/trade lookup.
- Run a reconciliation watchdog on startup and periodically: open orders, recent trades, sellable balances, local live positions.
- Freeze new entries when local position state, sellable balance, or submitted-order state disagrees beyond tolerance.

Verification:

- Fixture tests for `PLACEMENT`, `UPDATE`, `CANCELLATION`, `MATCHED`, `MINED`, `CONFIRMED`, `RETRYING`, and `FAILED`.
- Crash-restart test with a submitted order row and a later user trade event proving local position converges once.

Exit criteria:

- The dashboard and scheduler agree with CLOB sellable balances and recent trades after restart, WebSocket reconnect, partial fill, and failed settlement.

### M5: Polling Strategy Hardening

Goal: keep HKO ingestion competitive while respecting official cadence and avoiding hidden stalls.

Deliverables:

- Keep learned AWS GIS publish-minute windows, but add adaptive sub-second burst polling only inside the highest-value 10-20 second intervals around learned public availability.
- Record HTTP response timing and header timings for every HKO source.
- Report fetch-to-public-availability offsets and fail readiness evidence when the production DB lacks clustered AWS actual fetches inside the configured burst window.
- Add backoff state that slows non-critical sources without slowing the actual worker.
- Add freshness gates per signal type: if HKO source fresh but Polymarket cache stale, skip trading; if Polymarket fresh but HKO stale, do not infer a signal.

Verification:

- Fake-source tests for learned publish windows, burst cadence, and backoff isolation.
- Live dry-run report showing actual fetch attempts clustered around learned public availability and not blocked by orderbook work.

Exit criteria:

- Fresh HKO public availability is detected within the configured burst cadence, and the system proves DB commit to decision under 1 second.

### M6: Operational Readiness

Goal: run unattended without ambiguous money state.

Deliverables:

- Single-process DB lock before live scheduler start.
- Live startup health check: WebSocket market connected, user channel connected, REST fallback available, CLOB credentials valid, balances/allowances sufficient, no stale submitted orders, no local/CLOB drift.
- External alerting for trade, critical risk event, stalled WebSocket, source freshness breach, and reconciliation freeze.
- Runbook for start, stop, cancel-all, reconcile, restart after crash, and disable new entries.

Verification:

- Integration tests for startup fail-closed cases.
- Manual live-auth smoke, then minimum-size manual buy/sell, then scheduler dry-run, then capped live scheduler.

Exit criteria:

- Live scheduler can be restarted without manual DB surgery and will either converge state or freeze new entries with a precise reason.

## Priority Order

1. Add latency instrumentation and DB-change driven decisioning.
2. Add market WebSocket book cache and remove hot-path REST orderbook fetches.
3. Narrow the strategy execution paths, add candidate fan-out, and precompute active ladder metadata.
4. Add user WebSocket reconciliation and restart repair.
5. Harden HKO burst polling/backoff and operational fail-closed checks.

The current code has local/tested scaffolding for every roadmap milestone. It should not be treated as production-complete until the live network, real-auth, manual-money, capped-scheduler, kill-switch, settlement, and production latency evidence in `docs/low-latency-readiness-audit.md` has been captured. Use `docs/low-latency-live-evidence-handoff.md` for the current non-LAN live evidence procedure.
