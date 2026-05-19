# Live Trading Status

Last updated: 2026-05-19 HKT

## Current State

Live trading scaffolding is implemented behind explicit gates.

The project supports local-first paper trading with live HKO and Polymarket read-only data. Live mode now has additive storage, Keychain hot-key setup, pre-derived credential loading, a `live-env-exports` helper for shell-safe export lines from a local env file, manual FAK buy/sell, reconcile, cancel-one, cancel-all commands, kill-switch settings, live dashboard reporting, and live tick/scheduler command wiring. Paper trading remains the default.

Live preflight now interprets raw pUSD micro-unit balance/allowance payloads from the CLOB, requires enough available balance for the scheduler cap before `live-tick`/`live-scheduler`, and automatically sets `block_new_entries` after three consecutive CLOB live-buy rejections for insufficient balance or allowance.

The dashboard now includes a `/historicals` route for historical HKO accuracy review. It exposes `/api/historicals` with separate max-temperature and min-temperature series: OCF forecast error versus the actual daily extreme timestamp, forecast-bucket YES token prices versus the same lead-hours axis, lead-hour aggregate mean-error stats, and paper PNL histograms grouped by signal reason and D+0/D+1/D+N entry timing.

Dashboard forecast charts now initialize each high/low lead panel to the same HKT midnight-to-current-time x-axis for the panel's target date instead of auto-fitting sparse market data. Rendered chart series are first filtered to the latest source/update HKT day, then projected onto the panel target date while preserving intraday times. Each chart includes a hidden minute-by-minute time scaffold for the full target HKT day, so panning into sparse or empty future hours preserves the same time spacing instead of collapsing gaps such as 14:00-to-20:00. This gives D+1/D+2 the same initial viewport behavior as D+0, prevents prior-day points from leaking into D+0 hover labels after refresh, keeps D+1/D+2 date labels on their target dates, and preserves manual pan/zoom after the first render.

The root-level `useful-commands.md` now collects frequently needed CLI, DB inspection, dashboard, paper-trading, live-order, kill-switch, live-log, and process-check commands. It keeps production-like DB safety rules and backup/reset guidance close to the operational examples.

Operational review on 2026-05-15 HKT found a live accounting split between local positions and the Polymarket wallet. The May 15 lowest-temperature 26°C YES order bought 40.33339 shares at 21c, sold 5.58 shares at 7.3c, sold 15.52 shares at 7.2c, then recorded a zero-proceeds `RECONCILE_SELL` for another 15.52 shares while the wallet still reported those 15.52 shares as sellable. The bot later sold 3.71 more shares at 7.1c, leaving the wallet with about 15.52339 shares, but local `live_positions` only shows dust and realized PnL of about -$6.68 for that token. This makes the dashboard overstate realized losses and understate remaining position value. The live reconcile watchdog is correctly freezing new entries because it sees the drift, but the repair path should not consume local shares when CLOB sellable balance is lower due to in-flight or recently matched sell orders.

The zero-proceeds live accounting bug is fixed. Live sell execution still caps submitted shares to the CLOB-reported sellable balance and records a warning risk event, but no longer writes a zero-proceeds `RECONCILE_SELL` row or reduces local position shares for the unexplained difference. Drift repair now leaves positions unchanged so the watchdog freezes new entries until the drift clears through real fills or explicit operator action. Live position rebuilds and dashboard order replay ignore only legacy zero-proceeds `RECONCILE_SELL` rows whose reconcile payload still had positive CLOB sellable balance, so the bad May 15 partial-balance row no longer consumes open lots or inflates realized losses while older zero-CLOB repairs still close stale local positions.

Scheduler startup backups now use a freshness gate instead of always creating a full SQLite copy. Paper and live schedulers ensure a backup newer than 6 hours by default, reuse a fresh existing backup when available, and still allow `--startup-backup-min-interval-minutes 0` to force the previous fresh-copy behavior. The explicit `backup-db` command remains fresh-by-default and adds `--if-older-than-minutes` for opt-in recent-backup reuse.

Spec and milestone-file governance is now documented in `docs/specs.md` and `docs/milestone-files.md`, and `AGENTS.md` points future agents at those files for spec location, status-file discipline, milestone structure, and session-end updates.

Live dashboard trade rows now normalize filled order notional from `fill_price * fill_shares` whenever CLOB/API storage has a zero or missing `fill_size_usd`, so the USD column, realized PnL, unrealized PnL, and chart markers use the same cash-flow basis. Live dashboard open-position reporting now replays filled live orders instead of trusting persisted `live_positions.avg_price`, and open trade drilldowns show only remaining open buy-lot shares so table uPnL adds up to the summary.

Live invalidation exits now cap submitted sell shares to the CLOB-reported conditional token balance when that balance is lower than local live position shares. This avoids rejected all-or-nothing FAK exits when the local live replay overstates sellable shares, while recording a `live_position_balance_mismatch` warning risk event so the accounting mismatch remains visible.

Live entries now refresh the CLOB orderbook immediately before submitting a live buy, persist that fresh quote, and re-apply the entry cap/slippage rule against it. If the fresh quote has moved beyond the executable rule that produced the candidate, the buy is recorded as missed instead of sending a stale-price FAK order.

Live buys now size down to available visible ask depth within the slippage cap instead of requiring enough depth for the full requested order. A scheduler buy that requests 20 USD can submit a smaller FAK, such as 3 USD, when only that much depth is executable, while still enforcing a 1 USD minimum live entry fill.

Live sell misses now signpost their reason in scheduler notes, including the label, side, trigger, and bid. Open-position exits also check whether a position is actually invalidated before counting missing bid depth as a sell miss, so `sells=0/N` no longer includes non-actionable held positions with thin books.

Live balance mismatches now remain explicit risk events rather than automatic local position adjustments. When CLOB reports fewer sellable conditional tokens than the local ledger, the scheduler caps sell submission to the CLOB balance and leaves the local drift visible for the watchdog and operator review.

Live buys now reconcile reported fills against the wallet's conditional-token balance delta. If CLOB exposes pre/post token balances and the received token delta is smaller than the reported fill, the local fill is capped to the observed delta; if no tokens arrive, the order is marked `unknown_fill` and no local position is opened.

The live dashboard now runs the live-order reconcile path before serving live stats, forecast panels, PnL, and trade drilldowns. This lets submitted or `unknown_fill` orders become filled live orders and rebuilt open positions through ordinary dashboard refreshes, instead of requiring a separate manual `live-reconcile` before the dashboard can show overnight fills.

Forecast-panel trade markers now include traded live/paper tokens even when the token is missing from the latest orderbook candidate rows. Marker-only traded tokens fall back to their latest fill price, so B/S chart bubbles remain visible after fills on tokens that no longer have a fresh orderbook snapshot.

Live scheduler and live tick startup preflight now distinguish entry capacity from exit capability. Low pUSD cash balance, insufficient entry allowance, or `block_new_entries` can still prevent new buys, but they no longer stop the process before open-position exit checks can submit sells.

Live scheduler buy sizing is reduced to a `5 USD` per-order cap while the strategy proves consistent profitability.

Scheduler logs now print a loud `💰 TRADE EXECUTED 💰` line whenever a tick records filled buys or sells, including filled buy/sell counts and tick notes.

AWS GIS actual readings remain enabled for low-latency current temperature and extrema, but `MAXTEMP`/`MINTEMP` from exactly `00:00 HKT` are treated as previous-day rollover extrema and are not stored as same-day since-midnight max/min values. This prevents a midnight carryover such as `MAXTEMP=26.1` from triggering current-day actual-cross buys.

Operational strategy review on 2026-05-16 HKT found that live losses are mostly real strategy/execution losses rather than only the earlier zero-proceeds accounting bug. Excluding legacy drift-repair rows, filled live buys totaled about `$487.66` and filled live sells totaled about `$265.19`; rebuilt live positions reported about `-$215.18` realized PnL with only about `$7.29` remaining open cost. The largest loss was the May 9 highest-temperature `27°C or higher` YES thesis: the bot bought about `$174.16`, sold about `$87.24`, and realized about `-$86.92` after the HKO forecast/cheap-bucket logic repeatedly added exposure before the actual max settled below the bucket at about `26.1°C`.

The assumptions that failed were: HKO forecast buckets are not reliably mispriced when Polymarket disagrees; a small latest-two-snapshot price move does not prove the market has not already repriced before the detected forecast change; repeated cheap-ask entries compound a single wrong forecast thesis because `forecast_value` allows existing-position add-ons; and immediate invalidation exits crystallize losses when hourly forecasts oscillate by one bucket in thin books. Earlier actual-cross losses also showed a data-quality assumption failure around same-day extrema: midnight carryover extrema could create false actual-cross signals, and exact-bucket NO positions could be dumped on transient exact matches before the day moved through the upper boundary.

Strategy tightening on 2026-05-16 HKT disables live `forecast_value` entries by default with `Settings.live_forecast_value_entries_enabled = False`, while preserving paper-mode forecast-value research behavior. Live candidate buys now reject duplicate open positions even if a paper-mode caller explicitly allows add-ons, so a single forecast thesis cannot compound exposure through repeated cheap-ask dips. Forecast-change candidate selection now compares the latest price against the latest orderbook snapshot at or before the new HKO forecast update time when that baseline is available, falling back to the previous latest-two-snapshots behavior only when no event-time baseline exists. This prevents entries where the market already moved before the last two stored snapshots made the price look quiet.

Historical ledger estimate on 2026-05-16 HKT replayed filled `paper_orders` FIFO against latest executable bids instead of doing a full strategy replay, because the current backtest harness runs paper mode and does not naturally exercise the new live-only `forecast_value` kill switch. On the dashboard-active non-excluded paper ledger, baseline total PnL was about `-$184.88`; removing `forecast_value` buys changed it to about `+$63.05`, a `+$247.93` improvement. On all filled paper orders including excluded rows, baseline total PnL was about `-$687.57`; removing `forecast_value` buys changed it to about `-$142.48`, a `+$545.09` improvement. Simulating both no `forecast_value` entries and no add-ons to an already-open token across all filled paper orders changed total PnL to about `-$42.23`, a `+$645.34` improvement. This estimate does not fully quantify the forecast-change event-time baseline filter, because that requires a slower full decision replay or a more purpose-built historical harness.

Operational investigation on 2026-05-17 HKT found that the overnight D+0 May 17 high-temperature behavior was not a `forecast_value` live-entry regression. The 25°C YES buy at `2026-05-16T23:14:03Z` was recorded as `forecast_change` for the effective high move `26.0 -> 25.9`, filled at `38c`, and came from the normal new-bucket forecast-change rule. The 26°C NO candidate for the same move was generated but rejected because the live fresh quote had no ask within the current `forecast_change_max_entry_price = 0.40` cap; the local orderbook showed the 26°C NO ask around `64c`. The later second successive forecast reduction `25.9 -> 25.1`, first seen at `2026-05-17T00:34:10Z`, produced only a processed event and an exit miss; it generated no 26°C NO candidate because `build_forecast_move_candidates` returns no entries when `floor(old_forecast) == floor(new_forecast)`. At that time the 26°C NO ask was around `74c`, later moving into the low `90c` range. This identifies a strategy gap: same-floor downward momentum below an already-above bucket can strengthen an invalidated-bucket NO thesis but is invisible to current forecast-change candidate generation.

Follow-up log and ledger check for the same D+0 high position found that the 25°C YES sell was not triggered by a daily displayed forecast drop below 25°C. It sold in four `exit_check` fills from `2026-05-17T05:14:29Z` to `05:14:52Z` at `26c-27c` because the latest OCF hourly path first breached the 25°C bucket floor only at `21:00` HKT, matching the existing `late-day forecast peak guard`. The latest actual high in the decision details was still only `24.2°C`, and later `forecast_exit` attempts were rejected as below exchange precision because the position had already been reduced to dust.

Same-day live trade review on 2026-05-17 HKT found the other filled trades were also driven by forecast-change bucket rotations or hourly-forecast exits. The D+0 low 23°C YES position, bought the prior evening at `36c`, sold around midnight at `52c` because the latest hourly low path no longer reached the 23°C bucket, even though the observed low had already touched `23.9°C`; this is an over-eager exact-low exit risk because future hourly forecasts can ignore an already-observed in-bucket low. The D+0 high 26°C YES position, bought at `33c`, sold at `36c` after the high forecast moved `26.0 -> 25.9` and the hourly high no longer reached 26°C. For D+1 May 18, the bot bought 27°C high YES at `35c` on `28.0 -> 27.0`, then sold it at `23c-26c` when the forecast later moved `27.0 -> 26.0`; it also bought 26°C high YES at `27c` on that new bucket. The bot bought 24°C low YES at `39c` on `25.0 -> 24.0`, then sold at `30c` when the forecast later moved `24.0 -> 23.0`; it also bought 23°C low YES at `20c`. The latest checked orderbooks had the open D+1 26°C high YES bid/ask around `21c/24c` and the open D+1 23°C low YES around `20c/36c`.

Observation-source check on 2026-05-17 HKT found that the apparent `16:02` current temperature of `25.0°C` came from the public `rhrread` JSON current-weather feed, which reports whole-degree values for "Hong Kong Observatory" and stored no since-midnight extrema. The official/AWS decimal row around the same time reported `TEMP=24.5` and `MAXTEMP=24.6`, while the since-midnight CSV also reported max `24.6`; this explains why the current-temp chart can show `25.0°C` while the since-midnight max never reaches the 25°C bucket.

Historical check on 2026-05-17 HKT scanned completed target dates through 2026-05-16 for adjacent-bucket NO entries after two consecutive effective forecast moves. For highest-temperature two-step downward moves, 33 adjacent NO observations were priced; caps through `0.70` had about `+0.17` average return per share, while `0.75` still stayed positive at about `+0.10` and would have admitted the missed May 17-style `74c` 26°C NO. Caps above `0.75` degraded sharply because false positives near `78c-86c` erased much of the edge. For lowest-temperature two-step upward moves, the signal was weaker: `0.40` to `0.50` caps looked best on a small sample, `0.60` was only slightly positive, and `0.70+` was effectively breakeven. This supports a separate, higher cap for high-temperature downward momentum NO invalidations, tentatively around `0.75`, while keeping low-temperature upward NO invalidations more conservative unless another filter is added.

Market-versus-forecast historical check on 2026-05-17 HKT scanned completed target dates through 2026-05-16 and compared the Polymarket ladder favorite at each distinct OCF update time with the effective forecast bucket. Using latest non-stale YES prices within 30 minutes before the forecast update, favorites with implied probability at least `0.65` disagreed with the forecast in 134 update-time observations and resolved YES in 78 of them, about `58%`. On a target-day/market-kind basis, 18 completed high/low day-kind pairs had at least one `>=0.65` disagreement and 11 of those had at least one disagreeing favorite that won, about `61%`. A stricter "market led the forecast" filter, where a later OCF update eventually matched the earlier disagreeing market favorite, was much rarer but stronger: at `>=0.65`, 21 observations had later forecast catch-up and 16 resolved YES, about `76%`; at `>=0.70`, 18 catch-up observations had 14 winners, about `78%`. The catch-up subset split unevenly: low-temperature catch-ups were 13/13 winners at `>=0.65` in this sample, while high-temperature catch-ups were 3/8, so this edge should not be treated as symmetric without more filtering.

Live scheduler check on 2026-05-18 HKT found the scheduler process still running and processing market websocket messages, but `orderbook_snapshots` had grown to about 49 GB with a 2 GB WAL. The flood was caused by market websocket `price_change` placeholder rows for unsubscribed/unknown `0x...` asset IDs and empty book updates with no bid/ask depth. The websocket client now filters incoming market messages to the active subscribed token IDs, and `OrderBookCache` now ignores placeholder `price_change`, `best_bid_ask`, and `last_trade_price` messages when no cached real book exists. Cache updates that produce an empty bid/ask book still update in-memory state to avoid stale executable depth, but they are no longer persisted as websocket orderbook snapshots.

Orderbook hot-table split is specified in `docs/orderbook-hot-table.md` and implemented as the first non-destructive archive/execution separation step. `migrate()` now creates `orderbook_latest`, keyed by `outcome_id`, and `store_orderbook()` writes the append-only `orderbook_snapshots` archive while upserting the latest row only when the incoming snapshot is at least as recent as the existing hot row. `latest_orderbook()` and trading-decision orderbook-age metadata now read from `orderbook_latest` first and fall back to `orderbook_snapshots` for pre-migration data. Historical charting, event-time baseline logic, backtests, and research still read the archive table.

Live CLOB orderbook availability is now tracked per outcome. When Polymarket CLOB returns `404` with "No orderbook exists for the requested token id", `fetch_orderbook()` raises a typed `ClobOrderBookUnavailable` instead of a generic HTTP error. Orderbook polling marks the affected outcome row `clob_tradeable=0` with `clob_status='no_orderbook'`, later polling skips it, and active websocket token/condition subscriptions exclude non-tradeable outcomes. Live targeted entry refreshes also mark unavailable tokens and record the buy candidate as missed/ignored instead of repeatedly hammering archived/no-book markets. This addresses the May 19 HKT Polymarket-side condition where Gamma still exposed HK high/low temperature event rows but CLOB archived the condition and removed the executable orderbook.

Relevant existing implementation:

- Strategy/decision path: `src/whenitrains/runner.py`
- Paper execution: `src/whenitrains/paper_db.py`
- Market/orderbook client: `src/whenitrains/polymarket.py`
- Persistence/migrations: `src/whenitrains/storage.py`
- Scheduler: `src/whenitrains/scheduler.py`
- CLI: `src/whenitrains/cli.py`
- Orderbook hot-table spec: `docs/orderbook-hot-table.md`
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

Session verification on 2026-05-15 HKT:

- Red/green tests updated: `test_repair_live_position_drifts_does_not_book_zero_proceeds_adjustment`, `test_execute_live_sell_caps_to_clob_sellable_balance`, `test_execute_live_sell_keeps_local_position_when_clob_has_no_sellable_balance`, and `test_rebuild_live_positions_ignores_zero_proceeds_balance_adjustments`.
- Red/green test added: `test_live_dashboard_ignores_zero_proceeds_reconcile_sells`.
- Red/green tests added: `test_rebuild_live_positions_honors_zero_clob_reconcile_sells` and `test_live_dashboard_honors_zero_clob_reconcile_sells`.
- `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_live.py'` passes: 55 tests.
- `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_dashboard_server.py' -k 'live'` passes: 12 tests.
- `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests` passes: 515 tests.
- Browser visual check completed against `http://127.0.0.1:8788/live` using a temporary `/private/tmp` SQLite DB with a legacy zero-proceeds `RECONCILE_SELL`; the summary and open-position drilldown showed the remaining 15.5234 shares, -$3.42 realized PnL, and $3.99 unrealized PnL.
- Live scheduler restarted with the patched code in detached `screen` session `whenitrains-live-scheduler`, logging to `/private/tmp/whenitrains-live-scheduler-restart-20260515-132806.log`. Restart preflight passed, startup drift scan reported `drift_count=0`, `block_new_entries` auto-cleared to `0`, and subsequent ticks ran without the previous freeze note.

Session verification on 2026-05-16 HKT:

- Investigation-only session; no implementation changes or automated tests were run.
- Read local live logs from `/Users/magnus/whenitrains-live-logs` and active restart log `/private/tmp/whenitrains-live-scheduler-restart-20260515-132806.log`.
- Queried `data/whenitrains.sqlite3` read-only for `live_orders`, `live_positions`, `paper_decisions`, `orderbook_snapshots`, `hko_forecasts`, `hko_current_observations`, `markets`, and `outcomes` to separate strategy losses, open exposure, and legacy accounting artifacts.
- Red/green test added: `test_forecast_change_skips_when_pre_event_baseline_already_moved`.
- Red/green test added: `test_live_forecast_value_entries_are_disabled`.
- Red/green test added: `test_live_forecast_value_does_not_add_to_existing_position_even_when_enabled`.
- `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_runner.py' -k 'pre_event_baseline' -k 'live_forecast_value'` passes: 3 tests.
- `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_runner.py' -k 'forecast_change' -k 'forecast_value' -k 'live_forecast'` passes: 41 tests.
- `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_runner.py'` passes: 97 tests.
- `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_live.py'` passes: 55 tests.
- `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests` passes: 518 tests.

Session verification on 2026-05-17 HKT:

- Investigation-only session; no implementation changes or automated tests were run.
- Read the active local live scheduler log at `/private/tmp/whenitrains-live-scheduler-restart-20260515-132806.log`.
- Queried `data/whenitrains.sqlite3` read-only for `live_orders`, `paper_decisions`, `orderbook_snapshots`, `ocf_forecast_samples`, `outcomes`, `markets`, `live_settings`, and `risk_events`.
- Confirmed the D+0 May 17 25°C YES buy was a `forecast_change` fill, not a `forecast_value` fill.
- Confirmed the D+0 May 17 25°C YES sell was an `exit_check` from the hourly late-day forecast peak guard, with the first forecast 25°C-or-higher hour at 21:00 HKT and actual high still at 24.2°C.
- Reviewed the other May 17 HKT filled live trade groups: D+0 low 23°C sell, D+0 high 26°C sell, D+1 high 27°C round-trip, D+1 low 24°C round-trip, and new open D+1 26°C high / 23°C low buys.
- Checked the apparent 16:02 HKT 25.0°C actual reading and confirmed it came from rounded `rhrread` current temperature; decimal AWS/since-midnight sources stayed at current `24.5°C` and max `24.6°C`.
- Confirmed the missed 26°C NO opportunity split across two mechanics: the first cross-floor reduction generated a NO candidate but hit the 40c cap, while the second same-floor reduction generated no NO candidate at all.
- Ran a read-only historical adjacent-bucket NO scan for two consecutive high-forecast downward moves and two consecutive low-forecast upward moves through completed dates ending 2026-05-16.
- Ran a read-only historical Polymarket-favorite disagreement scan using latest non-stale pre-update YES prices, final observed high/low outcomes, and an additional later-forecast-catch-up filter.

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
Ran 526 tests in 13.471s
OK
```

CLOB no-orderbook tradeability red/green:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_markets.py' -k 'fetch_orderbook_maps'
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_storage.py' -k 'mark_clob_tokens'
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_cli.py' -k 'fetch_orderbooks_marks_no_orderbook'
```

Red result: CLOB no-book 404s surfaced as generic HTTP errors, outcome rows had no CLOB tradeability state, and orderbook polling retried no-book tokens on every pass.

Green result after adding typed no-book errors, additive outcome tradeability columns, polling skip/mark behavior, and websocket subscription filtering:

```text
Ran 1 test in 0.002s
OK
Ran 1 test in 0.022s
OK
Ran 1 test in 0.025s
OK
```

Websocket flood red/green:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_market_websocket.py'
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_orderbook_cache.py'
```

Red result: unknown websocket `asset_id` messages were counted/applied, and placeholder websocket updates without cached books returned/persisted empty `OrderBook` snapshots.

Green result after filtering market websocket messages to subscribed token IDs and suppressing placeholder empty websocket persistence:

```text
Ran 5 tests in 0.018s
OK
Ran 10 tests in 0.096s
OK
```

Orderbook hot-table red/green:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_storage.py'
```

Red result: `orderbook_latest` did not exist, `store_orderbook()` wrote only the historical archive, and `latest_orderbook()` could not use a hot execution row.

Green result after adding `orderbook_latest`, write-through upsert, hot-table latest reads, archive fallback, and matching fixture updates:

```text
Ran 18 tests in 0.387s
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
