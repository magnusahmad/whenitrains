from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import Settings
from .hko import (
    FLW_URL,
    FND_URL,
    SINCE_MIDNIGHT_URL,
    fetch_json as fetch_hko_json,
    fetch_text,
    parse_flw_forecast,
    parse_fnd_forecasts,
    parse_since_midnight_csv,
)
from .polymarket import event_slug_for_date, fetch_hk_temperature_event, parse_event_markets
from .polymarket import fetch_orderbook
from .paper_db import calculate_entry, calculate_exit, execute_paper_buy, execute_paper_sell
from .storage import (
    connect,
    find_outcome_by_label,
    latest_orderbook,
    list_outcomes,
    migrate,
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
    paper_sell = sub.add_parser("paper-sell")
    paper_sell.add_argument("label")
    paper_sell.add_argument("side", choices=["YES", "NO"])
    args = parser.parse_args(argv)

    db = connect(Path(args.db))
    if args.command == "init-db":
        migrate(db)
        print(f"initialized {args.db}")
        return 0
    if args.command == "fetch-hko":
        migrate(db)
        csv_text = fetch_text(SINCE_MIDNIGHT_URL)
        obs_snapshot = store_raw_snapshot(db, "hko", SINCE_MIDNIGHT_URL, csv_text)
        store_hko_observation(db, obs_snapshot.id, parse_since_midnight_csv(csv_text))

        fnd_payload = fetch_hko_json(FND_URL)
        fnd_snapshot = store_raw_snapshot(db, "hko", FND_URL, json.dumps(fnd_payload))
        store_hko_forecasts(db, fnd_snapshot.id, parse_fnd_forecasts(fnd_payload))

        flw_payload = fetch_hko_json(FLW_URL)
        flw_snapshot = store_raw_snapshot(db, "hko", FLW_URL, json.dumps(flw_payload))
        store_hko_forecasts(db, flw_snapshot.id, [parse_flw_forecast(flw_payload)])
        print("stored HKO snapshots")
        return 0
    if args.command == "discover-market":
        migrate(db)
        from datetime import date

        target_date = date.fromisoformat(args.date)
        event = fetch_hk_temperature_event(event_slug_for_date(target_date))
        if not event:
            print("no market event found")
            return 2
        markets = parse_event_markets(event)
        for market in markets:
            store_polymarket_event(db, market)
            print(f"stored {market.event_slug} with {len(market.outcomes)} outcomes")
        return 0
    if args.command == "fetch-orderbooks":
        migrate(db)
        for outcome in list_outcomes(db):
            yes_book = fetch_orderbook(outcome["yes_token_id"])
            no_book = fetch_orderbook(outcome["no_token_id"])
            store_orderbook(db, outcome["yes_token_id"], yes_book)
            store_orderbook(db, outcome["no_token_id"], no_book)
            print(
                f"{outcome['label']} | "
                f"YES bid {_fmt(yes_book.best_bid)} ask {_fmt(yes_book.best_ask)} | "
                f"NO bid {_fmt(no_book.best_bid)} ask {_fmt(no_book.best_ask)}"
            )
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
        quote = calculate_exit(db, token_id, current_bid, args.take_profit)
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
    return 1


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


if __name__ == "__main__":
    raise SystemExit(main())
