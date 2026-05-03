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

Try paper trade lifecycle commands:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 calc-entry '25°C' YES 100
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 paper-buy '25°C' YES 100
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 check-exit '25°C' YES --take-profit 0.03
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 paper-sell '25°C' YES
```
