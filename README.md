# When It Rains

Latency-first Polymarket weather trading bot project.

Initial scope:

- Market: Hong Kong highest temperature only
- Source of truth: Hong Kong Observatory official daily maximum temperature
- Strategy: latency and event-driven repricing around HKO updates
- Default mode: local-first paper trading
- Live mode: scaffolded behind explicit CLI flags, environment gates, Keychain hot-key loading, pre-derived CLOB credentials, risk caps, and kill-switch settings

See [docs/hk-high-temp-latency-spec.md](docs/hk-high-temp-latency-spec.md) for the initial build spec.

## Local Testing

Create the repo venv and install dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Run tests:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
```

Initialize a database and collect live read-only data:

```bash
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 init-db
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 fetch-hko
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 discover-market 2026-05-04
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 fetch-orderbooks
```

Create a consistent SQLite backup. This uses SQLite's online backup API, so it is safer than copying the DB file while the bot may be writing:

```bash
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 backup-db
```

Backups are written to `data/backups/` by default. The latest 5 are kept; when a sixth backup is created, the oldest one is deleted.

Sample the HKO station forecast source every 10 minutes for 24 hours:

```bash
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 sample-ocf --interval-minutes 10 --hours 24
```

Try paper trade lifecycle commands:

```bash
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 calc-entry '25°C' YES 100
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 paper-buy '25°C' YES 100
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 check-exit '25°C' YES --take-profit 0.20
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 paper-sell '25°C' YES
```

Run the autonomous local paper loop once:

```bash
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 paper-loop --ticks 1
```

Run the polling-window scheduler:

```bash
PYTHONUNBUFFERED=1 PYTHONPATH=src .venv/bin/python -u -m whenitrains.cli --db data/whenitrains.sqlite3 paper-scheduler
```

The scheduler creates a startup DB backup by default. Use `--no-startup-backup` only for disposable test databases.
It polls learned HKO update windows, refreshes active Polymarket orderbooks, discovers current/future HK highest-temperature markets, and runs the paper decision pass every loop. AWS GIS latest readings are the priority actual source: HKO `TEMP`, `MAXTEMP`, and `MINTEMP` are stored as decimal current, since-midnight max, and since-midnight min observations. A dedicated AWS actual worker covers both the regular 5-minute reading timestamps and learned publish/fetchable minutes from HTTP `Last-Modified`; learned publish minutes are expanded into the matching 10-minute pattern and use a 2-minute buffer on each side at 10-second cadence, independent of orderbook and decision work. AWS GIS station forecast (`forecast/HKO.xml`) is the priority forecast source: the bot uses its full decimal hourly path first, then daily decimal max/min, and falls back to the older OCF station URL only if the AWS GIS forecast fetch fails. CSDI since-midnight and `rhrread` current weather are retained for observation/fallback evidence only and must not silently replace AWS GIS trading signals. Trading decisions use decimal signals only; rounded/display daily max values are display/audit data only.

Use verbose mode to print every scheduler tick and all orderbook bid/ask lines:

```bash
PYTHONUNBUFFERED=1 PYTHONPATH=src .venv/bin/python -u -m whenitrains.cli --db data/whenitrains.sqlite3 paper-scheduler --verbose
```

Replay a historical day into a scratch DB for policy backtesting:

```bash
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 backtest-day 2026-05-06
```

By default the harness uses historical scheduler decision timestamps as ticks and writes the replay DB to `/private/tmp/whenitrains-backtest-YYYY-MM-DD.sqlite3`. Use `--tick-source data` for data fetch timestamps, `--tick-source both` to combine them, `--include-orderbook-ticks` for denser data-driven replays, `--max-ticks` for smoke runs, and `--json` for machine-readable output.

Run an isolated experimental strategy backtest without mutating paper tables:

```bash
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 experiment-backtest-day 2026-05-06
```

Experimental runs write only to `experiment_*` tables in the replay DB and are intended for comparing future policy variants without changing the production paper scheduler.

Clear test paper trades without deleting market/HKO/orderbook history:

```bash
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 reset-paper --yes
```

`reset-paper` creates a backup first by default. Use `--no-backup` only for disposable test databases.

Inspect the terminal paper dashboard:

```bash
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 dashboard
```

Run the browser dashboard:

```bash
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 dashboard-serve
```

Open `http://127.0.0.1:8765/` for paper charts and `http://127.0.0.1:8765/live` for live order/position status.
The web dashboard uses HKT timestamps formatted as `YYYY-MM-DD HH:MM:SS`, keeps the x-axis scoped to the selected HKT date starting at midnight, and shows visible bot-signal bubbles so signal updates can be inspected without hunting for exact hover pixels. Open positions, realized PnL, and unrealized PnL are clickable summary tiles that switch the chart area to the underlying trade/activity table.

Live commands are fail-closed and require explicit live flags plus environment setup. See [docs/live-trading.md](docs/live-trading.md).
