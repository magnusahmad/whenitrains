# Live Trading Spec

## 1. Scope

Add guarded Polymarket live execution to the existing HK highest-temperature trading bot.

The live trading system must reuse the current paper strategy, market parsing, orderbook ingestion, signal dedupe, and risk logic. The first live version should change only the execution adapter and persistence layer needed to submit, track, reconcile, and cancel real CLOB orders.

Paper trading remains the default mode. Live trading must fail closed unless explicitly enabled by CLI flag, environment gate, authenticated credentials, and passing preflight checks.

Current implementation status as of 2026-05-06 HKT:

- Additive live storage exists for orders, positions, and persistent live settings.
- Live config loading requires `WHENITRAINS_TRADING_MODE=live`, pre-derived CLOB API credentials, funder/signature values, and a Keychain-stored hot key.
- Manual live commands exist for preflight, auth smoke, buy, sell, reconcile, cancel one order, cancel all orders, and kill-switch setting changes.
- `live-tick --live` and `live-scheduler --live` are wired through the existing scheduler path and use live positions for duplicate-position checks.
- Live buys use FAK semantics through the official Python CLOB client wrapper, apply visible-depth quoting, worst-price protection, minimum fill, order caps, total exposure cap, and entry kill-switch checks.
- Live sells reject missing positions, sell no more than the confirmed local live position, and update live PnL from reconciled fills.
- Browser live reporting exists at `/live` with `/api/live/stats`.
- Real-auth smoke and real-money orders are still pending credentials, dependency validation, and explicit user approval.

## 2. Current Execution Boundary

The current strategy path already has a useful execution boundary:

- `runner._execute_candidate_buy(...)` validates duplicate positions, remaining budget, max entry price, slippage cap, and minimum fill before calling `execute_paper_buy(...)`.
- Exit logic calls `execute_paper_sell(...)` after strategy and invalidation checks decide that a position should be sold.
- Paper execution persists to `paper_orders` and `paper_positions`; strategy decisions are written through the shared trading-decision path. The current compatibility table is still named `paper_decisions`, but the terminology should be treated as strategy-decision logging so paper and live execution can run side by side.

Live trading should preserve those decision paths and replace the execution adapter. Strategy code should not know Polymarket SDK details.

Target interface:

```python
class ExecutionAdapter:
    def buy(
        self,
        *,
        db,
        token_id: str,
        side: str,
        size_usd: float,
        asks: list[tuple[float, float]],
        max_order_usd: float,
        reason: str,
        max_price: float | None,
        min_fill_usd: float,
        context: dict,
    ) -> ExecutionResult:
        ...

    def sell(
        self,
        *,
        db,
        token_id: str,
        bids: list[tuple[float, float]],
        reason: str,
        context: dict,
    ) -> ExecutionResult:
        ...
```

The paper adapter can wrap the existing `execute_paper_buy` and `execute_paper_sell`. The live adapter will submit CLOB orders and reconcile real fills.

## 3. Polymarket Integration

Use the official Polymarket CLOB client rather than hand-rolling signing and authentication.

Current Polymarket docs state:

- Public Gamma, Data API, and CLOB read endpoints do not require authentication.
- CLOB trading endpoints require L2 authentication headers.
- The CLOB uses L1 private-key signing to create or derive API credentials.
- L2 API credentials authenticate order submission, order/cancel queries, balances, and allowances.
- Order creation still requires local signing of the order payload.
- All Polymarket orders are limit orders. Market orders are implemented as marketable limits with `FOK` or `FAK`; the `price` field is worst-price protection.
- `FAK` can partially fill available liquidity and cancel the remainder.
- `FOK` must fill entirely or cancel.

References:

- https://docs.polymarket.com/api-reference/authentication
- https://docs.polymarket.com/developers/CLOB/orders/create-order
- https://docs.polymarket.com/developers/CLOB/clients/methods-l2
- https://github.com/Polymarket/py-clob-client

Initial dependency target:

```toml
dependencies = [
  "py-clob-client",
]
```

Pin the package version once installed and tested locally.

## 4. Credentials And Configuration

Secrets must come from environment variables or a local secret manager. They must never be stored in SQLite, committed to git, or printed in terminal output.

Wallet strategy:

- Use a dedicated Polymarket proxy wallet for bot trading.
- Use the proxy wallet funder address shown by Polymarket as `POLYMARKET_FUNDER_ADDRESS`.
- Use `POLYMARKET_SIGNATURE_TYPE=3` for this Polymarket proxy-wallet path.
- Keep Ledger or another hardware wallet as the treasury/root of trust.
- Fund the bot proxy wallet only with the current live risk budget plus a small operational buffer.
- Do not require the Ledger during automated trading. The bot needs hot signing authority for unattended order creation.

Hot-key handling:

- Use a dedicated bot private key only for Polymarket trading.
- Do not reuse a personal wallet, treasury wallet, browser wallet, or Ledger seed.
- Store the hot key on the isolated MacBook only.
- Store the hot key in macOS Keychain.
- Default Keychain service: `whenitrains-polymarket`.
- Default Keychain account: `bot-private-key`.
- Print only the public signer address and funder address in diagnostics.
- Never print, persist, or log private key material, API secret material, or full auth headers.
- Treat compromise of the MacBook as compromise of the bot wallet, and rely on funding limits plus live risk caps as the blast-radius control.

API credential handling:

- Runtime live commands require pre-derived L2 credentials: `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, and `POLYMARKET_API_PASSPHRASE`.
- The live scheduler must not create or derive API credentials during normal startup.
- Add a separate explicit setup command later, such as `live-create-api-creds`, if credential creation/derivation is automated in this repo.
- Even with pre-derived L2 credentials, the hot private key is still required at runtime to sign order payloads locally.
- The API secret must be treated as sensitive as the hot key for logging and storage purposes.

Required live env:

- `WHENITRAINS_TRADING_MODE=live`
- Keychain item containing `POLYMARKET_PRIVATE_KEY`
- `POLYMARKET_SIGNATURE_TYPE=3`
- `POLYMARKET_FUNDER_ADDRESS`
- `POLYMARKET_API_KEY`
- `POLYMARKET_API_SECRET`
- `POLYMARKET_API_PASSPHRASE`

Optional:

- `POLYMARKET_HOST=https://clob.polymarket.com`
- `POLYMARKET_CHAIN_ID=137`
- `WHENITRAINS_LIVE_CONFIRM=...`
- `WHENITRAINS_KEYCHAIN_SERVICE=whenitrains-polymarket`
- `WHENITRAINS_KEYCHAIN_ACCOUNT=bot-private-key`

The Keychain service/account values are labels chosen by this application. They are not issued by Polymarket. Setup should store the bot private key under the default labels unless the user overrides them.

To load the required live env vars from a non-committed local env file into the current shell:

```bash
eval "$(PYTHONPATH=src .venv/bin/python -m whenitrains.cli live-env-exports --env-file .env)"
```

The command prints only the required live exports, shell-quotes values, and fails closed if any required value is missing. Keep `.env` local-only; it is ignored by git.

`live-env-exports` is read-only: it does not create or edit `.env`. Add `POLYMARKET_FUNDER_ADDRESS=<proxy wallet address>` to `.env` before loading exports. The `eval` wrapper only applies the printed exports to the current shell process.

Live mode should require both a CLI flag and env gate:

```bash
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 live-scheduler --live
```

If `--live` is present but `WHENITRAINS_TRADING_MODE` is not `live`, abort.

If env is live but the command is a paper command, stay in paper mode.

## 5. Live Order Semantics

### Entry

Before submitting a buy:

- Re-fetch or validate the latest orderbook freshness.
- Re-run visible-depth quote calculation.
- Apply `max_order_usd`.
- Apply remaining per-token position budget.
- Apply `max_entry_price`.
- Apply dynamic slippage cap: `best_ask + max_entry_limit_slippage`.
- Apply `min_entry_fill_usd`.
- Confirm market is active and accepting orders if the field is available.
- Confirm the market still matches expected HK highest-temperature resolution rules.

Initial live entry order type:

- Use `FAK` market order or marketable limit order.
- Amount is USD spend.
- Price is worst allowed price.
- Persist both requested amount and actual fill.
- Do not place resting entry orders in live v1.

Reasoning:

- `FAK` matches the current paper model better than `FOK` because it can accept partial available liquidity, while `min_entry_fill_usd` prevents tiny dust fills.
- `FOK` is stricter but can miss otherwise acceptable liquidity when the full requested size is not immediately available.
- `GTC` and `GTD` should not be used in live v1 because resting stale orders around fast HKO updates can create uncontrolled exposure and reconciliation complexity.

### Exit

Before submitting a sell:

- Reconcile current local live position from CLOB order/trade history where possible.
- Re-fetch or validate current bid depth.
- Apply worst-price protection.
- Sell no more than confirmed held shares.
- Prefer `FAK` so available bids can be taken immediately without leaving a resting order.
- Do not place resting exit orders in live v1.

Exit reasons include:

- take profit reached
- max hold time reached
- position invalidated by forecast change
- position invalidated by observed max
- manual live sell
- risk kill switch

### Order Status

The immediate order response is not sufficient as the source of truth.

After every live submit:

- Persist the raw response.
- Query order status when an order ID is returned.
- Query recent user trades/fills for the token.
- Compute actual filled shares, actual average fill price, and actual proceeds/cost.
- Update live positions only from actual fills.
- Record unmatched or ambiguous responses as risk events.

## 6. Persistence

Do not overload paper tables with real orders. Add live-specific tables so paper backtests and real-money audit trails cannot be confused.

Proposed tables:

```sql
create table if not exists live_orders (
    id integer primary key autoincrement,
    created_at_utc text not null,
    submitted_at_utc text,
    reconciled_at_utc text,
    event_type text,
    event_key text,
    outcome_id text not null,
    label text,
    side text not null,
    action text not null,
    clob_order_id text,
    order_type text,
    status text not null,
    requested_size_usd real,
    requested_shares real,
    limit_price real,
    fill_price real,
    fill_size_usd real,
    fill_shares real,
    reason text,
    error text,
    raw_request_json text,
    raw_response_json text,
    raw_reconcile_json text
);

create table if not exists live_positions (
    outcome_id text primary key,
    net_shares real not null,
    avg_price real not null,
    realized_pnl real not null,
    updated_at_utc text not null,
    last_reconciled_at_utc text
);
```

Potential later additions:

- `live_balance_snapshots`
- `live_cancellations`
- `live_reconcile_events`

Implemented live settings table:

```sql
create table if not exists live_settings (
    name text primary key,
    value text not null,
    updated_at_utc text not null
);
```

The current live settings are `block_new_entries` and `cancel_open_orders_and_exit_positions`.

Every schema change against the production-like DB requires a backup first:

```bash
PYTHONPATH=src .venv/bin/python -m whenitrains.cli --db data/whenitrains.sqlite3 backup-db
```

## 7. Safety Controls

Live mode must include these hard gates before the first real order:

- Explicit CLI command or `--live` flag.
- `WHENITRAINS_TRADING_MODE=live`.
- Authenticated CLOB client initialization succeeds.
- Balance and allowance checks pass.
- Startup DB backup succeeds.
- No critical unresolved `risk_events` for resolution rules, parser mismatch, or reconciliation mismatch.
- Single-process DB lock prevents duplicate schedulers.
- Market allowlist matches the HK highest-temperature slug family.
- Order size cap.
- Per-token exposure cap.
- Total open exposure cap.
- Daily realized loss cap.
- Worst-case open loss cap.
- Source freshness guard.
- Orderbook freshness guard.
- Kill switch persistent state, emergency file, or runtime flags block entries and optionally exit positions when explicitly configured.

Recommended initial live caps:

- Manual real-money smoke cap: `5 USD`.
- Live scheduler order cap: `5 USD`.
- Initial total open exposure cap: `200 USD` cost basis across all live HK highest-temperature positions.
- Initial daily realized loss cap: `200 USD`.
- Disable live forecast-value add-on buys until base entry/exit round trips are tested.

For v1, total open exposure means the sum of confirmed cost basis across all open live positions:

```text
total_open_exposure = sum(live_positions.net_shares * live_positions.avg_price)
```

The bot must reject new entries if the next fill could push this value above the configured cap.

Kill switch controls are two separate settings:

- `block_new_entries`: prevents all new live buys.
- `cancel_open_orders_and_exit_positions`: cancels live open orders and attempts live exits for confirmed positions.

Kill switch control surfaces:

- Persistent state: store the two kill-switch booleans in SQLite or a small local state table so CLI changes survive process restarts.
- Emergency file: if `data/KILL_SWITCH` exists, the live scheduler must block new entries immediately on the next tick.
- Runtime flags: live commands accept explicit flags such as `--no-new-entries` and `--exit-on-kill-switch` for one-off process behavior.
- Exit behavior is never implied by the emergency file alone; exits require the persistent `cancel_open_orders_and_exit_positions` setting or an explicit runtime flag.

## 8. CLI Commands

Add manual commands before scheduler integration:

```bash
whenitrains live-preflight
whenitrains live-store-hot-key
whenitrains live-auth-smoke
whenitrains live-buy LABEL SIDE SIZE_USD --yes-i-understand
whenitrains live-sell LABEL SIDE --yes-i-understand
whenitrains live-reconcile
whenitrains live-cancel-order ORDER_ID --yes-i-understand
whenitrains live-cancel-all --yes-i-understand
```

Scheduler commands:

```bash
whenitrains live-tick --live
whenitrains live-scheduler --live
```

Implementation note: these commands now exist. Authenticated live commands print a clear `LIVE TRADING` banner before doing authenticated actions. Order-submitting/canceling commands require `--live`; manual buy, sell, cancel-one, and cancel-all also require `--yes-i-understand`.

Kill-switch command:

```bash
whenitrains live-kill-switch --block-new-entries
whenitrains live-kill-switch --allow-new-entries
whenitrains live-kill-switch --exit-on-kill-switch
whenitrains live-kill-switch --no-exit-on-kill-switch
```

## 9. Scheduler Integration

Current scheduler directly calls `run_paper_tick`.

Live scheduler should use a mode-aware runner:

```python
run_trading_tick(db, today_hkt, execution_adapter)
```

Paper scheduler should continue to use the paper adapter by default.

Live scheduler startup sequence:

1. Migrate DB.
2. Create DB backup.
3. Acquire scheduler lock.
4. Initialize authenticated CLOB client.
5. Check balance/allowance.
6. Reconcile live orders and positions.
7. Verify no fatal risk events.
8. Start normal polling and decision loop.

Live mode must not share paper positions for duplicate-position checks. It must use live positions or reconciled CLOB state.

## 10. Testing Plan

### Unit Tests

- Execution adapter interface calls paper implementation unchanged.
- Live buy rejects without env gate.
- Live buy rejects without credentials.
- Live buy rejects when balance/allowance is insufficient.
- Live buy rejects stale orderbooks.
- Live buy uses worst-price cap.
- Live buy respects `min_entry_fill_usd`.
- Live buy persists rejected SDK responses.
- Live buy updates live positions only from actual fills.
- Live sell rejects when local/reconciled position is zero.
- Live sell caps shares to confirmed held amount.
- Live reconcile handles partial fill, full fill, canceled, rejected, and missing order IDs.
- Kill switch blocks entries.
- Critical risk event blocks live scheduler startup.

### Integration Tests With Fake CLOB Client

- Manual `live-preflight` happy path.
- Manual `live-buy` full fill.
- Manual `live-buy` partial fill above min fill.
- Manual `live-buy` partial fill below min fill records risk/missed status.
- Manual `live-sell` closes position.
- `live-reconcile` repairs local position after delayed fill.
- `live-cancel-all` records cancellation results.
- Scheduler in live mode places exactly one order for a deduped event.
- Scheduler in live mode does not read or mutate paper positions.

### Read-Only Auth Smoke

Use real credentials only for:

- Client authentication.
- Server time / health endpoint.
- Balance and allowance query.
- Open orders query.
- No order submission.

### Manual Real-Money Smoke

Only after fake-client integration and read-only auth pass:

- Use disposable DB copy or production DB after backup.
- Use smallest practical order size.
- Submit one manual `live-buy` with `FAK`.
- Reconcile immediately.
- Submit one manual `live-sell`.
- Reconcile immediately.
- Confirm dashboard/status output distinguishes live from paper.

### Scheduler Real-Money Trial

Only after manual real-money smoke:

- Enable one target market/date.
- Use very small cap.
- Run for a bounded number of ticks.
- Confirm no duplicate orders for repeated events.
- Confirm exit behavior.
- Keep paper scheduler running separately for parity comparison.

## 11. Rollout Milestones

Milestone L1: Spec and dependency decision.

Milestone L2: Execution adapter abstraction with paper adapter parity.

Milestone L3: Live schema, storage helpers, and migrations.

Milestone L4: Authenticated CLOB client wrapper and preflight checks.

Milestone L5: Manual live commands using fake CLOB tests.

Milestone L6: Read-only real-auth smoke.

Milestone L7: Manual real-money buy/sell smoke.

Milestone L8: Mode-aware runner and live tick.

Milestone L9: Live scheduler behind explicit gates.

Milestone L10: Live dashboard/status reporting and operational runbook.

## 12. Open Questions

No known product decisions remain before real-auth smoke. Real credentials, dependency installation, and Polymarket account-specific signature/funder validation are still required operationally.
