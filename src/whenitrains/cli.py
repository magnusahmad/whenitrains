from __future__ import annotations

import argparse
import time
from datetime import date, datetime
from pathlib import Path

from .config import Settings
from .hko import (
    FLW_PAGE_DATA_URL,
    FLW_PAGE_URL,
    SINCE_MIDNIGHT_URL,
    fetch_text,
    parse_flw_page,
    parse_flw_page_data_json,
    parse_since_midnight_csv,
    HKT,
)
from .polymarket import event_slug_for_date, fetch_hk_temperature_event, parse_event_markets
from .polymarket import fetch_orderbook
from .runner import render_dashboard, run_paper_loop, run_paper_tick
from .scheduler import run_scheduled_paper_loop
from .paper_db import calculate_entry, calculate_exit, execute_paper_buy, execute_paper_sell
from .storage import (
    connect,
    find_outcome_by_label,
    latest_orderbook,
    list_outcomes,
    list_outcomes_for_date,
    migrate,
    reset_paper_state,
    store_hko_forecasts,
    store_hko_observation,
    store_orderbook,
    store_polymarket_event,
    store_raw_snapshot,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="whenitrains")
    parser.add_argument("--db", default=str(Settings.database_path))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db")
    sub.add_parser("fetch-hko")
    discover = sub.add_parser("discover-market")
    discover.add_argument("date")
    sub.add_parser("fetch-orderbooks")
    calc_entry = sub.add_parser("calc-entry")
    calc_entry.add_argument("label")
    calc_entry.add_argument("side", choices=["YES", "NO"])
    calc_entry.add_argument("size_usd", type=float)
    paper_buy = sub.add_parser("paper-buy")
    paper_buy.add_argument("label")
    paper_buy.add_argument("side", choices=["YES", "NO"])
    paper_buy.add_argument("size_usd", type=float)
    check_exit = sub.add_parser("check-exit")
    check_exit.add_argument("label")
    check_exit.add_argument("side", choices=["YES", "NO"])
    check_exit.add_argument("--take-profit", type=float, default=Settings.take_profit_move)
    check_exit.add_argument("--max-hold-minutes", type=float, default=Settings.max_hold_minutes)
    paper_sell = sub.add_parser("paper-sell")
    paper_sell.add_argument("label")
    paper_sell.add_argument("side", choices=["YES", "NO"])
    paper_tick = sub.add_parser("paper-tick")
    paper_tick.add_argument("--no-fetch", action="store_true")
    paper_loop = sub.add_parser("paper-loop")
    paper_loop.add_argument("--interval", type=float, default=15.0)
    paper_loop.add_argument("--ticks", type=int)
    paper_loop.add_argument("--no-fetch", action="store_true")
    scheduled_loop = sub.add_parser("paper-scheduler")
    scheduled_loop.add_argument("--sleep", type=float, default=1.0)
    scheduled_loop.add_argument("--ticks", type=int)
    scheduled_loop.add_argument("--verbose", action="store_true")
    reset_paper = sub.add_parser("reset-paper")
    reset_paper.add_argument("--yes", action="store_true")
    sub.add_parser("dashboard")
    args = parser.parse_args(argv)

    db = connect(Path(args.db))
    if args.command == "init-db":
        migrate(db)
        print(f"initialized {args.db}")
        return 0
    if args.command == "fetch-hko":
        migrate(db)
        _fetch_hko(db)
        print("stored HKO snapshots")
        return 0
    if args.command == "discover-market":
        migrate(db)
        target_date = date.fromisoformat(args.date)
        if not _discover_market(db, target_date):
            print("no market event found")
            return 2
        print(f"stored {event_slug_for_date(target_date)}")
        return 0
    if args.command == "fetch-orderbooks":
        migrate(db)
        _fetch_orderbooks(db)
        return 0
    if args.command == "calc-entry":
        outcome = find_outcome_by_label(db, args.label)
        token_id = outcome["yes_token_id"] if args.side == "YES" else outcome["no_token_id"]
        book = latest_orderbook(db, token_id)
        quote = calculate_entry(
            token_id, args.size_usd, book.asks, max_order_usd=Settings.max_order_usd
        )
        print(
            f"{quote.status} {args.label} {args.side} "
            f"size=${quote.requested_size_usd:.2f} limit={_fmt(quote.limit_price)} "
            f"avg={_fmt(quote.estimated_avg_price)} shares={quote.estimated_shares:.4f} "
            f"cost=${quote.estimated_cost_usd:.2f} reason={quote.reason}"
        )
        return 0 if quote.status == "fillable" else 2
    if args.command == "paper-buy":
        outcome = find_outcome_by_label(db, args.label)
        token_id = outcome["yes_token_id"] if args.side == "YES" else outcome["no_token_id"]
        book = latest_orderbook(db, token_id)
        result = execute_paper_buy(
            db,
            token_id=token_id,
            side=args.side,
            size_usd=args.size_usd,
            asks=book.asks,
            max_order_usd=Settings.max_order_usd,
            reason=f"manual paper buy {args.label} {args.side}",
        )
        print(
            f"{result.status} BUY_{args.side} {args.label} "
            f"avg={_fmt(result.fill_price)} cost=${result.fill_size_usd:.2f} "
            f"shares={result.shares:.4f} reason={result.reason}"
        )
        return 0 if result.status == "filled" else 2
    if args.command == "check-exit":
        outcome = find_outcome_by_label(db, args.label)
        token_id = outcome["yes_token_id"] if args.side == "YES" else outcome["no_token_id"]
        book = latest_orderbook(db, token_id)
        current_bid = book.best_bid
        if current_bid is None:
            print("no current bid")
            return 2
        quote = calculate_exit(
            db,
            token_id,
            current_bid,
            args.take_profit,
            max_hold_minutes=args.max_hold_minutes,
        )
        print(
            f"{args.label} {args.side} bid={current_bid:.4f} "
            f"entry={quote.avg_entry_price:.4f} move={quote.price_move:.4f} "
            f"shares={quote.net_shares:.4f} should_sell={quote.should_sell} "
            f"reason={quote.reason}"
        )
        return 0
    if args.command == "paper-sell":
        outcome = find_outcome_by_label(db, args.label)
        token_id = outcome["yes_token_id"] if args.side == "YES" else outcome["no_token_id"]
        book = latest_orderbook(db, token_id)
        result = execute_paper_sell(
            db,
            token_id=token_id,
            bids=book.bids,
            reason=f"manual paper sell {args.label} {args.side}",
        )
        print(
            f"{result.status} SELL {args.label} {args.side} "
            f"avg={_fmt(result.fill_price)} proceeds=${result.fill_size_usd:.2f} "
            f"shares={result.shares:.4f} reason={result.reason}"
        )
        return 0 if result.status == "filled" else 2
    if args.command == "paper-tick":
        migrate(db)
        today = datetime.now(HKT).date()
        if not args.no_fetch:
            _fetch_hko(db)
            _discover_market(db, today)
            _fetch_orderbooks(db, today)
        result = run_paper_tick(db, today_hkt=today)
        print(
            f"paper-tick buys={result.buys_filled}/{result.buys_missed} "
            f"sells={result.sells_filled}/{result.sells_missed} "
            f"signals={result.signals} notes={'; '.join(result.notes)}"
        )
        return 0
    if args.command == "paper-loop":
        migrate(db)
        today = datetime.now(HKT).date()
        if args.no_fetch:
            run_paper_loop(db, tick_seconds=args.interval, max_ticks=args.ticks, today_hkt=today)
            return 0
        ticks = 0
        while args.ticks is None or ticks < args.ticks:
            _fetch_hko(db)
            _discover_market(db, today)
            _fetch_orderbooks(db, today)
            result = run_paper_tick(db, today_hkt=today)
            print(
                f"paper-tick buys={result.buys_filled}/{result.buys_missed} "
                f"sells={result.sells_filled}/{result.sells_missed} "
                f"signals={result.signals} notes={'; '.join(result.notes)}"
            )
            ticks += 1
            if args.ticks is None or ticks < args.ticks:
                time.sleep(args.interval)
        return 0
    if args.command == "dashboard":
        migrate(db)
        print(render_dashboard(db))
        return 0
    if args.command == "paper-scheduler":
        migrate(db)
        run_scheduled_paper_loop(
            db,
            fetch_since_midnight=lambda: _fetch_since_midnight(db),
            fetch_bulletin=lambda: _fetch_bulletin(db),
            discover_market=lambda target: _discover_market(db, target),
            fetch_orderbooks=lambda target: _fetch_orderbooks(db, target, quiet=not args.verbose),
            base_sleep_seconds=args.sleep,
            max_ticks=args.ticks,
            quiet=not args.verbose,
        )
        return 0
    if args.command == "reset-paper":
        migrate(db)
        if not args.yes:
            print("refusing to reset paper state without --yes")
            return 2
        reset_paper_state(db)
        print("reset paper orders, positions, decisions, and signals")
        return 0
    return 1


def _fetch_hko(db) -> None:
    _fetch_since_midnight(db)
    _fetch_bulletin(db)


def _fetch_since_midnight(db) -> str:
    csv_text = fetch_text(SINCE_MIDNIGHT_URL)
    obs_snapshot = store_raw_snapshot(db, "hko", SINCE_MIDNIGHT_URL, csv_text)
    store_hko_observation(db, obs_snapshot.id, parse_since_midnight_csv(csv_text))
    return csv_text


def _fetch_bulletin(db) -> str:
    flw_page = fetch_text(FLW_PAGE_URL)
    flw_snapshot = store_raw_snapshot(db, "hko", FLW_PAGE_URL, flw_page)
    flw_forecast = parse_flw_page(flw_page)
    payload = flw_page
    if flw_forecast.parse_warning:
        flw_data = fetch_text(FLW_PAGE_DATA_URL)
        flw_snapshot = store_raw_snapshot(db, "hko", FLW_PAGE_DATA_URL, flw_data)
        flw_forecast = parse_flw_page_data_json(flw_data)
        payload = flw_data
    store_hko_forecasts(db, flw_snapshot.id, [flw_forecast])
    return payload


def _discover_market(db, target_date) -> bool:
    event = fetch_hk_temperature_event(event_slug_for_date(target_date))
    if not event:
        return False
    markets = parse_event_markets(event)
    for market in markets:
        store_polymarket_event(db, market)
    return True


def _fetch_orderbooks(db, target_date=None, quiet: bool = False) -> None:
    outcomes = (
        list_outcomes_for_date(db, target_date.isoformat())
        if target_date is not None
        else list_outcomes(db)
    )
    for outcome in outcomes:
        try:
            yes_book = fetch_orderbook(outcome["yes_token_id"])
            store_orderbook(db, outcome["yes_token_id"], yes_book)
        except Exception as exc:
            if quiet:
                print(f"orderbook warning {outcome['label']} YES: {exc}")
            else:
                print(f"{outcome['label']} | YES error {exc}")
            yes_book = None
        try:
            no_book = fetch_orderbook(outcome["no_token_id"])
            store_orderbook(db, outcome["no_token_id"], no_book)
        except Exception as exc:
            if quiet:
                print(f"orderbook warning {outcome['label']} NO: {exc}")
            else:
                print(f"{outcome['label']} | NO error {exc}")
            no_book = None
        if not quiet:
            print(
                f"{outcome['label']} | "
                f"YES bid {_fmt(yes_book.best_bid if yes_book else None)} "
                f"ask {_fmt(yes_book.best_ask if yes_book else None)} | "
                f"NO bid {_fmt(no_book.best_bid if no_book else None)} "
                f"ask {_fmt(no_book.best_ask if no_book else None)}"
            )


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


if __name__ == "__main__":
    raise SystemExit(main())
