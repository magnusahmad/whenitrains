from __future__ import annotations

import argparse
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import Settings
from .forecast_accuracy import build_forecast_accuracy_report, render_accuracy_report
from .hko import (
    FLW_PAGE_DATA_URL,
    FLW_PAGE_URL,
    OCF_STATION_URL,
    RHRREAD_URL,
    SINCE_MIDNIGHT_URL,
    fetch_response,
    parse_flw_page,
    parse_flw_page_data_json,
    parse_http_datetime_hkt,
    parse_ocf_station_json,
    parse_rhrread_temperature_json,
    parse_since_midnight_csv,
    HKT,
)
from .hourly_accuracy import build_hourly_accuracy_report, render_hourly_accuracy_report
from .polymarket import (
    event_slug_for_date,
    fetch_hk_temperature_event,
    parse_event_markets,
    resolution_rules_warning,
)
from .polymarket import fetch_orderbook
from .dashboard_server import serve as serve_dashboard
from .runner import render_dashboard, run_paper_loop, run_paper_tick
from .scheduler import run_scheduled_paper_loop
from .paper_db import calculate_entry, calculate_exit, execute_paper_buy, execute_paper_sell
from .storage import (
    backup_sqlite_database,
    connect,
    find_outcome_by_label,
    latest_orderbook,
    list_hko_update_times,
    list_hko_forecast_dates,
    list_outcomes,
    list_outcomes_from_date,
    list_outcomes_for_date,
    migrate,
    reset_paper_state,
    record_hko_update_minute,
    store_hko_forecasts,
    store_hko_current_temperature,
    store_hko_observation,
    store_orderbook,
    store_ocf_forecast_samples,
    store_polymarket_event,
    store_raw_snapshot,
    store_risk_event,
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
    scheduled_loop.add_argument("--no-startup-backup", action="store_true")
    ocf_sample = sub.add_parser("sample-ocf")
    ocf_sample.add_argument("--interval-minutes", type=float, default=10.0)
    ocf_sample.add_argument("--hours", type=float, default=24.0)
    ocf_sample.add_argument("--ticks", type=int)
    reset_paper = sub.add_parser("reset-paper")
    reset_paper.add_argument("--yes", action="store_true")
    reset_paper.add_argument("--no-backup", action="store_true")
    backup_db = sub.add_parser("backup-db")
    backup_db.add_argument("--backup-dir")
    backup_db.add_argument("--keep", type=int, default=5)
    accuracy = sub.add_parser("research-forecast-accuracy")
    accuracy.add_argument("--start")
    accuracy.add_argument("--end")
    accuracy.add_argument("--months", type=int, default=12)
    accuracy.add_argument("--cache-dir", default="data/research/hko_forecast_accuracy")
    accuracy.add_argument("--output")
    hourly_accuracy = sub.add_parser("research-hourly-accuracy")
    hourly_accuracy.add_argument("--output")
    sub.add_parser("dashboard")
    dashboard_serve = sub.add_parser("dashboard-serve")
    dashboard_serve.add_argument("--host", default="127.0.0.1")
    dashboard_serve.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if args.command == "backup-db":
        backup_path = backup_sqlite_database(
            db_path,
            backup_dir=Path(args.backup_dir) if args.backup_dir else None,
            keep=args.keep,
        )
        print(f"created backup {backup_path}")
        return 0

    db = connect(db_path)
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
            _discover_markets_for_forecast_dates(db, today)
            _fetch_orderbooks(db)
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
            _discover_markets_for_forecast_dates(db, today)
            _fetch_orderbooks(db)
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
    if args.command == "dashboard-serve":
        migrate(db)
        db.close()
        serve_dashboard(db_path, host=args.host, port=args.port)
        return 0
    if args.command == "paper-scheduler":
        migrate(db)
        if not args.no_startup_backup:
            backup_path = backup_sqlite_database(db_path)
            print(f"created startup backup {backup_path}")
        run_scheduled_paper_loop(
            db,
            fetch_since_midnight=lambda: _fetch_since_midnight(db),
            fetch_bulletin=lambda: _fetch_bulletin(db),
            fetch_current_temperature=lambda: _fetch_current_temperature(db),
            learned_forecast_times=lambda: list_hko_update_times(db, "ocf_station"),
            discover_market=lambda target: _discover_markets_for_forecast_dates(db, target),
            fetch_orderbooks=lambda target: _fetch_orderbooks(db, None, quiet=not args.verbose),
            base_sleep_seconds=args.sleep,
            max_ticks=args.ticks,
            quiet=not args.verbose,
        )
        return 0
    if args.command == "sample-ocf":
        migrate(db)
        interval_seconds = max(args.interval_minutes * 60.0, 0.0)
        ticks = args.ticks
        if ticks is None:
            if args.interval_minutes <= 0:
                print("sample-ocf requires --ticks when --interval-minutes is 0")
                return 2
            ticks = int((args.hours * 60.0) / args.interval_minutes)
        print(
            "ocf-sampler started "
            f"interval={args.interval_minutes:g}m ticks={ticks}"
        )
        previous_hash = None
        for tick in range(ticks):
            snapshot_hash, forecasts = _fetch_ocf_forecast(db)
            changed = previous_hash is None or previous_hash != snapshot_hash
            previous_hash = snapshot_hash
            current = forecasts[0] if forecasts else None
            print(
                f"ocf-sample tick={tick + 1}/{ticks} changed={changed} "
                f"rows={len(forecasts)} "
                f"first={current.forecast_date_hkt.isoformat() if current and current.forecast_date_hkt else 'n/a'} "
                f"high={current.forecast_max_c if current else 'n/a'}"
            )
            if tick + 1 < ticks and interval_seconds:
                time.sleep(interval_seconds)
        return 0
    if args.command == "reset-paper":
        migrate(db)
        if not args.yes:
            print("refusing to reset paper state without --yes")
            return 2
        if not args.no_backup:
            backup_path = backup_sqlite_database(db_path)
            print(f"created backup {backup_path}")
        reset_paper_state(db)
        print("reset paper orders, positions, decisions, and signals")
        return 0
    if args.command == "research-forecast-accuracy":
        end = date.fromisoformat(args.end) if args.end else datetime.now(HKT).date() - timedelta(days=1)
        start = date.fromisoformat(args.start) if args.start else _months_before(end, args.months)
        rows, summaries = build_forecast_accuracy_report(
            start=start,
            end=end,
            cache_dir=Path(args.cache_dir),
        )
        report = render_accuracy_report(rows, summaries, start, end)
        if args.output:
            Path(args.output).write_text(report + "\n")
        print(report)
        return 0
    if args.command == "research-hourly-accuracy":
        rows, summaries = build_hourly_accuracy_report(db)
        report = render_hourly_accuracy_report(rows, summaries)
        if args.output:
            Path(args.output).write_text(report + "\n")
        print(report)
        return 0
    return 1


def _fetch_hko(db) -> None:
    _fetch_since_midnight(db)
    _fetch_bulletin(db)
    _fetch_current_temperature(db)


def _fetch_since_midnight(db) -> str:
    response = fetch_response(SINCE_MIDNIGHT_URL)
    obs_snapshot = store_raw_snapshot(
        db, "hko", SINCE_MIDNIGHT_URL, response.text, response.headers
    )
    store_hko_observation(db, obs_snapshot.id, parse_since_midnight_csv(response.text))
    return response.text


def _fetch_current_temperature(db) -> str:
    response = fetch_response(RHRREAD_URL)
    snapshot = store_raw_snapshot(db, "hko", RHRREAD_URL, response.text, response.headers)
    store_hko_current_temperature(
        db, snapshot.id, parse_rhrread_temperature_json(response.text)
    )
    return response.text


def _fetch_bulletin(db) -> str:
    snapshot_hash, _forecasts = _fetch_ocf_forecast(db)
    return snapshot_hash


def _fetch_ocf_forecast(db) -> tuple[str, list]:
    response = fetch_response(OCF_STATION_URL)
    ocf_snapshot = store_raw_snapshot(
        db, "hko", OCF_STATION_URL, response.text, response.headers
    )
    forecasts, samples = parse_ocf_station_json(response.text)
    store_hko_forecasts(db, ocf_snapshot.id, forecasts)
    store_ocf_forecast_samples(db, ocf_snapshot.id, samples)
    _record_ocf_update_minutes(db, response.headers, forecasts)
    return ocf_snapshot.content_hash, forecasts


def _record_ocf_update_minutes(db, headers: dict[str, str], forecasts: list) -> None:
    seen: set[str] = set()
    for forecast in forecasts[:1]:
        if forecast.update_time and forecast.update_time not in seen:
            seen.add(forecast.update_time)
            record_hko_update_minute(
                db,
                "ocf_station",
                datetime.fromisoformat(forecast.update_time),
                {"kind": "payload_LastModified", "value": forecast.update_time},
            )
    header_last_modified = parse_http_datetime_hkt(headers.get("Last-Modified"))
    if header_last_modified is not None:
        record_hko_update_minute(
            db,
            "ocf_station",
            header_last_modified,
            {
                "kind": "http_Last-Modified",
                "value": headers.get("Last-Modified"),
                "etag": headers.get("Etag") or headers.get("ETag"),
            },
        )


def _fetch_flw_bulletin(db) -> str:
    flw_response = fetch_response(FLW_PAGE_URL)
    flw_snapshot = store_raw_snapshot(
        db, "hko", FLW_PAGE_URL, flw_response.text, flw_response.headers
    )
    flw_forecast = parse_flw_page(flw_response.text)
    payload = flw_response.text
    if flw_forecast.parse_warning:
        flw_data_response = fetch_response(FLW_PAGE_DATA_URL)
        flw_snapshot = store_raw_snapshot(
            db,
            "hko",
            FLW_PAGE_DATA_URL,
            flw_data_response.text,
            flw_data_response.headers,
        )
        flw_forecast = parse_flw_page_data_json(flw_data_response.text)
        payload = flw_data_response.text
    store_hko_forecasts(db, flw_snapshot.id, [flw_forecast])
    return payload


def _discover_market(db, target_date) -> bool:
    event = fetch_hk_temperature_event(event_slug_for_date(target_date))
    if not event:
        return False
    markets = parse_event_markets(event)
    for market in markets:
        store_polymarket_event(db, market)
        warning = resolution_rules_warning(market)
        if warning is not None:
            print(f"🚨🚨🚨 RESOLUTION RULES WARNING: {warning} 🚨🚨🚨")
            store_risk_event(
                db,
                "resolution_rules_mismatch",
                "critical",
                {
                    "slug": market.event_slug,
                    "warning": warning,
                    "resolution_rules_text": market.resolution_rules_text,
                },
            )
    return True


def _discover_markets_for_forecast_dates(db, today_hkt) -> int:
    discovered = 0
    for forecast_date in list_hko_forecast_dates(db, today_hkt.isoformat()):
        if _discover_market(db, date.fromisoformat(forecast_date)):
            discovered += 1
    return discovered


def _fetch_orderbooks(db, target_date=None, quiet: bool = False) -> None:
    outcomes = (
        list_outcomes_for_date(db, target_date.isoformat())
        if target_date is not None
        else list_outcomes_from_date(db, datetime.now(HKT).date().isoformat())
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


def _months_before(day: date, months: int) -> date:
    month_index = day.year * 12 + day.month - 1 - months
    year = month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(day.day, _month_days(year, month)))


def _month_days(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - date(year, month, 1)).days


if __name__ == "__main__":
    raise SystemExit(main())
