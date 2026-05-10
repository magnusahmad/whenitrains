# Low-Latency Live Runbook

Last updated: 2026-05-11 HKT

## Scope

This runbook covers the low-latency live scheduler path for HK temperature markets. It assumes the scheduler is running against `data/whenitrains.sqlite3` and that live trading is explicitly enabled with `--live`.

## Start

1. Confirm the working tree is on the intended branch and no unrelated database operation is in progress.
2. Confirm live credentials are available with `whenitrains live-env-exports`.
3. Generate the concrete live evidence command list for the target market before running any live-money command:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 live-readiness-checklist --label <label> --side <YES-or-NO> --size-usd <minimum-size> --date <YYYY-MM-DD> --market-kind <highest-or-lowest> --scheduler-ticks 3
```

Archive this output with the scheduler logs. It is read-only and does not touch the database.

4. Run a no-trade live network smoke to confirm both scheduler-owned WebSocket workers can start:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 live-network-smoke --live --seconds 10 --require-connected
```

Expected output includes `live network smoke websocket_all_running=True`, at least two reported clients, per-client `connected_once=True` lines, and `live network smoke connected_once_all=True`; the command exits `0` only when runtime liveness, market/user client count, and connection evidence pass. This command starts and stops the market/user WebSocket runtime but does not run trading decisions.

5. Run a no-trade live auth smoke to confirm CLOB credentials, signer/funder addresses, balance, and allowance:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 live-auth-smoke --live
```

Expected output includes `auth ok=True`, `required_balance_usd=...`, `allowance_ok=True`, signer/funder addresses, and exits `0`. This command performs preflight checks only and does not place orders.

6. Confirm there is no emergency entry block unless intentional:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 live-kill-switch
```

7. Start the live scheduler:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 live-scheduler --live --verbose
```

Expected startup behavior:

- A startup SQLite backup is created unless `--no-startup-backup` is used.
- A DB-specific live scheduler lock is acquired.
- Stale submitted live orders freeze new entries with a `live_stale_submitted_orders` risk event.
- Live preflight validates credentials, balance, allowance, and kill-switch state.
- If `WHENITRAINS_ALERT_WEBHOOK_URL` is set, live startup-health freezes, reconcile-health freezes, and filled trade ticks emit JSON webhook alerts.

## Stop

Use a normal interrupt first so the scheduler can release its DB lock:

```bash
Ctrl-C
```

After stopping, inspect recent live orders and risk events from the dashboard or SQLite before restarting.

## Disable New Entries

To block new live entries while still allowing exit/reconcile workflows:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 live-kill-switch --block-new-entries
```

To allow entries again after the issue is understood:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 live-kill-switch --allow-new-entries
```

## Cancel All Open CLOB Orders

Use this when submitted order state is ambiguous or when shutting down under market stress:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 live-cancel-all --live --yes-i-understand
```

Then run reconciliation.

## Reconcile

Use reconciliation after cancel-all, restart, WebSocket reconnect, or any suspected state drift:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 live-reconcile --live
```

Expected behavior:

- Submitted and unknown-fill live orders are checked through CLOB REST.
- Filled orders rebuild live positions.
- User-channel trade events, when available, apply matched deltas exactly once.
- `low-latency-readiness-report --require-evidence` will fail until at least one live order row has `reconciled_at_utc`, so archive this output after the manual buy/sell reconciliation pass.

## Restart After Crash

1. Do not delete or reset `data/whenitrains.sqlite3`.
2. Disable new entries:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 live-kill-switch --block-new-entries
```

3. Cancel all open CLOB orders if submitted state is ambiguous.
4. Run live reconciliation.
5. Inspect risk events for `live_stale_submitted_orders`, balance mismatches, and submit failures.
6. Restart the scheduler only after local live orders and CLOB state are understood.
7. Re-enable entries only after the restart has passed preflight and state checks.

## Critical Alerts To Page On

- `live_order_submit_failed` with severity `critical`.
- `live_stale_submitted_orders`.
- Stalled market WebSocket or user WebSocket.
- Polymarket book cache stale during a live hot-path entry.
- Local/CLOB position or sellable-balance drift.
- HKO source freshness breach during a learned AWS actual publish window.

Webhook alerts:

```bash
export WHENITRAINS_ALERT_WEBHOOK_URL=https://alerts.example.invalid/whenitrains
```

The webhook receives JSON with `title`, `severity`, `details`, and formatted `text` fields.

## Exit Criteria For Returning To Normal

- `live-reconcile --live` completes without new unknown-fill or stale submitted orders.
- New entries remain blocked until the operator confirms no local/CLOB drift.
- Latest market WebSocket book age is within `Settings.live_orderbook_cache_max_age_seconds` before relying on hot-path entries.
- The dashboard live positions, recent live orders, and CLOB state agree.
- `low-latency-readiness-report --require-evidence` has exited `0` and been archived with scheduler logs after any capped live readiness run.
