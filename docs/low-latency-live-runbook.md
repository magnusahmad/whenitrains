# Low-Latency Live Runbook

Last updated: 2026-05-11 HKT

## Scope

This runbook covers the low-latency live scheduler path for HK temperature markets. It assumes the scheduler is running against `data/whenitrains.sqlite3` and that live trading is explicitly enabled with `--live`. For the current non-LAN evidence workflow, also use `docs/low-latency-live-evidence-handoff.md`.

## Start

1. Confirm the working tree is on the intended branch and no unrelated database operation is in progress.
2. Confirm live credentials are available with `whenitrains live-env-exports`.
3. Generate the concrete live evidence command list for the target market before running any live-money command:

```bash
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 live-readiness-checklist --label <label> --side <YES-or-NO> --size-usd <minimum-size> --date <YYYY-MM-DD> --market-kind <highest-or-lowest> --scheduler-ticks 3
```

Archive this output with the scheduler logs. It is read-only and does not touch the database. Add `--live-log-url http://<live-host>:8765/` only when the evidence runner can reach the live log server; the generated checklist downloads the log to `/private/tmp/whenitrains-live-scheduler.log` and passes that file to the archive command. Otherwise keep the generated `--live-log-file <path-to-live-scheduler.log>` archive command and provide the copied log path. The generated archive command records `live_log_url` in `manifest.txt` when `--live-log-url` is supplied.
The checklist includes the real-account kill-switch verification sequence, a capped-scheduler log archive reminder for independent-candidate concurrency evidence, and a settlement-validation reminder for the first resolved live market.

4. Start or verify the log publisher before running live evidence commands.

On the live machine:

```bash
mkdir -p ~/whenitrains-live-logs
cd ~/whenitrains-live-logs
python3 -m http.server 8765 --bind 0.0.0.0
```

From a machine that can reach the live host:

```bash
curl -L http://<live-host>:8765/
```

If this workstation is not on the same LAN as the live machine, run the checklist on the live machine or copy the scheduler log into the evidence directory by another secure channel. When a reachable HTTP endpoint exists, pass it as `--live-log-url` so the probe, archive download command, and `manifest.txt` provenance match the actual source.

The readiness evidence run is not complete unless scheduler logs can be collected, archived as `live-scheduler.log`, and verified with the final evidence bundle.

5. Run a no-trade live network smoke to confirm both scheduler-owned WebSocket workers can start:

```bash
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 live-network-smoke --live --seconds 10 --require-connected
```

Expected output includes `live network smoke websocket_all_running=True`, at least two reported clients, per-client `connected_once=True` lines, and `live network smoke connected_once_all=True`; the command exits `0` only when runtime liveness, market/user client count, and connection evidence pass. This command starts and stops the market/user WebSocket runtime but does not run trading decisions.

6. Run a no-trade live auth smoke to confirm CLOB credentials, signer/funder addresses, balance, and allowance:

```bash
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 live-auth-smoke --live
```

Expected output includes `auth ok=True`, `required_balance_usd=...`, `allowance_ok=True`, signer/funder addresses, and exits `0`. This command performs preflight checks only and does not place orders.

7. Confirm there is no emergency entry block unless intentional:

```bash
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 live-kill-switch
```

8. Start the live scheduler:

```bash
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 live-scheduler --live --verbose
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
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 live-kill-switch --block-new-entries
```

To allow entries again after the issue is understood:

```bash
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 live-kill-switch --allow-new-entries
```

## Cancel All Open CLOB Orders

Use this when submitted order state is ambiguous or when shutting down under market stress:

```bash
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 live-cancel-all --live --yes-i-understand
```

Then run reconciliation.

## Reconcile

Use reconciliation after cancel-all, restart, WebSocket reconnect, or any suspected state drift:

```bash
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 live-reconcile --live
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
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 live-kill-switch --block-new-entries
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
- Targeted Polymarket book refresh failure during a live hot-path entry.
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
- At least one resolved-market live settlement row has been validated against CLOB/onchain state and archived.
- Record the validation with `live-settlement-validate --live --order-id <live-settlement-order-id> --reference <CLOB/onchain-reference>`.
- Archive capped live scheduler logs showing either independent candidate actions progressing concurrently or that no independent-candidate opportunity occurred during the smoke. The log must include both `live scheduler concurrency evidence ...` and `live scheduler smoke ok ...`; the final archive verifier rejects failed-smoke logs and logs without structured concurrency/no-opportunity evidence.
- Provide the capped scheduler log to the archive command as an explicit copied-log input:

```bash
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 low-latency-archive-evidence --output-dir data/low-latency-evidence/<run-id> --live-log-file <path-to-live-scheduler.log> --require-evidence
```

- Run `low-latency-readiness-db-audit` read-only before final readiness/archive commands; it should report nonzero evidence counts for latency traces, timed HKO raw snapshots, WebSocket orderbook snapshots, orderbook-age decisions, live orders, live user events, and risk-event smoke records.
- `low-latency-readiness-report --require-evidence` has exited `0` and been archived with scheduler logs after any capped live readiness run.
- Archive report artifacts with `low-latency-archive-evidence --output-dir data/low-latency-evidence/<run-id> --live-log-file <path-to-live-scheduler.log> --require-evidence`; the archive copies the provided scheduler log to `live-scheduler.log` and includes the read-only DB audit output alongside latency, HKO source timing, and readiness reports.
- Verify archived artifacts with `low-latency-verify-evidence-archive --input-dir data/low-latency-evidence/<run-id>`.
