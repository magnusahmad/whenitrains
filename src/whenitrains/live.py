from __future__ import annotations

import os
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
    live_realized_pnl_since,
    live_setting_enabled,
    live_total_open_exposure,
    store_live_order,
    store_risk_event,
    update_live_order_reconcile,
    upsert_live_position,
)


class LiveTradingError(RuntimeError):
    pass


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

    def reconcile_order(self, order_id: str | None, token_id: str) -> dict:
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
    required = {
        "WHENITRAINS_TRADING_MODE": env.get("WHENITRAINS_TRADING_MODE", ""),
        "POLYMARKET_SIGNATURE_TYPE": env.get("POLYMARKET_SIGNATURE_TYPE", ""),
        "POLYMARKET_FUNDER_ADDRESS": env.get("POLYMARKET_FUNDER_ADDRESS", ""),
        "POLYMARKET_API_KEY": env.get("POLYMARKET_API_KEY", ""),
        "POLYMARKET_API_SECRET": env.get("POLYMARKET_API_SECRET", ""),
        "POLYMARKET_API_PASSPHRASE": env.get("POLYMARKET_API_PASSPHRASE", ""),
    }
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
    kill_switch_path: Path = Settings.live_kill_switch_path,
) -> LivePreflightResult:
    if config.trading_mode != "live":
        return LivePreflightResult(False, None, config.funder_address, None, False, "not live mode")
    if entries_blocked(db, kill_switch_path=kill_switch_path):
        return LivePreflightResult(
            False, client.signer_address(), config.funder_address, None, False, "entries blocked"
        )
    try:
        if hasattr(client, "balance_allowance"):
            payload = client.balance_allowance()
            balance = _balance_from_payload(payload)
            allowance_ok = _allowance_ok_from_payload(payload)
        else:
            balance = client.balance_usd()
            allowance_ok = client.allowance_ok()
    except Exception as exc:
        return LivePreflightResult(
            False,
            client.signer_address(),
            config.funder_address,
            None,
            False,
            f"balance/allowance check failed: {type(exc).__name__}",
        )
    if balance is not None and balance < Settings.live_manual_order_cap_usd:
        return LivePreflightResult(
            False, client.signer_address(), config.funder_address, balance, allowance_ok, "insufficient balance"
        )
    if not allowance_ok:
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

    request = {"token_id": token_id, "price": quote.limit_price, "size_usd": quote.estimated_cost_usd, "order_type": "FAK"}
    try:
        response = client.buy_fak(token_id, float(quote.limit_price), quote.estimated_cost_usd)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        store_risk_event(db, "live_order_submit_failed", "critical", {"token_id": token_id, "error": str(exc)})
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
        client.reconcile_order(clob_order_id, token_id), clob_order_id, token_id
    )
    fill_price, fill_size_usd, fill_shares = _fill_values(
        reconcile,
        quote.estimated_avg_price,
        quote.estimated_cost_usd,
        quote.estimated_shares,
    )
    status = "filled" if fill_shares > 0 else "submitted"
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
    submitted_shares = _floor_decimal(shares, "0.01")
    if submitted_shares <= 0:
        store_live_order(
            db,
            outcome_id=token_id,
            label=label,
            side="SELL",
            action="SELL",
            status="rejected",
            reason="position rounds below sellable share precision",
            event_type=event_type,
            event_key=event_key,
        )
        return LiveExecutionResult(
            "rejected",
            token_id,
            "SELL",
            None,
            0,
            0,
            "position rounds below sellable share precision",
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
        client.reconcile_order(clob_order_id, token_id), clob_order_id, token_id
    )
    fill_price, proceeds, sold = _fill_values(
        reconcile, best_bid, submitted_shares * best_bid, submitted_shares
    )
    status = "filled" if sold > 0 else "submitted"
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


def _order_id(response: dict) -> str | None:
    for key in ("orderID", "orderId", "order_id", "id"):
        value = response.get(key)
        if value:
            return str(value)
    return None


def _reconcile_payload(
    payload: dict | None, order_id: str | None, token_id: str
) -> dict:
    if payload is None:
        return {"order_id": order_id, "token_id": token_id, "status": "unknown"}
    return payload


def _fill_values(
    payload: dict,
    default_price: float | None,
    default_size_usd: float,
    default_shares: float,
) -> tuple[float | None, float, float]:
    filled = payload.get("filled") or payload.get("status") in ("filled", "matched")
    shares = _optional_float(payload, "fill_shares", "filled_shares", "size_matched", "matched_size")
    size_usd = _optional_float(payload, "fill_size_usd", "filled_amount", "amount_matched", "matched_amount")
    price = _optional_float(payload, "fill_price", "avg_price", "price")
    if filled and shares is None:
        shares = default_shares
    if filled and size_usd is None:
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


def _usdc_amount(value) -> float:
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


def _allowance_ok_from_payload(payload: dict | None) -> bool:
    if payload is None:
        return True
    allowance = payload.get("allowance")
    if allowance is None:
        allowances = payload.get("allowances")
        if isinstance(allowances, dict):
            return any(float(value or 0) > 0 for value in allowances.values())
        return True
    return float(allowance) > 0
