from __future__ import annotations

import os
import json
import shlex
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol

from .config import Settings
from .paper_db import calculate_entry
from .storage import (
    get_live_position,
    get_live_setting,
    live_realized_pnl_since,
    live_setting_enabled,
    live_total_open_exposure,
    set_live_setting,
    store_live_order,
    store_risk_event,
    update_live_order_reconcile,
    upsert_live_position,
)


class LiveTradingError(RuntimeError):
    pass


INSUFFICIENT_BALANCE_BLOCK_THRESHOLD = 3
INSUFFICIENT_BALANCE_ERROR_COUNT_SETTING = "insufficient_balance_error_count"


REQUIRED_LIVE_ENV_NAMES = (
    "WHENITRAINS_TRADING_MODE",
    "POLYMARKET_SIGNATURE_TYPE",
    "POLYMARKET_FUNDER_ADDRESS",
    "POLYMARKET_API_KEY",
    "POLYMARKET_API_SECRET",
    "POLYMARKET_API_PASSPHRASE",
)


@dataclass(frozen=True)
class LiveConfig:
    trading_mode: str
    private_key: str
    signature_type: int
    funder_address: str
    api_key: str
    api_secret: str
    api_passphrase: str
    host: str = "https://clob.polymarket.com"
    chain_id: int = 137
    keychain_service: str = Settings.live_keychain_service
    keychain_account: str = Settings.live_keychain_account


@dataclass(frozen=True)
class LivePreflightResult:
    ok: bool
    signer_address: str | None
    funder_address: str | None
    balance_usd: float | None
    allowance_ok: bool
    reason: str


@dataclass(frozen=True)
class LiveExecutionResult:
    status: str
    token_id: str
    side: str
    fill_price: float | None
    fill_size_usd: float
    shares: float
    reason: str
    clob_order_id: str | None = None


class LiveClobClient(Protocol):
    def signer_address(self) -> str | None:
        ...

    def balance_usd(self) -> float | None:
        ...

    def allowance_ok(self) -> bool:
        ...

    def buy_fak(self, token_id: str, price: float, size_usd: float) -> dict:
        ...

    def sell_fak(self, token_id: str, price: float, shares: float) -> dict:
        ...

    def token_balance(self, token_id: str) -> float | None:
        ...

    def reconcile_order(self, order_id: str | None, token_id: str) -> dict:
        ...

    def trades_for_order(self, order_id: str | None, token_id: str) -> list[dict]:
        ...

    def cancel_order(self, order_id: str) -> dict:
        ...

    def cancel_all(self) -> dict:
        ...


class PolymarketClobClient:
    def __init__(self, config: LiveConfig):
        try:
            from py_clob_client_v2 import ApiCreds, ClobClient
        except ImportError as exc:
            raise LiveTradingError(
                "py-clob-client-v2 is not installed; run `python3 -m pip install -e .` before live trading"
            ) from exc
        creds = ApiCreds(
            api_key=config.api_key,
            api_secret=config.api_secret,
            api_passphrase=config.api_passphrase,
        )
        self._client = ClobClient(
            config.host,
            key=config.private_key,
            chain_id=config.chain_id,
            creds=creds,
            signature_type=config.signature_type,
            funder=config.funder_address,
        )
        self._signature_type = config.signature_type
        self._uses_v2_client = True

    def signer_address(self) -> str | None:
        return getattr(self._client, "get_address", lambda: None)()

    def balance_usd(self) -> float | None:
        result = self._balance_allowance()
        if result is not None:
            for key in ("balance", "usdc", "available"):
                if key in result:
                    return _usdc_amount(result[key])
        return None

    def allowance_ok(self) -> bool:
        result = self._balance_allowance()
        if result is not None:
            allowance = result.get("allowance")
            if allowance is not None:
                return float(allowance) > 0
            allowances = result.get("allowances")
            if isinstance(allowances, dict):
                return any(float(value or 0) > 0 for value in allowances.values())
        return True

    def balance_allowance(self) -> dict | None:
        return self._balance_allowance()

    def token_balance(self, token_id: str) -> float | None:
        if hasattr(self._client, "get_balance_allowance"):
            payload = self._client.get_balance_allowance(self._conditional_params(token_id))
            return _asset_amount_from_payload(payload)
        return None

    def _balance_allowance(self) -> dict | None:
        if hasattr(self._client, "get_balance_allowance"):
            return self._client.get_balance_allowance(self._collateral_params())
        return None

    def _collateral_params(self):
        try:
            from py_clob_client_v2 import AssetType, BalanceAllowanceParams
        except ImportError:
            return SimpleNamespace(
                asset_type="COLLATERAL", signature_type=self._signature_type
            )
        try:
            return BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=self._signature_type,
            )
        except TypeError:
            return BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)

    def _conditional_params(self, token_id: str):
        try:
            from py_clob_client_v2 import AssetType, BalanceAllowanceParams
        except ImportError:
            return SimpleNamespace(
                asset_type="CONDITIONAL",
                token_id=token_id,
                signature_type=self._signature_type,
            )
        asset_type = getattr(AssetType, "CONDITIONAL", "CONDITIONAL")
        try:
            return BalanceAllowanceParams(
                asset_type=asset_type,
                token_id=token_id,
                signature_type=self._signature_type,
            )
        except TypeError:
            try:
                return BalanceAllowanceParams(asset_type=asset_type, token_id=token_id)
            except TypeError:
                return BalanceAllowanceParams(asset_type=asset_type)

    def buy_fak(self, token_id: str, price: float, size_usd: float) -> dict:
        if self._uses_v2_client:
            return self._post_v2_market_buy(token_id, price, size_usd)
        return self._post_order(token_id, price, size_usd, "BUY")

    def sell_fak(self, token_id: str, price: float, shares: float) -> dict:
        return self._post_order(token_id, price, shares, "SELL")

    def reconcile_order(self, order_id: str | None, token_id: str) -> dict:
        if order_id and hasattr(self._client, "get_order"):
            payload = self._client.get_order(order_id)
            if payload is not None:
                return dict(payload)
        return {"order_id": order_id, "token_id": token_id, "status": "unknown"}

    def trades_for_order(self, order_id: str | None, token_id: str) -> list[dict]:
        if not order_id or not hasattr(self._client, "get_trades"):
            return []
        try:
            from py_clob_client_v2 import TradeParams
            params = TradeParams(asset_id=token_id)
        except (ImportError, TypeError):
            params = SimpleNamespace(asset_id=token_id)
        try:
            payload = self._client.get_trades(params)
        except TypeError:
            payload = self._client.get_trades()
        return [
            trade
            for trade in _coerce_trade_list(payload)
            if _trade_mentions_order(trade, order_id)
        ]

    def cancel_order(self, order_id: str) -> dict:
        if self._uses_v2_client and hasattr(self._client, "cancel_order"):
            try:
                from py_clob_client_v2 import OrderPayload
            except ImportError as exc:
                raise LiveTradingError("py-clob-client-v2 order types unavailable") from exc
            return dict(self._client.cancel_order(OrderPayload(orderID=order_id)))
        if hasattr(self._client, "cancel"):
            return dict(self._client.cancel(order_id))
        if hasattr(self._client, "cancel_order"):
            return dict(self._client.cancel_order(order_id))
        raise LiveTradingError("CLOB client does not expose cancel_order")

    def cancel_all(self) -> dict:
        if hasattr(self._client, "cancel_all"):
            return dict(self._client.cancel_all())
        raise LiveTradingError("CLOB client does not expose cancel_all")

    def _post_order(self, token_id: str, price: float, size: float, side: str) -> dict:
        if self._uses_v2_client:
            return self._post_v2_order(token_id, price, size, side)
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL
        except ImportError as exc:
            raise LiveTradingError("py-clob-client order types unavailable") from exc
        order_kwargs = {
            "token_id": token_id,
            "price": price,
            "size": size,
            "side": BUY if side == "BUY" else SELL,
        }
        try:
            args = OrderArgs(**order_kwargs, order_type=OrderType.FAK)
        except TypeError:
            args = OrderArgs(**order_kwargs)
        options = self._order_options(token_id)
        try:
            signed = self._client.create_order(args, options=options)
        except TypeError:
            signed = self._client.create_order(args)
        return dict(self._client.post_order(signed, OrderType.FAK))

    def _post_v2_order(self, token_id: str, price: float, size: float, side: str) -> dict:
        try:
            from py_clob_client_v2 import (
                OrderArgs,
                OrderType,
                PartialCreateOrderOptions,
                Side,
            )
        except ImportError as exc:
            raise LiveTradingError("py-clob-client-v2 order types unavailable") from exc
        options = self._order_options(token_id)
        response = self._client.create_and_post_order(
            order_args=OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=Side.BUY if side == "BUY" else Side.SELL,
            ),
            options=PartialCreateOrderOptions(
                tick_size=options.tick_size,
                neg_risk=options.neg_risk,
            ),
            order_type=OrderType.FAK,
        )
        return dict(response)

    def _post_v2_market_buy(self, token_id: str, price: float, size_usd: float) -> dict:
        try:
            from py_clob_client_v2 import (
                MarketOrderArgs,
                OrderType,
                PartialCreateOrderOptions,
                Side,
            )
        except ImportError as exc:
            raise LiveTradingError("py-clob-client-v2 order types unavailable") from exc
        options = self._order_options(token_id)
        amount = _floor_decimal(size_usd, "0.01")
        if amount <= 0:
            raise LiveTradingError("buy amount rounds below one cent")
        response = self._client.create_and_post_market_order(
            order_args=MarketOrderArgs(
                token_id=token_id,
                amount=amount,
                side=Side.BUY,
                price=price,
                order_type=OrderType.FAK,
            ),
            options=PartialCreateOrderOptions(
                tick_size=options.tick_size,
                neg_risk=options.neg_risk,
            ),
            order_type=OrderType.FAK,
        )
        return dict(response)

    def _order_options(self, token_id: str) -> SimpleNamespace:
        tick_size = self._get_tick_size(token_id)
        neg_risk = self._get_neg_risk(token_id)
        if tick_size is not None and neg_risk is not None:
            return SimpleNamespace(tick_size=str(tick_size), neg_risk=bool(neg_risk))

        try:
            market = self._client.get_market(token_id)
        except Exception:
            market = {}
        tick_size = tick_size or (
            market.get("minimum_tick_size")
            or market.get("minimumTickSize")
            or market.get("tick_size")
            or market.get("tickSize")
            or "0.01"
        )
        neg_risk = neg_risk if neg_risk is not None else market.get("neg_risk")
        if neg_risk is None:
            neg_risk = market.get("negRisk")
        return SimpleNamespace(tick_size=str(tick_size), neg_risk=bool(neg_risk))

    def _get_tick_size(self, token_id: str) -> str | None:
        if not hasattr(self._client, "get_tick_size"):
            return None
        try:
            return str(self._client.get_tick_size(token_id))
        except Exception:
            return None

    def _get_neg_risk(self, token_id: str) -> bool | None:
        if not hasattr(self._client, "get_neg_risk"):
            return None
        try:
            return bool(self._client.get_neg_risk(token_id))
        except Exception:
            return None


def _floor_decimal(value: float, quantum: str) -> float:
    return float(Decimal(str(value)).quantize(Decimal(quantum), rounding=ROUND_DOWN))


def load_live_config(environ: dict[str, str] | None = None) -> LiveConfig:
    env = os.environ if environ is None else environ
    service = env.get("WHENITRAINS_KEYCHAIN_SERVICE", Settings.live_keychain_service)
    account = env.get("WHENITRAINS_KEYCHAIN_ACCOUNT", Settings.live_keychain_account)
    private_key = read_keychain_secret(service, account)
    required = {name: env.get(name, "") for name in REQUIRED_LIVE_ENV_NAMES}
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise LiveTradingError("missing live config: " + ", ".join(missing))
    if required["WHENITRAINS_TRADING_MODE"] != "live":
        raise LiveTradingError("WHENITRAINS_TRADING_MODE must be live")
    if not private_key:
        raise LiveTradingError(
            f"missing Keychain hot key service={service} account={account}"
        )
    return LiveConfig(
        trading_mode=required["WHENITRAINS_TRADING_MODE"],
        private_key=private_key,
        signature_type=int(required["POLYMARKET_SIGNATURE_TYPE"]),
        funder_address=required["POLYMARKET_FUNDER_ADDRESS"],
        api_key=required["POLYMARKET_API_KEY"],
        api_secret=required["POLYMARKET_API_SECRET"],
        api_passphrase=required["POLYMARKET_API_PASSPHRASE"],
        host=env.get("POLYMARKET_HOST", "https://clob.polymarket.com"),
        chain_id=int(env.get("POLYMARKET_CHAIN_ID", "137")),
        keychain_service=service,
        keychain_account=account,
    )


def read_live_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name not in REQUIRED_LIVE_ENV_NAMES:
            continue
        values[name] = _parse_env_value(value.strip())
    return values


def render_live_env_exports(values: dict[str, str]) -> tuple[list[str], list[str]]:
    missing = [name for name in REQUIRED_LIVE_ENV_NAMES if not values.get(name)]
    if missing:
        return [], missing
    return [
        f"export {name}={shlex.quote(values[name])}"
        for name in REQUIRED_LIVE_ENV_NAMES
    ], []


def _parse_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        try:
            parsed = shlex.split(value, posix=True)
        except ValueError:
            return value[1:-1]
        if len(parsed) == 1:
            return parsed[0]
    return value


def read_keychain_secret(service: str, account: str) -> str:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def store_keychain_secret(service: str, account: str, secret: str) -> None:
    result = subprocess.run(
        [
            "security",
            "add-generic-password",
            "-U",
            "-s",
            service,
            "-a",
            account,
            "-w",
            secret,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise LiveTradingError("failed to store hot key in macOS Keychain")


def preflight_live(
    db: sqlite3.Connection,
    client: LiveClobClient,
    config: LiveConfig,
    *,
    required_balance_usd: float = Settings.live_manual_order_cap_usd,
    require_entry_capacity: bool = True,
    kill_switch_path: Path = Settings.live_kill_switch_path,
) -> LivePreflightResult:
    if config.trading_mode != "live":
        return LivePreflightResult(False, None, config.funder_address, None, False, "not live mode")
    if require_entry_capacity and entries_blocked(db, kill_switch_path=kill_switch_path):
        return LivePreflightResult(
            False, client.signer_address(), config.funder_address, None, False, "entries blocked"
        )
    try:
        required_amount = required_balance_usd if require_entry_capacity else 0.0
        if hasattr(client, "balance_allowance"):
            payload = client.balance_allowance()
            balance = _balance_from_payload(payload)
            allowance_ok = _allowance_ok_from_payload(
                payload, required_amount_usd=required_amount
            )
        else:
            balance = client.balance_usd()
            allowance_ok = True if not require_entry_capacity else client.allowance_ok()
    except Exception as exc:
        return LivePreflightResult(
            False,
            client.signer_address(),
            config.funder_address,
            None,
            False,
            f"balance/allowance check failed: {type(exc).__name__}",
        )
    if require_entry_capacity and balance is not None and balance < required_balance_usd:
        return LivePreflightResult(
            False,
            client.signer_address(),
            config.funder_address,
            balance,
            allowance_ok,
            "insufficient balance",
        )
    if require_entry_capacity and not allowance_ok:
        return LivePreflightResult(
            False, client.signer_address(), config.funder_address, balance, False, "insufficient allowance"
        )
    return LivePreflightResult(
        True, client.signer_address(), config.funder_address, balance, allowance_ok, "ok"
    )


def entries_blocked(
    db: sqlite3.Connection,
    *,
    kill_switch_path: Path = Settings.live_kill_switch_path,
    no_new_entries: bool = False,
) -> bool:
    return (
        no_new_entries
        or kill_switch_path.exists()
        or live_setting_enabled(db, "block_new_entries")
    )


def execute_live_buy(
    db: sqlite3.Connection,
    client: LiveClobClient,
    *,
    token_id: str,
    side: str,
    size_usd: float,
    asks: list[tuple[float, float]],
    reason: str,
    max_price: float | None,
    min_fill_usd: float,
    order_cap_usd: float,
    label: str | None = None,
    event_type: str | None = None,
    event_key: str | None = None,
    kill_switch_path: Path = Settings.live_kill_switch_path,
    no_new_entries: bool = False,
) -> LiveExecutionResult:
    if entries_blocked(db, kill_switch_path=kill_switch_path, no_new_entries=no_new_entries):
        store_live_order(
            db,
            outcome_id=token_id,
            label=label,
            side=f"BUY_{side}",
            action="BUY",
            status="blocked",
            reason="entries blocked",
            event_type=event_type,
            event_key=event_key,
        )
        return LiveExecutionResult("blocked", token_id, f"BUY_{side}", None, 0, 0, "entries blocked")
    if size_usd > order_cap_usd:
        store_live_order(
            db,
            outcome_id=token_id,
            label=label,
            side=f"BUY_{side}",
            action="BUY",
            status="rejected",
            requested_size_usd=size_usd,
            reason="order exceeds live cap",
            event_type=event_type,
            event_key=event_key,
        )
        return LiveExecutionResult("rejected", token_id, f"BUY_{side}", None, 0, 0, "order exceeds live cap")
    quote = calculate_entry(
        token_id,
        size_usd,
        asks,
        max_order_usd=order_cap_usd,
        max_price=max_price,
        min_fill_usd=min_fill_usd,
    )
    if quote.status != "fillable":
        store_live_order(
            db,
            outcome_id=token_id,
            label=label,
            side=f"BUY_{side}",
            action="BUY",
            status="rejected",
            requested_size_usd=size_usd,
            limit_price=quote.limit_price,
            reason=quote.reason,
            event_type=event_type,
            event_key=event_key,
        )
        return LiveExecutionResult("rejected", token_id, f"BUY_{side}", None, 0, 0, quote.reason)
    exposure_after = live_total_open_exposure(db) + quote.estimated_cost_usd
    if exposure_after > Settings.live_total_open_exposure_cap_usd:
        store_live_order(
            db,
            outcome_id=token_id,
            label=label,
            side=f"BUY_{side}",
            action="BUY",
            status="rejected",
            requested_size_usd=size_usd,
            limit_price=quote.limit_price,
            reason="live open exposure cap reached",
            event_type=event_type,
            event_key=event_key,
        )
        return LiveExecutionResult("rejected", token_id, f"BUY_{side}", None, 0, 0, "live open exposure cap reached")

    balance_before = _sellable_token_balance(client, token_id)
    request = {"token_id": token_id, "price": quote.limit_price, "size_usd": quote.estimated_cost_usd, "order_type": "FAK"}
    try:
        response = client.buy_fak(token_id, float(quote.limit_price), quote.estimated_cost_usd)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        details = {"token_id": token_id, "error": str(exc)}
        if _is_insufficient_balance_or_allowance_error(str(exc)):
            count = _record_insufficient_balance_submit_error(db)
            details["insufficient_balance_error_count"] = count
            if count >= INSUFFICIENT_BALANCE_BLOCK_THRESHOLD:
                set_live_setting(db, "block_new_entries", True)
                details["block_new_entries"] = True
        else:
            _reset_insufficient_balance_submit_errors(db)
        store_risk_event(db, "live_order_submit_failed", "critical", details)
        store_live_order(
            db,
            outcome_id=token_id,
            label=label,
            side=f"BUY_{side}",
            action="BUY",
            status="error",
            requested_size_usd=size_usd,
            limit_price=quote.limit_price,
            reason=reason,
            error=error,
            raw_request=request,
            event_type=event_type,
            event_key=event_key,
        )
        return LiveExecutionResult("error", token_id, f"BUY_{side}", None, 0, 0, error)
    _reset_insufficient_balance_submit_errors(db)
    clob_order_id = _order_id(response)
    order_id = store_live_order(
        db,
        outcome_id=token_id,
        label=label,
        side=f"BUY_{side}",
        action="BUY",
        status="submitted",
        clob_order_id=clob_order_id,
        order_type="FAK",
        requested_size_usd=size_usd,
        limit_price=quote.limit_price,
        reason=reason,
        raw_request=request,
        raw_response=response,
        event_type=event_type,
        event_key=event_key,
    )
    reconcile = _reconcile_payload(
        client.reconcile_order(clob_order_id, token_id),
        clob_order_id,
        token_id,
        fallback_response=response,
        action="BUY",
    )
    fill_price, fill_size_usd, fill_shares = _fill_values(
        reconcile,
        quote.estimated_avg_price,
        quote.estimated_cost_usd,
        quote.estimated_shares,
        allow_default_fill=_allow_default_fill_values(reconcile),
    )
    fill_price, fill_size_usd, fill_shares, balance_status = _reconcile_live_buy_fill_to_balance(
        db,
        client,
        token_id=token_id,
        fill_price=fill_price,
        fill_size_usd=fill_size_usd,
        fill_shares=fill_shares,
        balance_before=balance_before,
    )
    status = _live_reconcile_status(reconcile, fill_shares, "submitted")
    if balance_status is not None:
        status = balance_status
    update_live_order_reconcile(
        db,
        order_id,
        status=status,
        fill_price=fill_price,
        fill_size_usd=fill_size_usd,
        fill_shares=fill_shares,
        raw_reconcile=reconcile,
    )
    if fill_shares > 0:
        _apply_live_buy_fill(db, token_id, fill_shares, fill_size_usd)
    return LiveExecutionResult(status, token_id, f"BUY_{side}", fill_price, fill_size_usd, fill_shares, reason, clob_order_id)


def _reconcile_live_buy_fill_to_balance(
    db: sqlite3.Connection,
    client: LiveClobClient,
    *,
    token_id: str,
    fill_price: float | None,
    fill_size_usd: float,
    fill_shares: float,
    balance_before: float | None,
) -> tuple[float | None, float, float, str | None]:
    balance_after = _sellable_token_balance(client, token_id)
    if balance_before is None or balance_after is None or fill_shares <= 0:
        return fill_price, fill_size_usd, fill_shares, None
    actual_delta = max(balance_after - balance_before, 0.0)
    if fill_shares <= actual_delta + 1e-8:
        return fill_price, fill_size_usd, fill_shares, None
    store_risk_event(
        db,
        "live_buy_balance_mismatch",
        "warning",
        {
            "token_id": token_id,
            "balance_before": balance_before,
            "balance_after": balance_after,
            "actual_balance_delta": actual_delta,
            "reported_fill_shares": fill_shares,
            "reported_fill_size_usd": fill_size_usd,
        },
    )
    if actual_delta <= 1e-8:
        return fill_price, 0.0, 0.0, "unknown_fill"
    adjusted_size = (
        fill_size_usd * (actual_delta / fill_shares)
        if fill_size_usd > 0
        else (fill_price or 0.0) * actual_delta
    )
    return fill_price, adjusted_size, actual_delta, None


def execute_live_sell(
    db: sqlite3.Connection,
    client: LiveClobClient,
    *,
    token_id: str,
    bids: list[tuple[float, float]],
    reason: str,
    label: str | None = None,
    event_type: str | None = None,
    event_key: str | None = None,
) -> LiveExecutionResult:
    pos = get_live_position(db, token_id)
    if pos is None or float(pos["net_shares"]) <= 0:
        store_live_order(
            db,
            outcome_id=token_id,
            label=label,
            side="SELL",
            action="SELL",
            status="rejected",
            reason="no live position",
            event_type=event_type,
            event_key=event_key,
        )
        return LiveExecutionResult("rejected", token_id, "SELL", None, 0, 0, "no live position")
    best_bid = max((price for price, _ in bids), default=None)
    if best_bid is None:
        store_live_order(
            db,
            outcome_id=token_id,
            label=label,
            side="SELL",
            action="SELL",
            status="rejected",
            reason="no bid depth",
            event_type=event_type,
            event_key=event_key,
        )
        return LiveExecutionResult("rejected", token_id, "SELL", None, 0, 0, "no bid depth")
    shares = float(pos["net_shares"])
    sellable_shares = _sellable_token_balance(client, token_id)
    if sellable_shares is not None and sellable_shares < shares:
        missing_shares = max(shares - max(sellable_shares, 0.0), 0.0)
        store_risk_event(
            db,
            "live_position_balance_mismatch",
            "warning",
            {
                "token_id": token_id,
                "local_shares": shares,
                "clob_sellable_shares": sellable_shares,
            },
        )
        if missing_shares > 1e-8:
            _record_live_balance_adjustment(
                db,
                token_id=token_id,
                label=label,
                missing_shares=missing_shares,
                local_shares=shares,
                clob_sellable_shares=sellable_shares,
                event_type=event_type,
                event_key=event_key,
            )
        shares = max(sellable_shares, 0.0)
    submitted_shares = _floor_decimal(shares, "0.01")
    if submitted_shares <= 0:
        store_live_order(
            db,
            outcome_id=token_id,
            label=label,
            side="SELL",
            action="SELL",
            status="rejected",
            reason="no sellable token balance"
            if sellable_shares is not None
            else "position rounds below sellable share precision",
            event_type=event_type,
            event_key=event_key,
        )
        reason_text = (
            "no sellable token balance"
            if sellable_shares is not None
            else "position rounds below sellable share precision"
        )
        return LiveExecutionResult(
            "rejected",
            token_id,
            "SELL",
            None,
            0,
            0,
            reason_text,
        )
    request = {"token_id": token_id, "price": best_bid, "shares": submitted_shares, "order_type": "FAK"}
    try:
        response = client.sell_fak(token_id, best_bid, submitted_shares)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        store_risk_event(db, "live_order_submit_failed", "critical", {"token_id": token_id, "error": str(exc)})
        store_live_order(
            db,
            outcome_id=token_id,
            label=label,
            side="SELL",
            action="SELL",
            status="error",
            limit_price=best_bid,
            reason=reason,
            error=error,
            raw_request=request,
            event_type=event_type,
            event_key=event_key,
        )
        return LiveExecutionResult("error", token_id, "SELL", None, 0, 0, error)
    clob_order_id = _order_id(response)
    order_id = store_live_order(
        db,
        outcome_id=token_id,
        label=label,
        side="SELL",
        action="SELL",
        status="submitted",
        clob_order_id=clob_order_id,
        order_type="FAK",
        requested_shares=submitted_shares,
        limit_price=best_bid,
        reason=reason,
        raw_request=request,
        raw_response=response,
        event_type=event_type,
        event_key=event_key,
    )
    reconcile = _reconcile_payload(
        client.reconcile_order(clob_order_id, token_id),
        clob_order_id,
        token_id,
        fallback_response=response,
        action="SELL",
    )
    fill_price, proceeds, sold = _fill_values(
        reconcile,
        best_bid,
        submitted_shares * best_bid,
        submitted_shares,
        allow_default_fill=_allow_default_fill_values(reconcile),
    )
    status = _live_reconcile_status(reconcile, sold, "submitted")
    update_live_order_reconcile(
        db,
        order_id,
        status=status,
        fill_price=fill_price,
        fill_size_usd=proceeds,
        fill_shares=sold,
        raw_reconcile=reconcile,
    )
    if sold > 0:
        _apply_live_sell_fill(db, token_id, sold, proceeds)
    return LiveExecutionResult(status, token_id, "SELL", fill_price, proceeds, sold, reason, clob_order_id)


def _record_live_balance_adjustment(
    db: sqlite3.Connection,
    *,
    token_id: str,
    label: str | None,
    missing_shares: float,
    local_shares: float,
    clob_sellable_shares: float,
    event_type: str | None,
    event_key: str | None,
) -> None:
    pos = get_live_position(db, token_id)
    if pos is not None:
        old_avg = float(pos["avg_price"])
        remaining = max(float(pos["net_shares"]) - missing_shares, 0.0)
        avg_price = old_avg if remaining > 0 else 0.0
        realized_pnl = float(pos["realized_pnl"]) - missing_shares * old_avg
        upsert_live_position(db, token_id, remaining, avg_price, realized_pnl)
    details = {
        "token_id": token_id,
        "local_shares": local_shares,
        "clob_sellable_shares": clob_sellable_shares,
        "missing_shares": missing_shares,
    }
    store_live_order(
        db,
        outcome_id=token_id,
        label=label,
        side="RECONCILE_SELL",
        action="SELL",
        status="filled",
        fill_price=0.0,
        fill_size_usd=0.0,
        fill_shares=missing_shares,
        reason="CLOB sellable balance lower than local position; local balance adjustment",
        raw_reconcile=details,
        event_type=event_type,
        event_key=event_key,
    )


def daily_loss_limit_reached(db: sqlite3.Connection, now: datetime | None = None) -> bool:
    current = now or datetime.now(timezone.utc)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    return live_realized_pnl_since(db, start) <= -Settings.live_daily_realized_loss_cap_usd


def _apply_live_buy_fill(
    db: sqlite3.Connection, token_id: str, shares: float, cost_usd: float
) -> None:
    pos = get_live_position(db, token_id)
    old_shares = float(pos["net_shares"]) if pos else 0.0
    old_avg = float(pos["avg_price"]) if pos else 0.0
    old_realized = float(pos["realized_pnl"]) if pos else 0.0
    new_shares = old_shares + shares
    new_avg = (old_avg * old_shares + cost_usd) / new_shares
    upsert_live_position(db, token_id, new_shares, new_avg, old_realized)


def _apply_live_sell_fill(
    db: sqlite3.Connection, token_id: str, sold: float, proceeds: float
) -> None:
    pos = get_live_position(db, token_id)
    if pos is None:
        return
    old_shares = float(pos["net_shares"])
    avg = float(pos["avg_price"])
    old_realized = float(pos["realized_pnl"])
    remaining = max(old_shares - sold, 0.0)
    realized = old_realized + proceeds - sold * avg
    upsert_live_position(db, token_id, remaining, avg, realized)


def reconcile_submitted_live_order(
    db: sqlite3.Connection, client: LiveClobClient, row: sqlite3.Row
) -> LiveExecutionResult:
    token_id = row["outcome_id"]
    side = row["side"]
    action = row["action"] or ("SELL" if side == "SELL" else "BUY")
    raw_response = _decode_live_json(row["raw_response_json"])
    reconcile = _reconcile_payload(
        client.reconcile_order(row["clob_order_id"], token_id),
        row["clob_order_id"],
        token_id,
        fallback_response=raw_response,
        action=action,
    )
    trade_payload = _trade_fill_payload(
        client.trades_for_order(row["clob_order_id"], token_id),
        row["clob_order_id"],
        action,
    )
    if trade_payload is not None:
        reconcile = {
            "order_id": row["clob_order_id"],
            "token_id": token_id,
            **trade_payload,
        }
    default_price = float(row["limit_price"] or 0.0) or None
    if action == "SELL":
        default_shares = float(row["requested_shares"] or 0.0)
        default_size_usd = default_shares * float(default_price or 0.0)
    else:
        default_size_usd = float(row["requested_size_usd"] or 0.0)
        default_shares = (
            default_size_usd / default_price
            if default_price is not None and default_price > 0
            else 0.0
        )
    fill_price, fill_size_usd, fill_shares = _fill_values(
        reconcile,
        default_price,
        default_size_usd,
        default_shares,
        allow_default_fill=_allow_default_fill_values(reconcile),
    )
    status = _live_reconcile_status(
        reconcile, fill_shares, str(row["status"] or "submitted")
    )
    update_live_order_reconcile(
        db,
        int(row["id"]),
        status=status,
        fill_price=fill_price,
        fill_size_usd=fill_size_usd,
        fill_shares=fill_shares,
        raw_reconcile=reconcile,
    )
    if status == "filled":
        if action == "SELL":
            _apply_live_sell_fill(db, token_id, fill_shares, fill_size_usd)
        else:
            _apply_live_buy_fill(db, token_id, fill_shares, fill_size_usd)
    return LiveExecutionResult(
        status,
        token_id,
        side,
        fill_price,
        fill_size_usd,
        fill_shares,
        row["reason"] or "live reconcile",
        row["clob_order_id"],
    )


def rebuild_live_positions_from_filled_orders(db: sqlite3.Connection) -> int:
    rows = db.execute(
        """
        select outcome_id, action, fill_price, fill_size_usd, fill_shares,
               created_at_utc, id
        from live_orders
        where status = 'filled'
          and coalesce(fill_shares, 0) > 0
          and fill_price is not null
        order by created_at_utc asc, id asc
        """
    ).fetchall()
    positions: dict[str, tuple[float, float, float]] = {}
    for row in rows:
        token_id = row["outcome_id"]
        action = row["action"] or ""
        fill_shares = float(row["fill_shares"])
        fill_price = float(row["fill_price"] or 0.0)
        fill_size_usd = float(row["fill_size_usd"] or 0.0)
        if fill_size_usd <= 0 and fill_price > 0:
            fill_size_usd = fill_price * fill_shares
        shares, avg_price, realized_pnl = positions.get(token_id, (0.0, 0.0, 0.0))
        if action == "SELL":
            sold = min(fill_shares, shares)
            realized_pnl += fill_size_usd - sold * avg_price
            shares = max(shares - sold, 0.0)
        else:
            new_shares = shares + fill_shares
            avg_price = (
                (avg_price * shares + fill_size_usd) / new_shares
                if new_shares > 0
                else 0.0
            )
            shares = new_shares
        positions[token_id] = (shares, avg_price, realized_pnl)
    for token_id, (shares, avg_price, realized_pnl) in positions.items():
        upsert_live_position(db, token_id, shares, avg_price, realized_pnl)
    return len(positions)


def _order_id(response: dict) -> str | None:
    for key in ("orderID", "orderId", "order_id", "id"):
        value = response.get(key)
        if value:
            return str(value)
    return None


def _reconcile_payload(
    payload: dict | None,
    order_id: str | None,
    token_id: str,
    *,
    fallback_response: dict | None = None,
    action: str | None = None,
) -> dict:
    if payload is None or str(payload.get("status") or "").lower() in ("", "unknown"):
        fallback = _response_fill_payload(fallback_response, action)
        if fallback is not None:
            return {"order_id": order_id, "token_id": token_id, **fallback}
    if payload is None:
        return {"order_id": order_id, "token_id": token_id, "status": "unknown"}
    response_fill = _response_fill_payload(payload, action)
    if response_fill is not None and (
        "fill_shares" in response_fill or "fill_size_usd" in response_fill
    ):
        return {"order_id": order_id, "token_id": token_id, **payload, **response_fill}
    return payload


def _live_reconcile_status(
    payload: dict, fill_shares: float, default_status: str
) -> str:
    if fill_shares > 0:
        return "filled"
    status = str(payload.get("status") or default_status)
    if status.lower() == "unknown":
        return default_status
    if status.lower() in ("filled", "matched"):
        return "unknown_fill"
    return status


def _allow_default_fill_values(payload: dict) -> bool:
    return payload.get("fill_source") != "order_response"


def _response_fill_payload(response: dict | None, action: str | None) -> dict | None:
    if not response:
        return None
    status = str(response.get("status") or "").lower()
    if status not in ("filled", "matched"):
        return None
    payload = dict(response)
    payload["status"] = "matched"
    payload["fill_source"] = "order_response"
    action = (action or "").upper()
    making = _optional_float_amount(response, "makingAmount", "making_amount")
    taking = _optional_float_amount(response, "takingAmount", "taking_amount")
    if action == "BUY":
        if taking is not None:
            payload["fill_shares"] = taking
        if making is not None:
            payload["fill_size_usd"] = making
    elif action == "SELL":
        if making is not None:
            payload["fill_shares"] = making
        if taking is not None:
            payload["fill_size_usd"] = taking
    return payload


def _trade_fill_payload(
    trades: list[dict], order_id: str | None, action: str | None
) -> dict | None:
    related = [
        trade
        for trade in trades
        if not order_id or _trade_mentions_order(trade, order_id)
    ]
    if not related:
        return None
    action = (action or "").upper()
    total_shares = 0.0
    total_usd = 0.0
    for trade in related:
        size = _trade_matched_size(trade, order_id)
        price = _optional_float(trade, "price", "fill_price", "avg_price")
        if size is None or size <= 0 or price is None or price <= 0:
            continue
        total_shares += size
        total_usd += size * price
    if total_shares <= 0 or total_usd <= 0:
        return None
    return {
        "status": "matched",
        "fill_source": "trade_history",
        "fill_shares": total_shares,
        "fill_size_usd": total_usd,
        "fill_price": total_usd / total_shares,
        "trades": related,
    }


def _trade_matched_size(trade: dict, order_id: str | None) -> float | None:
    for maker_order in trade.get("maker_orders") or trade.get("makerOrders") or []:
        if not isinstance(maker_order, dict):
            continue
        if order_id and not _value_matches_order(maker_order, order_id):
            continue
        value = _optional_float_amount(
            maker_order,
            "matched_amount",
            "matchedAmount",
            "size",
            "amount",
        )
        if value is not None:
            return value
    return _optional_float_amount(trade, "size", "matched_amount", "matchedAmount", "amount")


def _coerce_trade_list(payload) -> list[dict]:
    if isinstance(payload, list):
        return [trade for trade in payload if isinstance(trade, dict)]
    if isinstance(payload, dict):
        for key in ("trades", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [trade for trade in value if isinstance(trade, dict)]
    return []


def _trade_mentions_order(trade: dict, order_id: str) -> bool:
    if _value_matches_order(trade, order_id):
        return True
    for maker_order in trade.get("maker_orders") or trade.get("makerOrders") or []:
        if isinstance(maker_order, dict) and _value_matches_order(maker_order, order_id):
            return True
    return False


def _value_matches_order(payload: dict, order_id: str) -> bool:
    for key in (
        "order_id",
        "orderID",
        "orderId",
        "id",
        "hash",
        "taker_order_id",
        "takerOrderId",
        "taker_order_hash",
        "maker_order_id",
        "makerOrderId",
    ):
        if str(payload.get(key) or "").lower() == order_id.lower():
            return True
    return False


def _fill_values(
    payload: dict,
    default_price: float | None,
    default_size_usd: float,
    default_shares: float,
    *,
    allow_default_fill: bool = True,
) -> tuple[float | None, float, float]:
    status = str(payload.get("status") or "").lower()
    filled = payload.get("filled") or status in ("filled", "matched")
    shares = _optional_float(payload, "fill_shares", "filled_shares", "size_matched", "matched_size")
    size_usd = _optional_float(payload, "fill_size_usd", "filled_amount", "amount_matched", "matched_amount")
    price = _optional_float(payload, "fill_price", "avg_price", "price")
    if filled and allow_default_fill and shares is None:
        shares = default_shares
    if filled and allow_default_fill and size_usd is None:
        size_usd = default_size_usd
    if filled and (size_usd is None or size_usd <= 0) and shares is not None and default_price is not None:
        size_usd = shares * default_price
    if price is None:
        price = default_price
    if filled and (price is None or price <= 0) and shares and size_usd:
        price = size_usd / shares
    return price, float(size_usd or 0.0), float(shares or 0.0)


def _optional_float(payload: dict, *keys: str) -> float | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return float(value)
    return None


def _optional_float_amount(payload: dict, *keys: str) -> float | None:
    value = _optional_float(payload, *keys)
    if value is None:
        return None
    if abs(value) >= 1_000_000:
        return value / 1_000_000
    return value


def _decode_live_json(value: str | None) -> dict:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _usdc_amount(value) -> float:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and stripped.lstrip("-").isdigit():
            return int(stripped) / 1_000_000
    amount = float(value or 0)
    if amount >= 1_000_000:
        return amount / 1_000_000
    return amount


def _balance_from_payload(payload: dict | None) -> float | None:
    if payload is None:
        return None
    for key in ("balance", "usdc", "available"):
        if key in payload:
            return _usdc_amount(payload[key])
    return None


def _asset_amount(value) -> float:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and stripped.lstrip("-").isdigit():
            return int(stripped) / 1_000_000
    amount = float(value or 0)
    if abs(amount) >= 1_000_000:
        return amount / 1_000_000
    return amount


def _asset_amount_from_payload(payload: dict | None) -> float | None:
    if payload is None:
        return None
    for key in ("balance", "available", "shares", "amount"):
        if key in payload:
            return _asset_amount(payload[key])
    return None


def _sellable_token_balance(client: LiveClobClient, token_id: str) -> float | None:
    if not hasattr(client, "token_balance"):
        return None
    try:
        return client.token_balance(token_id)
    except Exception:
        return None


def _allowance_ok_from_payload(
    payload: dict | None, *, required_amount_usd: float = 0.0
) -> bool:
    if payload is None:
        return True
    required = max(required_amount_usd, 0.0)
    allowance = payload.get("allowance")
    if allowance is None:
        allowances = payload.get("allowances")
        if isinstance(allowances, dict):
            return any(_usdc_amount(value) >= required for value in allowances.values())
        return True
    return _usdc_amount(allowance) >= required


def _is_insufficient_balance_or_allowance_error(message: str) -> bool:
    normalized = message.lower()
    return (
        "not enough balance" in normalized
        or "insufficient balance" in normalized
        or "insufficient allowance" in normalized
    )


def _record_insufficient_balance_submit_error(db: sqlite3.Connection) -> int:
    count = _live_setting_int(db, INSUFFICIENT_BALANCE_ERROR_COUNT_SETTING) + 1
    set_live_setting(db, INSUFFICIENT_BALANCE_ERROR_COUNT_SETTING, str(count))
    return count


def _reset_insufficient_balance_submit_errors(db: sqlite3.Connection) -> None:
    if _live_setting_int(db, INSUFFICIENT_BALANCE_ERROR_COUNT_SETTING) != 0:
        set_live_setting(db, INSUFFICIENT_BALANCE_ERROR_COUNT_SETTING, "0")


def _live_setting_int(db: sqlite3.Connection, name: str) -> int:
    try:
        return int(get_live_setting(db, name, "0"))
    except ValueError:
        return 0
