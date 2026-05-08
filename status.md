# Live Trading Status

Last updated: 2026-05-08 HKT

## Current State

Live trading scaffolding is implemented behind explicit gates.

The project supports local-first paper trading with live HKO and Polymarket read-only data. Live mode now has additive storage, Keychain hot-key setup, pre-derived credential loading, a `live-env-exports` helper for shell-safe export lines from a local env file, manual FAK buy/sell, reconcile, cancel-one, cancel-all commands, kill-switch settings, live dashboard reporting, and live tick/scheduler command wiring. Paper trading remains the default.

Relevant existing implementation:

- Strategy/decision path: `src/whenitrains/runner.py`
- Paper execution: `src/whenitrains/paper_db.py`
- Market/orderbook client: `src/whenitrains/polymarket.py`
- Persistence/migrations: `src/whenitrains/storage.py`
- Scheduler: `src/whenitrains/scheduler.py`
- CLI: `src/whenitrains/cli.py`
- Live execution: `src/whenitrains/live.py`

Known local tree state at the time this status file was updated:

- There are existing uncommitted changes across live, scheduler, runner, dashboard, CLI, config, and storage code.
- The status/spec updates describe those changes without attempting to reset or overwrite them.

## Decisions

- Wallet strategy: dedicated Polymarket proxy wallet for the bot.
- Root of trust: Ledger or another hardware wallet remains treasury/cold storage.
- Hot key: dedicated bot private key on the isolated MacBook only.
- Hot-key storage target: macOS Keychain.
- Default Keychain service/account: `whenitrains-polymarket` / `bot-private-key`.
- API credentials: require pre-derived `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, and `POLYMARKET_API_PASSPHRASE` at runtime.
- Local env workflow: use `live-env-exports --env-file .env` to print shell-safe exports for the required live env vars without dumping unrelated env values.
- Runtime startup must not create or derive API credentials.
- Credential creation/derivation, if implemented, belongs in a separate explicit setup command such as `live-create-api-creds`.
- Signature type: start with `POLYMARKET_SIGNATURE_TYPE=1` for Polymarket proxy-wallet flow unless auth smoke proves otherwise.
- Funder: Polymarket proxy wallet address.
- Manual real-money smoke cap: `5 USD`.
- Initial live scheduler order cap: `20 USD`.
- Initial total open exposure cap: `200 USD`.
- Initial daily realized loss cap: `200 USD`.
- Order type: `FAK`.
- Resting orders: disabled in live v1; no `GTC` or `GTD`.
- Kill switch settings: `block_new_entries` and `cancel_open_orders_and_exit_positions`.
- Kill switch controls: persistent local state plus `data/KILL_SWITCH` emergency file plus explicit command flags.
- Emergency file only blocks new entries unless exit behavior is separately enabled.
- Live reporting: `/live` route and `/api/live/stats` exist in the dashboard server.

For v1, total open exposure means confirmed cost basis across all open live positions:

```text
total_open_exposure = sum(live_positions.net_shares * live_positions.avg_price)
```

## Milestones

### L1: Spec And Dependency Decision

Status: implemented

Deliverables:

- `docs/live-trading.md`
- Decision on official Python CLOB client package and pinned version.
- Decision on credential model.

Tests:

- No runtime tests required.
- Documentation reviewed against current Polymarket docs before implementation starts.

Exit criteria:

- Credential and hot-key storage decisions are reflected in implementation tasks.

### L2: Execution Adapter Abstraction

Status: implemented

Deliverables:

- Shared execution result dataclass.
- Paper execution adapter wrapping existing `execute_paper_buy` and `execute_paper_sell`.
- Runner accepts an execution adapter without changing strategy behavior.
- Paper scheduler remains default.

Tests:

- Existing full unit suite remains green.
- Adapter parity tests prove paper orders and positions are unchanged.
- Duplicate-position and budget checks still work.

Exit criteria:

- Paper mode behavior is unchanged except for intentional naming/interface refactors.

### L3: Live Schema And Storage Helpers

Status: implemented

Deliverables:

- `live_orders` table.
- `live_positions` table.
- Storage helpers for insert, update, reconcile, list open positions, and risk event persistence.
- Migration is additive only.

Tests:

- Migration creates live tables on a fresh DB.
- Migration creates live tables on an existing DB.
- Live storage helpers persist rejected, submitted, partially filled, filled, canceled, and error states.
- Live storage does not mutate paper tables.

Exit criteria:

- Additive migration tested only against `/private/tmp/*.sqlite3` until backed up production-like DB is ready.

### L4: Authenticated CLOB Client Wrapper

Status: implemented, pending real dependency/auth smoke

Deliverables:

- Wrapper around official Polymarket Python CLOB client.
- Env-based config loader.
- Preflight checks for env gate, credentials, host, chain ID, signature type, funder, balance, allowance, and open orders.
- Redacted logging for all credential-adjacent failures.
- Hot key loaded from macOS Keychain.
- Default Keychain service/account can be overridden by config.
- Pre-derived L2 API credentials loaded from env or a non-committed local secret source.
- Required live env values can be exported from a local env file with `live-env-exports`.

Tests:

- Missing env gate fails closed.
- Missing private key fails closed.
- Missing funder/signature type fails closed when required.
- Fake CLOB balance/allowance failure blocks live mode.
- Secret values never appear in printed errors.
- Env export helper prints only required live keys and fails closed when any required value is missing.

Exit criteria:

- `live-preflight` works with fake client and cannot submit orders.

### L5: Manual Live Commands With Fake Client

Status: implemented

Deliverables:

- `live-auth-smoke`
- `live-buy`
- `live-sell`
- `live-reconcile`
- `live-cancel-order`
- `live-cancel-all`
- Clear `LIVE TRADING` terminal banner on all authenticated commands.

Tests:

- Manual buy full-fill fake path.
- Manual buy partial-fill fake path.
- Manual buy rejection fake path.
- Manual sell full-fill fake path.
- Manual sell no-position rejection.
- Cancel order success/failure persistence.
- Cancel all success/failure persistence.

Exit criteria:

- Fake-client tests cover all manual live command outcomes.

### L6: Read-Only Real Auth Smoke

Status: pending credentials and installed CLOB dependency

Deliverables:

- Authenticated real CLOB client can initialize.
- Balance and allowance can be queried.
- Open orders can be queried.
- No order submission is possible in this command.

Tests:

- Manual read-only smoke with real credentials.
- Confirm command exits before order-creation code path.

Exit criteria:

- Real auth, balance, allowance, and open-order checks succeed.

### L7: Manual Real-Money Buy/Sell Smoke

Status: blocked on L6 and user approval

Deliverables:

- One tiny manual buy with worst-price protection.
- Immediate reconciliation.
- One manual sell to close.
- Immediate reconciliation.
- Raw responses persisted.

Tests:

- Real manual buy submitted and reconciled.
- Real manual sell submitted and reconciled.
- Local live position returns to expected size.
- No paper tables are changed.
- Manual order size is capped at `5 USD`.

Exit criteria:

- A complete real order lifecycle has been audited.

### L8: Mode-Aware Runner And Live Tick

Status: implemented

Deliverables:

- `run_trading_tick(db, today_hkt, execution_adapter)` or equivalent.
- `live-tick --live` command.
- Live duplicate-position checks use live positions or reconciled CLOB state.
- Paper tick remains unchanged.

Tests:

- Fake-client live tick places exactly one order for a deduped event.
- Repeated live tick does not duplicate the same event.
- Live tick blocks on critical risk events.
- Live tick blocks on kill switch.

Exit criteria:

- Bounded one-tick live mode works with fake client.

### L9: Live Scheduler

Status: implemented, pending real-auth smoke before use

Deliverables:

- `live-scheduler --live`.
- Startup backup.
- Single-process DB lock.
- Preflight.
- Reconcile before trading.
- Bounded tick option for trials.
- Scheduler order size is capped at `20 USD`.
- Total open exposure cap is enforced at `200 USD`.
- Daily realized loss cap is enforced at `200 USD`.

Tests:

- Scheduler fails closed without env gate.
- Scheduler fails closed without startup backup on production-like DB.
- Scheduler fails closed on lock contention.
- Scheduler fake-client trial places no duplicate orders.

Exit criteria:

- Live scheduler can run against fake client and bounded real-auth read-only mode.

### L10: Live Reporting And Runbook

Status: partially implemented

Deliverables:

- Dashboard or CLI summary distinguishes paper vs live.
- Live order and live position summary.
- Live PnL estimate from confirmed positions.
- Existing dashboard gets a live route.
- Operational runbook for startup, shutdown, kill switch, cancel all, and reconciliation.

Tests:

- Dashboard/CLI reports live positions without reading paper positions.
- Runbook commands smoke-tested against fake client.

Exit criteria:

- Operator can see live state and recover from ambiguous order status.

## Test Matrix

Required before any real order:

- Full unit test suite.
- Live schema tests.
- Fake CLOB client tests.
- Manual fake buy/sell/reconcile.
- Read-only real-auth smoke.

Required before live scheduler:

- All tests required before real order.
- One manual real-money round trip.
- Live tick fake-client dedupe tests.
- Scheduler startup/fail-closed tests.
- Kill switch tests.

Required before increasing size:

- Multiple reconciled manual or scheduler orders.
- No ambiguous fill states.
- No duplicate live orders for the same event key.
- Dashboard/status output verified.
- User-approved cap increase.

## Current Open Questions

No known product decisions remain before real-auth smoke. Real credentials, dependency installation, and Polymarket account-specific signature/funder validation are still required operationally.

## Latest Verification

Command:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

Result:

```text
Ran 100 tests in 0.708s
OK
```

Fail-closed CLI smoke:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db /private/tmp/whenitrains-live-cli.sqlite3 live-preflight
PYTHONPATH=src python3 -m whenitrains.cli --db /private/tmp/whenitrains-live-cli.sqlite3 live-tick
```

Both refused to run without `--live`.
