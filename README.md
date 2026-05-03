# When It Rains

Latency-first Polymarket weather trading bot project.

Initial scope:

- Market: Hong Kong highest temperature only
- Source of truth: Hong Kong Observatory official daily maximum temperature
- Strategy: latency and event-driven repricing around HKO updates
- Mode: local-first paper trading
- Later: automated live execution behind explicit configuration and risk controls

See [docs/hk-high-temp-latency-spec.md](docs/hk-high-temp-latency-spec.md) for the initial build spec.
