# Experiment Harness Status

## Goal

Build a safe iterative research loop for model-driven trading experiments:

1. tweak model parameters or features,
2. create a staged experimental scheduler using that model,
3. backtest through the CLI,
4. record realized PnL and diagnostics,
5. iterate.

Hard constraint: never alter the existing `paper-scheduler` behavior for experiments. Experimental logic must be isolated by module, config, tables, and CLI command.

## Milestone 1: Isolated Experiment Storage

Status: implemented in first slice.

Requirements:

- Add experiment-only tables:
  - `experiment_runs`
  - `experiment_decisions`
  - `experiment_orders`
  - `experiment_positions`
  - `experiment_metrics`
- Do not write to `paper_orders`, `paper_positions`, `paper_decisions`, or `signals`.
- Store full experiment config JSON with each run.
- Store run source DB, target date range, and timestamps.

Tests:

- Migration creates experiment tables.
- Running an experiment tick writes experiment rows only.
- Paper tables remain empty/unchanged.

## Milestone 2: Configurable Experimental Scheduler

Status: implemented in first slice.

Requirements:

- Add `whenitrains.experiments.experimental_scheduler`.
- Accept a model/strategy config object.
- Produce a standard decision contract:
  - target date
  - label
  - side
  - action
  - model probability / confidence where available
  - reason/details JSON
- Execute simulated orders into experiment tables only.
- Start with one baseline strategy:
  - forecast bucket YES is cheap enough.
  - This is intentionally simple; it proves the loop before autoresearcher adds complex models.
- Current baseline strategy:
  - `forecast_bucket_cheap_yes`
  - finds the latest OCF forecast max for the target date,
  - maps it to the matching market bucket,
  - buys YES when the visible ask is at or below the configured `max_entry_price`.

Tests:

- Cheap forecast bucket creates a filled experimental BUY when ask is below threshold.
- Expensive forecast bucket creates a skipped/rejected decision.
- Duplicate event keys do not rebuy the same signal repeatedly.

## Milestone 3: Experimental Backtest Runner

Status: implemented in first slice.

Requirements:

- Add `experiment-backtest-day` CLI command.
- Copy the source DB to a disposable replay DB.
- Clear replay data and experiment tables in replay DB.
- Replay historical data forward in timestamp order.
- Run the experimental scheduler at each tick.
- Return summary:
  - run ID
  - tick count
  - decision count
  - order count
  - filled order count
  - open position count
  - realized PnL
  - cost basis

Tests:

- Backtest replays a day and writes only experiment tables.
- Backtest result JSON is deterministic for a small seeded DB.
- Existing `backtest-day` still uses current paper scheduler and remains unchanged.

Command:

```bash
PYTHONPATH=src python3 -m whenitrains.cli \
  --db data/whenitrains.sqlite3 \
  experiment-backtest-day 2026-05-04 --json
```

Optional config:

```json
{
  "name": "forecast-cheap-v1",
  "strategy": "forecast_bucket_cheap_yes",
  "execution": {
    "max_order_usd": 250.0,
    "order_size_usd": 250.0,
    "min_fill_usd": 25.0,
    "max_entry_price": 0.30
  }
}
```

Use with:

```bash
PYTHONPATH=src python3 -m whenitrains.cli \
  --db data/whenitrains.sqlite3 \
  experiment-backtest-day 2026-05-04 --config path/to/config.json
```

## Milestone 4: Metrics and Comparison

Status: next.

Requirements:

- Add richer metrics:
  - realized PnL
  - mark-to-market unrealized PnL
  - total PnL
  - max drawdown
  - turnover
  - trade count
  - PnL by date, side, bucket, and strategy
- Add `experiment-compare` CLI to compare two run IDs or result JSON files.

Tests:

- Metrics use bid-side liquidation for open longs.
- Comparison reports deltas vs baseline.

## Milestone 5: Autoresearcher Integration

Status: future.

Requirements:

- Define an experiment config schema that autoresearcher can mutate.
- Add a fixed train/validation/test split manifest.
- Add guardrails:
  - no future data leakage,
  - minimum trade count,
  - drawdown penalty,
  - turnover penalty,
  - complexity penalty.
- Objective should not be raw PnL only.

Tests:

- Config validation rejects unknown strategies/features.
- Feature builder cannot read rows after the simulated timestamp.
- Re-running the same config on the same replay DB produces the same result.
