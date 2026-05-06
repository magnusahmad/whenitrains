# When It Rains

Latency-first Polymarket weather trading bot project.

Initial scope:

- Market: Hong Kong highest temperature only
- Source of truth: Hong Kong Observatory official daily maximum temperature
- Strategy: latency and event-driven repricing around HKO updates
- Mode: local-first paper trading
- Later: automated live execution behind explicit configuration and risk controls

See [docs/hk-high-temp-latency-spec.md](docs/hk-high-temp-latency-spec.md) for the initial build spec.

## Local Testing

Run tests:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

Initialize a database and collect live read-only data:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 init-db
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 fetch-hko
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 discover-market 2026-05-04
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 fetch-orderbooks
```

Create a consistent SQLite backup. This uses SQLite's online backup API, so it is safer than copying the DB file while the bot may be writing:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 backup-db
```

Backups are written to `data/backups/` by default. The latest 5 are kept; when a sixth backup is created, the oldest one is deleted.

Sample the HKO OCF station forecast source every 10 minutes for 24 hours:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 sample-ocf --interval-minutes 10 --hours 24
```

Try paper trade lifecycle commands:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 calc-entry '25°C' YES 100
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 paper-buy '25°C' YES 100
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 check-exit '25°C' YES --take-profit 0.20
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 paper-sell '25°C' YES
```

Run the autonomous local paper loop once:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 paper-loop --ticks 1
```

Run the polling-window scheduler:

```bash
PYTHONUNBUFFERED=1 PYTHONPATH=src python3 -u -m whenitrains.cli --db data/whenitrains.sqlite3 paper-scheduler
```

The scheduler creates a startup DB backup by default. Use `--no-startup-backup` only for disposable test databases.

Use verbose mode to print every scheduler tick and all orderbook bid/ask lines:

```bash
PYTHONUNBUFFERED=1 PYTHONPATH=src python3 -u -m whenitrains.cli --db data/whenitrains.sqlite3 paper-scheduler --verbose
```

Clear test paper trades without deleting market/HKO/orderbook history:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 reset-paper --yes
```

`reset-paper` creates a backup first by default. Use `--no-backup` only for disposable test databases.

Inspect the paper dashboard:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 dashboard
```
