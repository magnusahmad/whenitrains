# Useful Commands

Operational command reference for the `whenitrains` CLI, database, dashboard,
paper trading, and guarded live order management.

Default production-like DB:

```bash
DB=data/whenitrains.sqlite3
```

Disposable smoke-test DB:

```bash
DB=/private/tmp/whenitrains-smoke.sqlite3
```

All examples below use the module entrypoint so they work without installing the
package script:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db "$DB" <command>
```

## Safety Rules

Never delete `data/`, `data/whenitrains.sqlite3`, `data/backups/`, or any
`*.sqlite3` file unless that exact destructive action is explicitly intended.

Create a SQLite online backup before migrations, state-clearing, storage changes,
or live scheduler startup checks:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 backup-db
```

Keep more or fewer backups:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 backup-db --keep 10
```

Write backups somewhere else:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 backup-db --backup-dir /private/tmp/whenitrains-backups
```

Use `reset-paper --yes` for paper-trading cleanup. It backs up first by default
and clears only paper orders, positions, decisions, and signals:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 reset-paper --yes
```

Use `--no-backup` only on disposable `/private/tmp/*.sqlite3` databases:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db /private/tmp/whenitrains-smoke.sqlite3 reset-paper --yes --no-backup
```

## Environment

Create and activate a venv:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install the package in editable mode:

```bash
python3 -m pip install -e .
```

Run the full unit suite:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

Show all CLI commands:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --help
```

## Database Setup And Data Fetching

Initialize or migrate a DB:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db "$DB" init-db
```

Fetch HKO observations and forecasts:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db "$DB" fetch-hko
```

Discover a HK temperature market for a date:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db "$DB" discover-market 2026-05-09
```

Fetch orderbooks for known outcomes:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db "$DB" fetch-orderbooks
```

One-shot bootstrap against the production-like DB:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 init-db
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 fetch-hko
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 discover-market 2026-05-09
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 fetch-orderbooks
```

Sample the OCF station forecast source every 10 minutes for 24 hours:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db "$DB" sample-ocf --interval-minutes 10 --hours 24
```

Fast one-shot OCF sampler smoke:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db /private/tmp/whenitrains-smoke.sqlite3 sample-ocf --interval-minutes 0 --ticks 1
```

## Paper Trading

Check whether an entry is fillable from current asks:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db "$DB" calc-entry '25°C' YES 100
PYTHONPATH=src python3 -m whenitrains.cli --db "$DB" calc-entry '25°C' NO 100
```

Manually paper-buy:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db "$DB" paper-buy '25°C' YES 100
```

Check exit conditions:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db "$DB" check-exit '25°C' YES --take-profit 0.20 --max-hold-minutes 10
```

Manually paper-sell:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db "$DB" paper-sell '25°C' YES
```

Run one autonomous paper tick with fresh data:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db "$DB" paper-tick
```

Run one autonomous paper tick using already-stored data:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db "$DB" paper-tick --no-fetch
```

Run a simple fixed-interval paper loop:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db "$DB" paper-loop --interval 15 --ticks 4
```

Run the production-style polling-window paper scheduler:

```bash
PYTHONUNBUFFERED=1 PYTHONPATH=src python3 -u -m whenitrains.cli --db data/whenitrains.sqlite3 paper-scheduler
```

Run a bounded verbose scheduler smoke:

```bash
PYTHONUNBUFFERED=1 PYTHONPATH=src python3 -u -m whenitrains.cli --db /private/tmp/whenitrains-smoke.sqlite3 paper-scheduler --ticks 1 --sleep 0 --verbose --no-startup-backup
```

## Dashboard

Print the terminal dashboard:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db "$DB" dashboard
```

Run the browser dashboard:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 dashboard-serve --host 127.0.0.1 --port 8765
```

Open these routes:

```text
http://127.0.0.1:8765/
http://127.0.0.1:8765/live
http://127.0.0.1:8765/historicals
```

Useful JSON endpoints:

```text
http://127.0.0.1:8765/api/stats
http://127.0.0.1:8765/api/live/stats
http://127.0.0.1:8765/api/historicals
```

If port `8765` is busy, use another port:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 dashboard-serve --port 8766
```

## Backtests And Research

Replay a historical day into a scratch replay DB:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 backtest-day 2026-05-06
```

Data-driven replay with denser orderbook ticks:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 backtest-day 2026-05-06 --tick-source data --include-orderbook-ticks
```

Bounded JSON smoke:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 backtest-day 2026-05-06 --max-ticks 50 --json
```

Run isolated experimental strategy replay without mutating paper tables:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 experiment-backtest-day 2026-05-06
```

Forecast accuracy report:

```bash
PYTHONPATH=src python3 -m whenitrains.cli research-forecast-accuracy --months 12
```

Hourly accuracy report from the local DB:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 research-hourly-accuracy
```

Write a report to a file:

```bash
PYTHONPATH=src python3 -m whenitrains.cli research-forecast-accuracy --start 2025-05-01 --end 2026-05-01 --output /private/tmp/forecast-accuracy.txt
```

## Live Trading Setup

Live mode is fail-closed. Authenticated commands require:

- `WHENITRAINS_TRADING_MODE=live`
- Keychain item containing the bot private key
- `POLYMARKET_SIGNATURE_TYPE=3`
- `POLYMARKET_FUNDER_ADDRESS`
- `POLYMARKET_API_KEY`
- `POLYMARKET_API_SECRET`
- `POLYMARKET_API_PASSPHRASE`

Optional defaults:

- `POLYMARKET_HOST=https://clob.polymarket.com`
- `POLYMARKET_CHAIN_ID=137`
- `WHENITRAINS_KEYCHAIN_SERVICE=whenitrains-polymarket`
- `WHENITRAINS_KEYCHAIN_ACCOUNT=bot-private-key`

Store the bot hot key in macOS Keychain:

```bash
PYTHONPATH=src python3 -m whenitrains.cli live-store-hot-key
```

Verify that the default Keychain item exists without printing the key:

```bash
security find-generic-password -s whenitrains-polymarket -a bot-private-key >/dev/null
```

Create a local `.env` file manually. Do not commit it:

```dotenv
WHENITRAINS_TRADING_MODE=live
POLYMARKET_SIGNATURE_TYPE=3
POLYMARKET_FUNDER_ADDRESS=0xYOUR_DEPOSIT_OR_PROXY_WALLET
POLYMARKET_API_KEY=YOUR_API_KEY
POLYMARKET_API_SECRET=YOUR_API_SECRET
POLYMARKET_API_PASSPHRASE=YOUR_API_PASSPHRASE
```

Optional `.env` values:

```dotenv
POLYMARKET_HOST=https://clob.polymarket.com
POLYMARKET_CHAIN_ID=137
WHENITRAINS_KEYCHAIN_SERVICE=whenitrains-polymarket
WHENITRAINS_KEYCHAIN_ACCOUNT=bot-private-key
```

The funder address is not the API key. For this bot's current proxy/deposit
wallet path, keep `POLYMARKET_SIGNATURE_TYPE=3` and set
`POLYMARKET_FUNDER_ADDRESS` to the bot's Polymarket deposit/proxy wallet address.
The repo does not currently discover or derive the funder address automatically.

Load live env vars from local `.env` into the current shell:

```bash
eval "$(PYTHONPATH=src python3 -m whenitrains.cli live-env-exports --env-file .env)"
```

Validate that `.env` contains every required value without exporting it:

```bash
PYTHONPATH=src python3 -m whenitrains.cli live-env-exports --env-file .env
```

Check exactly which required live vars are currently exported:

```bash
env | sort | rg '^(WHENITRAINS_TRADING_MODE|POLYMARKET_)='
```

The code intentionally does not create or derive API credentials during
`live-preflight`, `live-tick`, or `live-scheduler`. Pull or refresh L2 API
credentials as an explicit setup step, then paste the resulting API key, secret,
and passphrase into `.env`.

Recover existing L2 API credentials from the Keychain hot key using the default
nonce:

```bash
PYTHONPATH=src python3 - <<'PY'
from whenitrains.live import read_keychain_secret
from whenitrains.config import Settings
from py_clob_client_v2 import ClobClient

private_key = read_keychain_secret(
    Settings.live_keychain_service,
    Settings.live_keychain_account,
)
if not private_key:
    raise SystemExit("missing Keychain hot key")

client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key=private_key,
)
creds = client.derive_api_key()
api_key = getattr(creds, "api_key", None) or getattr(creds, "apiKey", None)
api_secret = getattr(creds, "api_secret", None) or getattr(creds, "secret", None)
api_passphrase = getattr(creds, "api_passphrase", None) or getattr(creds, "passphrase", None)
print("POLYMARKET_API_KEY=" + api_key)
print("POLYMARKET_API_SECRET=" + api_secret)
print("POLYMARKET_API_PASSPHRASE=" + api_passphrase)
PY
```

Create or derive credentials on first setup:

```bash
PYTHONPATH=src python3 - <<'PY'
from whenitrains.live import read_keychain_secret
from whenitrains.config import Settings
from py_clob_client_v2 import ClobClient

private_key = read_keychain_secret(
    Settings.live_keychain_service,
    Settings.live_keychain_account,
)
if not private_key:
    raise SystemExit("missing Keychain hot key")

client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key=private_key,
)
creds = client.create_or_derive_api_key()
api_key = getattr(creds, "api_key", None) or getattr(creds, "apiKey", None)
api_secret = getattr(creds, "api_secret", None) or getattr(creds, "secret", None)
api_passphrase = getattr(creds, "api_passphrase", None) or getattr(creds, "passphrase", None)
print("POLYMARKET_API_KEY=" + api_key)
print("POLYMARKET_API_SECRET=" + api_secret)
print("POLYMARKET_API_PASSPHRASE=" + api_passphrase)
PY
```

If the original nonce is lost and derivation cannot recover credentials, create
fresh credentials and immediately update `.env`. Creating a new API key can
invalidate the previous active key for that wallet.

```bash
PYTHONPATH=src python3 - <<'PY'
from whenitrains.live import read_keychain_secret
from whenitrains.config import Settings
from py_clob_client_v2 import ClobClient

private_key = read_keychain_secret(
    Settings.live_keychain_service,
    Settings.live_keychain_account,
)
if not private_key:
    raise SystemExit("missing Keychain hot key")

client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key=private_key,
)
creds = client.create_api_key()
api_key = getattr(creds, "api_key", None) or getattr(creds, "apiKey", None)
api_secret = getattr(creds, "api_secret", None) or getattr(creds, "secret", None)
api_passphrase = getattr(creds, "api_passphrase", None) or getattr(creds, "passphrase", None)
print("POLYMARKET_API_KEY=" + api_key)
print("POLYMARKET_API_SECRET=" + api_secret)
print("POLYMARKET_API_PASSPHRASE=" + api_passphrase)
PY
```

After updating `.env`, reload and run read-only checks:

```bash
eval "$(PYTHONPATH=src python3 -m whenitrains.cli live-env-exports --env-file .env)"
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 live-preflight --live
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 live-auth-smoke --live
```

Read-only authenticated preflight:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 live-preflight --live
```

Read-only auth smoke:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 live-auth-smoke --live
```

## Live Order Management

Manual live buys are capped by config at `5 USD` and require both `--live` and
`--yes-i-understand`:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 live-buy '25°C' YES 5 --live --yes-i-understand
```

Disambiguate by target date and market kind when labels overlap:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 live-buy '25°C' YES 5 --date 2026-05-09 --market-kind highest --live --yes-i-understand
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 live-buy '24°C' YES 5 --date 2026-05-09 --market-kind lowest --live --yes-i-understand
```

Manual live sell:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 live-sell '25°C' YES --date 2026-05-09 --market-kind highest --live --yes-i-understand
```

Reconcile submitted live orders and rebuild local live positions from filled
orders. This mutates `live_orders` and `live_positions`, so create the standard
SQLite backup first when running against `data/whenitrains.sqlite3`:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 backup-db
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 live-reconcile --live
```

`live-reconcile` is the recovery path for delayed or ambiguous CLOB fills. It
normalizes matched order statuses, including uppercase `MATCHED`, and uses exact
`makingAmount` / `takingAmount` fields when present. Matched orders without
exact fill amounts stay `unknown_fill` until trade history or token-balance
evidence is available.

Cancel one live order:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 live-cancel-order ORDER_ID --live --yes-i-understand
```

Cancel all open live orders:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 live-cancel-all --live --yes-i-understand
```

Block new live entries:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 live-kill-switch --block-new-entries
```

Allow new live entries again:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 live-kill-switch --allow-new-entries
```

Enable kill-switch exit behavior:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 live-kill-switch --exit-on-kill-switch
```

Disable kill-switch exit behavior:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 live-kill-switch --no-exit-on-kill-switch
```

Emergency file behavior: `data/KILL_SWITCH` blocks new entries on the next live
scheduler tick. It does not imply exits unless exit behavior is separately
enabled.

```bash
touch data/KILL_SWITCH
```

Remove the emergency file only when intentionally re-enabling live entries:

```bash
rm data/KILL_SWITCH
```

Run one live tick:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 live-tick --live
```

Run one live tick using already-stored data:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 live-tick --live --no-fetch
```

Run the guarded live scheduler:

```bash
PYTHONUNBUFFERED=1 PYTHONPATH=src python3 -u -m whenitrains.cli --db data/whenitrains.sqlite3 live-scheduler --live
```

Bounded live scheduler trial:

```bash
PYTHONUNBUFFERED=1 PYTHONPATH=src python3 -u -m whenitrains.cli --db data/whenitrains.sqlite3 live-scheduler --live --ticks 1 --verbose
```

## Live Scheduler With LAN Logs

Use this when starting the live scheduler on the live machine and publishing logs
so another machine on the same LAN can inspect them.

Terminal 1 on the live machine: publish the log directory.

```bash
mkdir -p ~/whenitrains-live-logs
cd ~/whenitrains-live-logs
python3 -m http.server 8765 --bind 0.0.0.0
```

Terminal 2 on the live machine: load live env, start the scheduler, and tee
output into the published directory.

```bash
cd /Users/magnus/Documents/Projects/whenitrains
eval "$(PYTHONPATH=src python3 -m whenitrains.cli live-env-exports --env-file .env)"

LOG="$HOME/whenitrains-live-logs/live-scheduler-$(date -u +%Y%m%d-%H%M%S).log"

PYTHONUNBUFFERED=1 PYTHONPATH=src python3 -u -m whenitrains.cli \
  --db data/whenitrains.sqlite3 \
  live-scheduler --live --verbose 2>&1 | tee -a "$LOG"
```

From this machine or another LAN machine: list, download, and inspect logs.

```bash
curl -L http://192.168.1.23:8765/
curl -L -o /private/tmp/live-scheduler-latest.log http://192.168.1.23:8765/<log-file-name>
tail -n 120 /private/tmp/live-scheduler-latest.log
```

## Read-Only SQLite Inspection

Prefer read-only SQLite queries for inspection. Do not edit the production-like
DB directly unless there is an explicit migration or recovery plan and a fresh
backup exists.

Open SQLite:

```bash
sqlite3 data/whenitrains.sqlite3
```

List tables:

```sql
.tables
```

Show schema for a table:

```sql
.schema paper_orders
```

Recent paper orders:

```sql
select id, created_at_utc, outcome_id, side, status, simulated_fill_price,
       simulated_fill_size_usd, reason
from paper_orders
order by id desc
limit 20;
```

Open paper positions:

```sql
select outcome_id, net_shares, avg_price, realized_pnl, updated_at_utc
from paper_positions
where abs(net_shares) > 0.000001
order by updated_at_utc desc;
```

Recent live orders:

```sql
select id, created_at_utc, outcome_id, label, side, action, status, fill_price,
       fill_size_usd, fill_shares, clob_order_id, reason, error
from live_orders
order by id desc
limit 20;
```

Open live positions:

```sql
select outcome_id, net_shares, avg_price, realized_pnl, updated_at_utc,
       last_reconciled_at_utc
from live_positions
where abs(net_shares) > 0.000001
order by updated_at_utc desc;
```

Live kill-switch settings:

```sql
select name, value, updated_at_utc
from live_settings
order by name;
```

Recent risk events:

```sql
select id, created_at_utc, severity, event_type, details_json
from risk_events
order by id desc
limit 50;
```

Known markets and outcome labels:

```sql
select m.target_date_hkt, m.slug, o.label, o.yes_token_id, o.no_token_id
from outcomes o
join markets m on m.id = o.market_id
order by m.target_date_hkt desc, o.predicate_value_c, o.label
limit 100;
```

Latest orderbook snapshot per token:

```sql
select token_id, max(fetched_at_utc) as latest_fetch
from orderbook_snapshots
group by token_id
order by latest_fetch desc
limit 50;
```

## Live Logs

The live machine publishes scheduler logs over the LAN:

```bash
curl -L http://192.168.1.23:8765/
```

Download a log:

```bash
curl -L -o /private/tmp/live-scheduler-latest.log http://192.168.1.23:8765/<log-file-name>
```

Search for important events:

```bash
rg -n -i "LIVE|preflight|failed|error|not enough|insufficient|balance|allowance|block_new_entries|buy|sell|filled|submitted|request error|live-scheduler actions" /private/tmp/live-scheduler-latest.log
```

Tail the latest downloaded log:

```bash
tail -n 120 /private/tmp/live-scheduler-latest.log
```

## Process Checks

Find dashboard or scheduler processes:

```bash
ps -axo pid,ppid,stat,etime,command | rg "whenitrains.cli|paper-scheduler|live-scheduler|dashboard-serve"
```

Inspect Python processes:

```bash
ps -axo pid,ppid,stat,etime,command | rg "python3 .*whenitrains"
```
