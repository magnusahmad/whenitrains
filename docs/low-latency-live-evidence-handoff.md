# Low-Latency Live Evidence Handoff

Last updated: 2026-05-11 HKT

## Scope

This handoff is for completing the remaining low-latency readiness exit criteria from a live machine or another runner that can access the live SQLite DB, CLOB credentials, and scheduler logs. This development workstation is no longer on the same LAN as the live machine, so it should not be used to fetch `192.168.1.x:8765` scheduler logs.

## Required Live Inputs

- Live DB: `data/whenitrains.sqlite3`.
- Live credentials loaded by `live-env-exports`.
- Explicit approval for minimum-size manual live buy/sell.
- Capped scheduler log from the same evidence run.
- A reachable `--live-log-url` only if the evidence runner can actually fetch it; otherwise use `--live-log-file <path-to-live-scheduler.log>`.

## Command Plan

Generate the exact command list on the live machine:

```bash
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 live-readiness-checklist --label <label> --side <YES-or-NO> --size-usd <minimum-size> --date <YYYY-MM-DD> --market-kind <highest-or-lowest> --scheduler-ticks 3
```

If a reachable log host exists, add:

```bash
--live-log-url http://<reachable-live-host>:8765/
```

Follow the generated checklist in order. The archive command must include the capped scheduler log:

```bash
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 low-latency-archive-evidence --output-dir data/low-latency-evidence/<run-id> --live-log-file <path-to-live-scheduler.log> --require-evidence
PYTHONPATH=src .venv/bin/python -m whenitrains.cli low-latency-verify-evidence-archive --input-dir data/low-latency-evidence/<run-id>
```

## Passing Evidence Criteria

The final archive verifier must exit `0`. The archive must include:

- Passing `low-latency-readiness-report --require-evidence`.
- Nonzero production latency samples for commit-to-decision, decision-to-submit, CLOB ack, fill-match, fill-confirm or reject, and live CLOB drift-scan timing.
- Timed HKO raw snapshots with response timing and public-availability clustered fetches.
- WebSocket orderbook snapshots with usable bid/ask/depth and fresh orderbook-age evidence.
- Live network/auth/scheduler smoke records whose latest status is OK.
- Manual live buy and sell rows with positive fill size or shares.
- User-channel trade evidence with applied position delta.
- Reconciled live orders with non-empty CLOB/REST reconcile payloads.
- Clear live CLOB drift scan evidence.
- Persistent kill-switch verification ending allowed/clear.
- Filled live settlement row plus `live-settlement-validate --live` evidence with a non-empty external reference.
- `live-scheduler.log` containing successful capped scheduler smoke and structured independent-candidate concurrency or no-opportunity evidence.

## Current Blocker

On this development machine, the current production-like DB still fails readiness because the live/account evidence rows and latency samples are absent. The next meaningful progress must come from running the checklist in the live environment and copying back the resulting evidence archive.
