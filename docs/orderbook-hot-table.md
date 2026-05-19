# Orderbook Hot Table Specification

## Goal

Keep live execution and operator summaries off the append-only `orderbook_snapshots`
history path. The scheduler can ingest high-frequency websocket and REST quotes
without requiring every latest-book read to sort or scan the historical table.

## Current Problem

`orderbook_snapshots` is both the historical archive and the latest execution
source. A live websocket run can create millions of rows, making latest-book
queries, dashboard summaries, and ad hoc health checks compete with historical
retention. The table is still valuable for charts, research, event-time
baselines, and replay, but it is too heavy to be the only source of current
state.

## Target Design

Add `orderbook_latest`, keyed by `outcome_id`.

Columns:

- `outcome_id text primary key`
- `snapshot_id integer`
- `fetched_at_utc text not null`
- `best_bid real`
- `best_ask real`
- `mid real`
- `depth_json text`

Write path:

- `store_orderbook()` inserts the full append-only row into
  `orderbook_snapshots`.
- The same call upserts `orderbook_latest` for that `outcome_id` only if the new
  snapshot is at least as recent as the existing latest row.
- Empty websocket books should not be persisted by the websocket cache, but if
  any caller intentionally stores an empty book, the hot table should represent
  that latest non-executable state.

Read path:

- Execution-facing latest-book reads use `orderbook_latest`.
- If `orderbook_latest` is empty for a token, readers fall back to
  `orderbook_snapshots` so existing databases keep working after migration
  before the next quote arrives.
- Historical charting, event-time baseline logic, backtests, and research keep
  using `orderbook_snapshots`.

## Non-Goals

- Do not delete, vacuum, or rewrite the production `data/` database in this
  milestone.
- Do not partition historical snapshots yet.
- Do not change dashboard historical charts to use the hot table.

## Verification

- Migration creates `orderbook_latest` and its lookup shape.
- Storing an orderbook writes both archive and hot rows.
- A newer stored book replaces the hot row; an older archive row cannot move the
  hot pointer backward.
- `latest_orderbook()` reads from `orderbook_latest` when present and falls back
  to `orderbook_snapshots` for pre-hot-table data.
