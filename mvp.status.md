# Live Trading Status

Last updated: 2026-05-14 HKT

## Current State

Live trading scaffolding is implemented behind explicit gates.

The project supports local-first paper trading with live HKO and Polymarket read-only data. Live mode now has additive storage, Keychain hot-key setup, pre-derived credential loading, a `live-env-exports` helper for shell-safe export lines from a local env file, manual FAK buy/sell, reconcile, cancel-one, cancel-all commands, kill-switch settings, live dashboard reporting, and live tick/scheduler command wiring. Paper trading remains the default.

Live preflight now interprets raw pUSD micro-unit balance/allowance payloads from the CLOB, requires enough available balance for the scheduler cap before `live-tick`/`live-scheduler`, and automatically sets `block_new_entries` after three consecutive CLOB live-buy rejections for insufficient balance or allowance.

The dashboard now includes a `/historicals` route for historical HKO accuracy review. It exposes `/api/historicals` with separate max-temperature and min-temperature series: OCF forecast error versus the actual daily extreme timestamp, forecast-bucket YES token prices versus the same lead-hours axis, lead-hour aggregate mean-error stats, and paper PNL histograms grouped by signal reason and D+0/D+1/D+N entry timing.

Dashboard forecast charts now initialize each high/low lead panel to the same HKT midnight-to-current-time x-axis for the panel's target date instead of auto-fitting sparse market data. Rendered chart series are first filtered to the latest source/update HKT day, then projected onto the panel target date while preserving intraday times. Each chart includes a hidden minute-by-minute time scaffold for the full target HKT day, so panning into sparse or empty future hours preserves the same time spacing instead of collapsing gaps such as 14:00-to-20:00. This gives D+1/D+2 the same initial viewport behavior as D+0, prevents prior-day points from leaking into D+0 hover labels after refresh, keeps D+1/D+2 date labels on their target dates, and preserves manual pan/zoom after the first render.

The root-level `useful-commands.md` now collects frequently needed CLI, DB inspection, dashboard, paper-trading, live-order, kill-switch, live-log, and process-check commands. It keeps production-like DB safety rules and backup/reset guidance close to the operational examples.

Scheduler startup backups now use a freshness gate instead of always creating a full SQLite copy. Paper and live schedulers ensure a backup newer than 6 hours by default, reuse a fresh existing backup when available, and still allow `--startup-backup-min-interval-minutes 0` to force the previous fresh-copy behavior. The explicit `backup-db` command remains fresh-by-default and adds `--if-older-than-minutes` for opt-in recent-backup reuse.

Spec and milestone-file governance is now documented in `docs/specs.md` and `docs/milestone-files.md`, and `AGENTS.md` points future agents at those files for spec location, status-file discipline, milestone structure, and session-end updates.

Live dashboard trade rows now normalize filled order notional from `fill_price * fill_shares` whenever CLOB/API storage has a zero or missing `fill_size_usd`, so the USD column, realized PnL, unrealized PnL, and chart markers use the same cash-flow basis. Live dashboard open-position reporting now replays filled live orders instead of trusting persisted `live_positions.avg_price`, and open trade drilldowns show only remaining open buy-lot shares so table uPnL adds up to the summary.

Live invalidation exits now cap submitted sell shares to the CLOB-reported conditional token balance when that balance is lower than local live position shares. This avoids rejected all-or-nothing FAK exits when the local live replay overstates sellable shares, while recording a `live_position_balance_mismatch` warning risk event so the accounting mismatch remains visible.

Live entries now refresh the CLOB orderbook immediately before submitting a live buy, persist that fresh quote, and re-apply the entry cap/slippage rule against it. If the fresh quote has moved beyond the executable rule that produced the candidate, the buy is recorded as missed instead of sending a stale-price FAK order.

Live buys now size down to available visible ask depth within the slippage cap instead of requiring enough depth for the full requested order. A scheduler buy that requests 20 USD can submit a smaller FAK, such as 3 USD, when only that much depth is executable, while still enforcing a 1 USD minimum live entry fill.

Live sell misses now signpost their reason in scheduler notes, including the label, side, trigger, and bid. Open-position exits also check whether a position is actually invalidated before counting missing bid depth as a sell miss, so `sells=0/N` no longer includes non-actionable held positions with thin books.

Live balance mismatches now write explicit local reconciliation adjustments. When CLOB reports fewer sellable conditional tokens than the local ledger, the missing shares are consumed by a `RECONCILE_SELL` ledger row with zero proceeds and a risk event, so open-position reporting and rebuilt live positions stop showing phantom sellable shares without pretending an exchange sell filled.

Live buys now reconcile reported fills against the wallet's conditional-token balance delta. If CLOB exposes pre/post token balances and the received token delta is smaller than the reported fill, the local fill is capped to the observed delta; if no tokens arrive, the order is marked `unknown_fill` and no local position is opened.

The live dashboard now runs the live-order reconcile path before serving live stats, forecast panels, PnL, and trade drilldowns. This lets submitted or `unknown_fill` orders become filled live orders and rebuilt open positions through ordinary dashboard refreshes, instead of requiring a separate manual `live-reconcile` before the dashboard can show overnight fills.

Forecast-panel trade markers now include traded live/paper tokens even when the token is missing from the latest orderbook candidate rows. Marker-only traded tokens fall back to their latest fill price, so B/S chart bubbles remain visible after fills on tokens that no longer have a fresh orderbook snapshot.

Live scheduler and live tick startup preflight now distinguish entry capacity from exit capability. Low pUSD cash balance, insufficient entry allowance, or `block_new_entries` can still prevent new buys, but they no longer stop the process before open-position exit checks can submit sells.

Live scheduler buy sizing is reduced to a `5 USD` per-order cap while the strategy proves consistent profitability.

Scheduler logs now print a loud `💰 TRADE EXECUTED 💰` line whenever a tick records filled buys or sells, including filled buy/sell counts and tick notes.

AWS GIS actual readings remain enabled for low-latency current temperature and extrema, but `MAXTEMP`/`MINTEMP` from exactly `00:00 HKT` are treated as previous-day rollover extrema and are not stored as same-day since-midnight max/min values. This prevents a midnight carryover such as `MAXTEMP=26.1` from triggering current-day actual-cross buys.

Relevant existing implementation:

- Strategy/decision path: `src/whenitrains/runner.py`
- Paper execution: `src/whenitrains/paper_db.py`
- Market/orderbook client: `src/whenitrains/polymarket.py`
- Persistence/migrations: `src/whenitrains/storage.py`
- Scheduler: `src/whenitrains/scheduler.py`
- CLI: `src/whenitrains/cli.py`
- Live execution: `src/whenitrains/live.py`
- Dashboard and historicals route: `src/whenitrains/dashboard_server.py`

Known local tree state at the time this status file was updated:

- There are existing uncommitted changes across live, scheduler, runner, dashboard, CLI, config, and storage code.
- The status/spec updates describe those changes without attempting to reset or overwrite them.

Session verification on 2026-05-10 HKT:

- Red/green test added: `test_live_dashboard_reconcile_makes_submitted_fill_visible`.
- Red/green test added: `test_parse_aws_gis_midnight_extremes_are_previous_day`.
- Red/green test added: `test_live_forecast_panel_keeps_trade_markers_without_orderbook`.
- Red/green tests added: `test_preflight_can_skip_entry_capacity_for_exit_only_scheduler_startup` and `test_preflight_can_skip_entry_block_for_exit_only_scheduler_startup`.
- Red/green test added: `test_live_scheduler_buy_cap_is_five_usd`.
- Red/green test added: `test_scheduler_prints_loud_trade_log_for_live_fills`.
- `PYTHONPATH=src python3 -m unittest tests.test_dashboard_server` passes.
- `PYTHONPATH=src python3 -m unittest tests.test_hko` passes.
- `PYTHONPATH=src python3 -m unittest tests.test_live` passes.
- `PYTHONPATH=src python3 -m unittest tests.test_scheduler` passes.
- Browser visual check completed against `http://127.0.0.1:8788/live` using a temporary `/private/tmp` SQLite DB.
- Browser visual check completed against `http://127.0.0.1:8789/live` using a temporary `/private/tmp` SQLite DB with a marker-only live trade; one visible `B` bubble rendered with the expected title.
- `curl -L http://127.0.0.1:8788/api/live/stats` returned a valid live payload.

Session verification on 2026-05-14 HKT:

- Red/green test added: `test_execute_live_buy_sizes_down_to_visible_depth`.
- Red/green test added: `test_execute_live_buy_rejects_depth_below_live_minimum_after_sizing_down`.
- Red/green tests added: `test_ensure_recent_sqlite_backup_reuses_fresh_backup`, `test_ensure_recent_sqlite_backup_creates_when_backup_is_too_old`, `test_backup_db_if_older_reuses_recent_backup`, `test_paper_scheduler_reuses_fresh_startup_backup_by_default`, and `test_paper_scheduler_zero_startup_backup_interval_forces_backup`.
- `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_live.py'` passes.
- `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_paper.py'` passes.
- `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_runner.py' -k 'forecast_value' -k 'actual_cross'` passes.
- `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_storage.py' -k 'ensure_recent_sqlite_backup'` passes.
- `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_cli.py' -k 'backup_db_if_older' -k 'paper_scheduler_reuses' -k 'paper_scheduler_zero'` passes.
- `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_storage.py'` passes: 15 tests.
- `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_cli.py'` passes: 43 tests.
- `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests` passes: 512 tests.

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
- Signature type: use `POLYMARKET_SIGNATURE_TYPE=3` for this Polymarket proxy-wallet flow.
- Funder: Polymarket proxy wallet address.
- Manual real-money smoke cap: `5 USD`.
- Live scheduler order cap: `5 USD`.
- Initial total open exposure cap: `200 USD`.
- Initial daily realized loss cap: `200 USD`.
- Order type: `FAK`.
- Resting orders: disabled in live v1; no `GTC` or `GTD`.
- Kill switch settings: `block_new_entries` and `cancel_open_orders_and_exit_positions`.
- Kill switch controls: persistent local state plus `data/KILL_SWITCH` emergency file plus explicit command flags.
- Emergency file only blocks new entries unless exit behavior is separately enabled.
- Live reporting: `/live` route and `/api/live/stats` exist in the dashboard server.
- Historical reporting: `/historicals` is read-only and uses existing observation, OCF sample, orderbook, outcome, paper decision, and paper order tables.
- Historical max-temp accuracy uses the highest stored HKO current-temperature reading for a date as the actual max, and the earliest timestamp with that max reading as the actual max time.
- Historical min-temp accuracy uses the lowest stored HKO current-temperature reading for a date as the actual min, and the earliest timestamp with that min reading as the actual min time.
- Forecast token price uses the matching highest-temperature or lowest-temperature market YES token whose predicate bucket matches `floor(forecast_c)`, priced from the latest best ask at or before the forecast issue time.
- Historical lead-hour charts exclude forecast observations published after the actual daily extreme has already occurred.
- PNL historical grouping attributes closed paper-trade lots to the nearest filled paper decision reason when available, otherwise the buy order reason.

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
- Scheduler/tick preflight checks the scheduler order cap and decodes raw micro-unit CLOB balance/allowance payloads.
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
- Three consecutive CLOB insufficient balance/allowance submit failures set `block_new_entries`.
- Secret values never appear in printed errors.
- Env export helper prints only required live keys and fails closed when any required value is missing.
- Live scheduler buys fetch a fresh orderbook just before execution and reject stale candidate prices that no longer satisfy the entry rule.

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
- Scheduler order size is capped at `5 USD`.
- Total open exposure cap is enforced at `200 USD`.
- Daily realized loss cap is enforced at `200 USD`.

Tests:

- Scheduler fails closed without env gate.
- Scheduler fails closed without a usable startup backup on production-like DB.
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

### L11: Historical HKO Accuracy Dashboard

Status: implemented

Deliverables:

- `/historicals` HTML dashboard route.
- `/api/historicals` JSON payload.
- Max and min forecast error points by hours before the actual daily extreme timestamp.
- Max and min forecast bucket YES token prices by hours before the actual daily extreme timestamp.
- Max and min lead-hour aggregate mean-error stats.
- Max and min PNL performance histograms by signal reason and D+0/D+1/D+N entry timing.

Tests:

- Historical payload verifies OCF max forecast error against the final actual max and max timestamp.
- Historical payload verifies OCF min forecast error against the final actual min and min timestamp.
- Historical payload verifies max and min forecast bucket token-price matching at or before forecast issue time.
- Historical payload verifies closed paper PNL percent gain/loss grouping by signal reason and day offset.
- Historical HTML verifies route-specific API and chart containers.
- Full unit suite remains green.

Exit criteria:

- `/historicals` loads successfully in the dashboard and visual checks show charts/stats without obvious layout problems.

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
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
```

Result:

```text
Ran 213 tests in 2.422s
OK
```

Live exit sellable-balance cap red/green:

```bash
PYTHONPATH=src python3 -m unittest tests.test_live.LiveTests.test_execute_live_sell_caps_to_clob_sellable_balance
```

Red result: live exit submitted the full local `372.66` shares even though the fake CLOB balance exposed only `255.361958` sellable shares.

Green result after capping sell size to the CLOB token balance and recording a mismatch risk event:

```text
Ran 1 test in 0.011s
OK
```

Dashboard visual check:

```text
Opened http://127.0.0.1:8766/historicals with Browser Use against data/whenitrains.sqlite3.
The historical stats and SVG charts rendered with separate max/min sections, numeric hours-before-extreme axes ending at `0h`, scatter-plus-median-trend price/error views, and vertical PNL histogram SVGs. Browser screenshot capture timed out on one long-page capture, but DOM inspection confirmed both max and min sections and the histogram SVGs were present.
```

Fail-closed CLI smoke:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db /private/tmp/whenitrains-live-cli.sqlite3 live-preflight
PYTHONPATH=src python3 -m whenitrains.cli --db /private/tmp/whenitrains-live-cli.sqlite3 live-tick
```

Both refused to run without `--live`.
