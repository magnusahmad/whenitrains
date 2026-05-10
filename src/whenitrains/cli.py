from __future__ import annotations

import argparse
import getpass
import json
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

from .backtest import dumps_result_json, render_backtest_result, run_backtest_day
from .alerting import alert_sink_from_env
from .config import Settings
from .experiments.backtest import (
    dumps_experiment_result_json,
    render_experiment_result,
    run_experiment_backtest_day,
)
from .experiments.config import ExperimentConfig
from .forecast_accuracy import build_forecast_accuracy_report, render_accuracy_report
from .hko import (
    AWS_GIS_FORECAST_URL,
    AWS_GIS_READINGS_URL,
    FLW_PAGE_DATA_URL,
    FLW_PAGE_URL,
    OCF_STATION_URL,
    RHRREAD_URL,
    SINCE_MIDNIGHT_URL,
    fetch_response,
    parse_aws_gis_current_temperature,
    parse_flw_page,
    parse_flw_page_data_json,
    parse_http_datetime_hkt,
    parse_ocf_station_json,
    parse_rhrread_temperature_json,
    parse_since_midnight_csv,
    HKT,
)
from .hourly_accuracy import build_hourly_accuracy_report, render_hourly_accuracy_report
from .low_latency import FastDecisionWorker, LowLatencyEventQueue
from .live_runtime import LiveWebSocketRuntime
from .operational import (
    LiveSchedulerLock,
    LiveSchedulerLockError,
    evaluate_live_startup_health,
    freeze_new_entries_for_health_failures,
)
from .polymarket import (
    event_slugs_for_date,
    fetch_hk_temperature_event,
    parse_event_markets,
    resolution_rules_warning,
)
from .polymarket import fetch_orderbook
from .dashboard_server import serve as serve_dashboard
from .runner import RunnerResult, render_dashboard, run_live_tick, run_paper_loop, run_paper_tick
from .scheduler import run_scheduled_paper_loop
from .paper_db import calculate_entry, calculate_exit, execute_paper_buy, execute_paper_sell
from .live import (
    LiveTradingError,
    PolymarketClobClient,
    enforce_live_kill_switch_exits,
    execute_live_buy,
    execute_live_sell,
    find_live_position_drifts,
    freeze_new_entries_for_stale_submitted_orders,
    load_live_config,
    preflight_live,
    read_live_env_file,
    reconcile_pending_live_orders,
    repair_live_position_drifts,
    render_live_env_exports,
    store_keychain_secret,
)
from .storage import (
    backup_sqlite_database,
    connect,
    find_outcome_by_label,
    find_outcome_by_label_and_filters,
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
    set_live_setting,
    live_setting_enabled,
    live_dashboard_stats,
    latency_duration_summary,
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
    latency_report = sub.add_parser("latency-report")
    latency_report.add_argument("start_stage")
    latency_report.add_argument("end_stage")
    hko_timing_report = sub.add_parser("hko-source-timing-report")
    hko_timing_report.add_argument("--endpoint-contains")
    hko_timing_report.add_argument("--limit", type=int, default=200)
    readiness_report = sub.add_parser("low-latency-readiness-report")
    readiness_report.add_argument("--hko-endpoint-contains", default="latestReadings")
    readiness_report.add_argument("--hko-limit", type=int, default=200)
    readiness_report.add_argument("--require-evidence", action="store_true")
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
    backtest = sub.add_parser("backtest-day")
    backtest.add_argument("date")
    backtest.add_argument("--replay-db")
    backtest.add_argument(
        "--tick-source",
        choices=["scheduler", "data", "both"],
        default="scheduler",
        help="scheduler uses historical paper decision timestamps; data uses stored data fetch timestamps",
    )
    backtest.add_argument("--include-orderbook-ticks", action="store_true")
    backtest.add_argument("--max-ticks", type=int)
    backtest.add_argument("--json", action="store_true")
    experiment_backtest = sub.add_parser("experiment-backtest-day")
    experiment_backtest.add_argument("date")
    experiment_backtest.add_argument("--config")
    experiment_backtest.add_argument("--replay-db")
    experiment_backtest.add_argument(
        "--tick-source",
        choices=["scheduler", "data", "both"],
        default="data",
        help="scheduler uses historical paper decision timestamps; data uses stored data fetch timestamps",
    )
    experiment_backtest.add_argument("--include-orderbook-ticks", action="store_true")
    experiment_backtest.add_argument("--max-ticks", type=int)
    experiment_backtest.add_argument("--json", action="store_true")
    live_store_key = sub.add_parser("live-store-hot-key")
    live_store_key.add_argument("--service", default=Settings.live_keychain_service)
    live_store_key.add_argument("--account", default=Settings.live_keychain_account)
    live_env_exports = sub.add_parser("live-env-exports")
    live_env_exports.add_argument("--env-file", default=".env")
    live_preflight = sub.add_parser("live-preflight")
    live_preflight.add_argument("--live", action="store_true")
    live_auth_smoke = sub.add_parser("live-auth-smoke")
    live_auth_smoke.add_argument("--live", action="store_true")
    live_network_smoke = sub.add_parser("live-network-smoke")
    live_network_smoke.add_argument("--live", action="store_true")
    live_network_smoke.add_argument("--seconds", type=float, default=5.0)
    live_network_smoke.add_argument("--require-connected", action="store_true")
    live_buy = sub.add_parser("live-buy")
    live_buy.add_argument("label")
    live_buy.add_argument("side", choices=["YES", "NO"])
    live_buy.add_argument("size_usd", type=float)
    live_buy.add_argument("--date")
    live_buy.add_argument("--market-kind", choices=["highest", "lowest"])
    live_buy.add_argument("--live", action="store_true")
    live_buy.add_argument("--yes-i-understand", action="store_true")
    live_sell = sub.add_parser("live-sell")
    live_sell.add_argument("label")
    live_sell.add_argument("side", choices=["YES", "NO"])
    live_sell.add_argument("--date")
    live_sell.add_argument("--market-kind", choices=["highest", "lowest"])
    live_sell.add_argument("--live", action="store_true")
    live_sell.add_argument("--yes-i-understand", action="store_true")
    live_reconcile = sub.add_parser("live-reconcile")
    live_reconcile.add_argument("--live", action="store_true")
    live_cancel_order = sub.add_parser("live-cancel-order")
    live_cancel_order.add_argument("order_id")
    live_cancel_order.add_argument("--live", action="store_true")
    live_cancel_order.add_argument("--yes-i-understand", action="store_true")
    live_cancel_all = sub.add_parser("live-cancel-all")
    live_cancel_all.add_argument("--live", action="store_true")
    live_cancel_all.add_argument("--yes-i-understand", action="store_true")
    live_tick = sub.add_parser("live-tick")
    live_tick.add_argument("--live", action="store_true")
    live_tick.add_argument("--no-fetch", action="store_true")
    live_scheduled = sub.add_parser("live-scheduler")
    live_scheduled.add_argument("--live", action="store_true")
    live_scheduled.add_argument("--sleep", type=float, default=1.0)
    live_scheduled.add_argument("--ticks", type=int)
    live_scheduled.add_argument("--verbose", action="store_true")
    live_scheduled.add_argument("--no-startup-backup", action="store_true")
    live_scheduled.add_argument("--no-websockets", action="store_true")
    live_kill = sub.add_parser("live-kill-switch")
    live_kill.add_argument("--block-new-entries", action="store_true")
    live_kill.add_argument("--allow-new-entries", action="store_true")
    live_kill.add_argument("--exit-on-kill-switch", action="store_true")
    live_kill.add_argument("--no-exit-on-kill-switch", action="store_true")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if args.command == "live-env-exports":
        try:
            values = read_live_env_file(Path(args.env_file))
        except OSError as exc:
            print(f"cannot read live env file: {exc}")
            return 2
        lines, missing = render_live_env_exports(values)
        if missing:
            print("missing live env values: " + ", ".join(missing))
            return 2
        for line in lines:
            print(line)
        return 0

    if args.command == "backup-db":
        backup_path = backup_sqlite_database(
            db_path,
            backup_dir=Path(args.backup_dir) if args.backup_dir else None,
            keep=args.keep,
        )
        print(f"created backup {backup_path}")
        return 0

    db = connect(db_path)
    if args.command == "latency-report":
        migrate(db)
        summary = latency_duration_summary(db, args.start_stage, args.end_stage)
        print(
            f"{summary['start_stage']} -> {summary['end_stage']} "
            f"count={summary['count']} "
            f"p50={_fmt_seconds(summary['p50_seconds'])} "
            f"p95={_fmt_seconds(summary['p95_seconds'])} "
            f"p99={_fmt_seconds(summary['p99_seconds'])}"
        )
        return 0
    if args.command == "hko-source-timing-report":
        migrate(db)
        report = _hko_source_timing_report(
            db,
            endpoint_contains=args.endpoint_contains,
            limit=args.limit,
        )
        print(report)
        return 0
    if args.command == "low-latency-readiness-report":
        migrate(db)
        report, gate_status = _low_latency_readiness_report(
            db,
            hko_endpoint_contains=args.hko_endpoint_contains,
            hko_limit=args.hko_limit,
        )
        print(report)
        if args.require_evidence and not gate_status["all_passed"]:
            print(
                "readiness evidence missing: "
                + ", ".join(gate_status["missing_gates"])
            )
            return 2
        return 0
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
        print(f"stored temperature markets for {target_date.isoformat()}")
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
    if args.command == "backtest-day":
        db.close()
        target = date.fromisoformat(args.date)
        replay_db = Path(args.replay_db) if args.replay_db else None
        result = run_backtest_day(
            db_path,
            target,
            replay_db=replay_db,
            tick_source=args.tick_source,
            include_orderbook_ticks=args.include_orderbook_ticks,
            max_ticks=args.max_ticks,
        )
        print(dumps_result_json(result) if args.json else render_backtest_result(result))
        return 0
    if args.command == "experiment-backtest-day":
        db.close()
        target = date.fromisoformat(args.date)
        config = ExperimentConfig.from_path(Path(args.config) if args.config else None)
        replay_db = Path(args.replay_db) if args.replay_db else None
        result = run_experiment_backtest_day(
            db_path,
            target,
            config,
            replay_db=replay_db,
            tick_source=args.tick_source,
            include_orderbook_ticks=args.include_orderbook_ticks,
            max_ticks=args.max_ticks,
        )
        print(
            dumps_experiment_result_json(result)
            if args.json
            else render_experiment_result(result)
        )
        return 0
    if args.command == "live-store-hot-key":
        private_key = getpass.getpass("Polymarket bot private key: ")
        if not private_key.startswith("0x"):
            print("refusing to store key: expected 0x-prefixed private key")
            return 2
        store_keychain_secret(args.service, args.account, private_key)
        print(f"stored hot key in Keychain service={args.service} account={args.account}")
        return 0
    if args.command == "live-preflight":
        migrate(db)
        if not args.live:
            print("refusing live preflight without --live")
            return 2
        print("LIVE TRADING preflight")
        try:
            config = load_live_config()
            client = PolymarketClobClient(config)
            result = preflight_live(
                db,
                client,
                config,
                required_balance_usd=Settings.live_scheduler_order_cap_usd,
            )
        except LiveTradingError as exc:
            print(f"live preflight failed: {exc}")
            return 2
        print(
            f"preflight ok={result.ok} signer={result.signer_address or 'n/a'} "
            f"funder={result.funder_address or 'n/a'} "
            f"balance={_fmt(result.balance_usd)} allowance_ok={result.allowance_ok} "
            f"reason={result.reason}"
        )
        return 0 if result.ok else 2
    if args.command == "live-auth-smoke":
        migrate(db)
        if not args.live:
            print("refusing live auth smoke without --live")
            return 2
        print("LIVE TRADING auth smoke")
        try:
            config = load_live_config()
            client = PolymarketClobClient(config)
            result = preflight_live(
                db,
                client,
                config,
                required_balance_usd=Settings.live_scheduler_order_cap_usd,
            )
        except LiveTradingError as exc:
            print(f"live auth smoke failed: {exc}")
            return 2
        print(
            f"auth ok={result.ok} signer={result.signer_address or 'n/a'} "
            f"funder={result.funder_address or 'n/a'} "
            f"balance={_fmt(result.balance_usd)} allowance_ok={result.allowance_ok} "
            f"reason={result.reason}"
        )
        return 0 if result.ok else 2
    if args.command == "live-network-smoke":
        migrate(db)
        if not args.live:
            print("refusing live network smoke without --live")
            return 2
        print("LIVE TRADING network smoke")
        websocket_runtime = None
        try:
            config = load_live_config()
            websocket_runtime = LiveWebSocketRuntime.for_live_scheduler(
                db_path=db_path,
                config=config,
                min_date_hkt=datetime.now(HKT).date().isoformat(),
            )
            websocket_runtime.start()
            time.sleep(max(args.seconds, 0.0))
            all_running = websocket_runtime.all_running
            print(f"live network smoke websocket_all_running={all_running}")
            client_statuses = list(getattr(websocket_runtime, "client_statuses", ()))
            for index, status in enumerate(
                client_statuses, start=1
            ):
                print(
                    "live network smoke "
                    f"client{index}_connected_once={status.connected_once} "
                    f"client{index}_attempts={status.connection_attempts} "
                    f"client{index}_messages={status.messages_applied} "
                    f"client{index}_last_error={status.last_error or 'n/a'}"
                )
            connected_once_all = bool(client_statuses) and all(
                status.connected_once for status in client_statuses
            )
            if args.require_connected:
                required_clients = 2
                print(
                    "live network smoke "
                    f"client_count={len(client_statuses)} "
                    f"required_clients={required_clients}"
                )
                print(
                    "live network smoke "
                    f"connected_once_all={connected_once_all}"
                )
                has_required_clients = len(client_statuses) >= required_clients
                return 0 if all_running and connected_once_all and has_required_clients else 2
            return 0 if all_running else 2
        except LiveTradingError as exc:
            print(f"live network smoke failed: {exc}")
            return 2
        finally:
            if websocket_runtime is not None:
                websocket_runtime.stop(timeout=5)
    if args.command == "live-buy":
        migrate(db)
        if not args.live or not args.yes_i_understand:
            print("refusing live buy without --live and --yes-i-understand")
            return 2
        if args.size_usd > Settings.live_manual_order_cap_usd:
            print(f"refusing live buy above manual cap ${Settings.live_manual_order_cap_usd:.2f}")
            return 2
        print("LIVE TRADING manual buy")
        try:
            config = load_live_config()
            client = PolymarketClobClient(config)
            outcome = find_outcome_by_label_and_filters(
                db,
                args.label,
                target_date_hkt=args.date,
                slug_contains=args.market_kind,
            )
            token_id = outcome["yes_token_id"] if args.side == "YES" else outcome["no_token_id"]
            book = latest_orderbook(db, token_id)
            max_price = book.best_ask + Settings.max_entry_limit_slippage if book.best_ask is not None else None
            result = execute_live_buy(
                db,
                client,
                token_id=token_id,
                side=args.side,
                size_usd=args.size_usd,
                asks=book.asks,
                reason=f"manual live buy {args.label} {args.side}",
                max_price=max_price,
                min_fill_usd=min(args.size_usd, Settings.min_entry_fill_usd),
                order_cap_usd=Settings.live_manual_order_cap_usd,
                label=args.label,
                event_type="manual_live",
            )
        except (LiveTradingError, ValueError) as exc:
            print(f"live buy failed: {exc}")
            return 2
        print(
            f"{result.status} {result.side} {args.label} "
            f"avg={_fmt(result.fill_price)} cost=${result.fill_size_usd:.2f} "
            f"shares={result.shares:.4f} order={result.clob_order_id or 'n/a'} "
            f"reason={result.reason}"
        )
        return 0 if result.status in ("filled", "submitted") else 2
    if args.command == "live-sell":
        migrate(db)
        if not args.live or not args.yes_i_understand:
            print("refusing live sell without --live and --yes-i-understand")
            return 2
        print("LIVE TRADING manual sell")
        try:
            config = load_live_config()
            client = PolymarketClobClient(config)
            outcome = find_outcome_by_label_and_filters(
                db,
                args.label,
                target_date_hkt=args.date,
                slug_contains=args.market_kind,
            )
            token_id = outcome["yes_token_id"] if args.side == "YES" else outcome["no_token_id"]
            book = latest_orderbook(db, token_id)
            result = execute_live_sell(
                db,
                client,
                token_id=token_id,
                bids=book.bids,
                reason=f"manual live sell {args.label} {args.side}",
                label=args.label,
                event_type="manual_live",
            )
        except (LiveTradingError, ValueError) as exc:
            print(f"live sell failed: {exc}")
            return 2
        print(
            f"{result.status} SELL {args.label} {args.side} "
            f"avg={_fmt(result.fill_price)} proceeds=${result.fill_size_usd:.2f} "
            f"shares={result.shares:.4f} order={result.clob_order_id or 'n/a'} "
            f"reason={result.reason}"
        )
        return 0 if result.status in ("filled", "submitted") else 2
    if args.command == "live-reconcile":
        migrate(db)
        if not args.live:
            print("refusing live reconcile without --live")
            return 2
        print("LIVE TRADING reconcile")
        try:
            config = load_live_config()
            client = PolymarketClobClient(config)
            reconcile_result = reconcile_pending_live_orders(db, client)
        except LiveTradingError as exc:
            print(f"live reconcile failed: {exc}")
            return 2
        print(
            f"reconciled {reconcile_result.orders_checked} live orders; "
            f"filled={reconcile_result.orders_filled} "
            f"open={reconcile_result.orders_open} "
            f"errors={reconcile_result.orders_error} "
            f"rebuilt_positions={reconcile_result.rebuilt_positions}"
        )
        return 0
    if args.command == "live-cancel-order":
        migrate(db)
        if not args.live or not args.yes_i_understand:
            print("refusing live cancel without --live and --yes-i-understand")
            return 2
        print("LIVE TRADING cancel order")
        try:
            config = load_live_config()
            client = PolymarketClobClient(config)
            payload = client.cancel_order(args.order_id)
        except LiveTradingError as exc:
            print(f"live cancel failed: {exc}")
            return 2
        print(f"cancelled order {args.order_id}: {payload}")
        return 0
    if args.command == "live-cancel-all":
        migrate(db)
        if not args.live or not args.yes_i_understand:
            print("refusing live cancel-all without --live and --yes-i-understand")
            return 2
        print("LIVE TRADING cancel all")
        try:
            config = load_live_config()
            client = PolymarketClobClient(config)
            payload = client.cancel_all()
        except LiveTradingError as exc:
            print(f"live cancel-all failed: {exc}")
            return 2
        print(f"cancel-all result: {payload}")
        return 0
    if args.command == "live-tick":
        migrate(db)
        if not args.live:
            print("refusing live tick without --live")
            return 2
        print("LIVE TRADING tick")
        try:
            config = load_live_config()
            client = PolymarketClobClient(config)
            today = datetime.now(HKT).date()
            if not args.no_fetch:
                _fetch_hko(db)
                _discover_markets_for_forecast_dates(db, today)
                _fetch_orderbooks(db)
            preflight = preflight_live(
                db,
                client,
                config,
                required_balance_usd=Settings.live_scheduler_order_cap_usd,
                require_entry_capacity=False,
            )
            if not preflight.ok:
                print(f"live preflight failed: {preflight.reason}")
                return 2
            kill_switch_exit = enforce_live_kill_switch_exits(
                db,
                client,
                event_key="live_tick",
            )
            result = run_live_tick(
                db,
                client,
                today_hkt=today,
                order_cap_usd=Settings.live_scheduler_order_cap_usd,
            )
        except (LiveTradingError, ValueError) as exc:
            print(f"live tick failed: {exc}")
            return 2
        print(
            f"live-tick buys={result.buys_filled}/{result.buys_missed} "
            f"sells={result.sells_filled}/{result.sells_missed} "
            f"signals={result.signals} notes={'; '.join(result.notes)}"
        )
        if kill_switch_exit.enabled:
            print(
                "live kill-switch exits "
                f"cancel_all={kill_switch_exit.cancel_all_status} "
                f"sells={kill_switch_exit.sells_filled}/{kill_switch_exit.sells_attempted} "
                f"missed={kill_switch_exit.sells_missed}",
                flush=True,
            )
        return 0
    if args.command == "live-scheduler":
        migrate(db)
        if not args.live:
            print("refusing live scheduler without --live")
            return 2
        print("LIVE TRADING scheduler starting", flush=True)
        if not args.no_startup_backup:
            print("creating startup backup...", flush=True)
            backup_path = backup_sqlite_database(db_path)
            print(f"created startup backup {backup_path}", flush=True)
        stale_submitted = freeze_new_entries_for_stale_submitted_orders(db)
        if stale_submitted:
            print(
                f"blocked new entries: {stale_submitted} stale submitted live orders",
                flush=True,
            )
        print("loading live config and running preflight...", flush=True)
        try:
            config = load_live_config()
            client = PolymarketClobClient(config)
            alert_sink = alert_sink_from_env(os.environ)
            preflight = preflight_live(
                db,
                client,
                config,
                required_balance_usd=Settings.live_scheduler_order_cap_usd,
                require_entry_capacity=False,
            )
            if not preflight.ok:
                print(f"live preflight failed: {preflight.reason}")
                return 2
            kill_switch_exit = enforce_live_kill_switch_exits(
                db,
                client,
                event_key="live_scheduler_startup",
            )
            if kill_switch_exit.enabled:
                print(
                    "live kill-switch exits "
                    f"cancel_all={kill_switch_exit.cancel_all_status} "
                    f"sells={kill_switch_exit.sells_filled}/{kill_switch_exit.sells_attempted} "
                    f"missed={kill_switch_exit.sells_missed}",
                    flush=True,
                )
            reconcile_result = reconcile_pending_live_orders(db, client)
            if reconcile_result.orders_checked:
                print(
                    "live reconcile "
                    f"checked={reconcile_result.orders_checked} "
                    f"filled={reconcile_result.orders_filled} "
                    f"open={reconcile_result.orders_open} "
                    f"errors={reconcile_result.orders_error} "
                    f"rebuilt_positions={reconcile_result.rebuilt_positions}",
                    flush=True,
                )
            drift_count = len(find_live_position_drifts(db, client))
            health = evaluate_live_startup_health(
                market_websocket_connected=not args.no_websockets,
                user_websocket_connected=not args.no_websockets,
                rest_fallback_available=True,
                credentials_valid=True,
                balance_allowance_ok=True,
                stale_submitted_orders=stale_submitted,
                local_clob_drift_count=drift_count,
            )
            if freeze_new_entries_for_health_failures(
                db, health, alert_sink=alert_sink
            ):
                print(
                    "blocked new entries: live startup health failed: "
                    + "; ".join(health.reasons),
                    flush=True,
                )
        except LiveTradingError as exc:
            print(f"live scheduler failed: {exc}")
            return 2
        try:
            scheduler_lock = LiveSchedulerLock(db_path)
            scheduler_lock.acquire()
        except LiveSchedulerLockError as exc:
            print(f"live scheduler failed: {exc}")
            return 2
        with scheduler_lock:
            low_latency_queue = LowLatencyEventQueue()
            websocket_runtime = None
            if not args.no_websockets:
                websocket_runtime = LiveWebSocketRuntime.for_live_scheduler(
                    db_path=db_path,
                    config=config,
                    min_date_hkt=datetime.now(HKT).date().isoformat(),
                )
                websocket_runtime.start()
                print("live websocket runtime started", flush=True)
            aws_actual_poll_fetch = lambda: _fetch_current_temperature_for_path(
                db_path, event_queue=low_latency_queue
            )
            aws_actual_poll_learned_times = lambda: _list_hko_update_times_for_path(
                db_path, "aws_gis_actual"
            )
            def reconcile_watchdog(tick_db):
                kill_switch_exit = enforce_live_kill_switch_exits(
                    tick_db,
                    client,
                    event_key="live_reconcile_watchdog",
                )
                reconcile_result = reconcile_pending_live_orders(tick_db, client)
                drifts = find_live_position_drifts(tick_db, client)
                repaired = 0
                if drifts:
                    repaired = repair_live_position_drifts(
                        tick_db,
                        drifts,
                        event_key="live_reconcile_watchdog",
                    )
                    if repaired:
                        drifts = find_live_position_drifts(tick_db, client)
                websocket_stalled = (
                    websocket_runtime is not None and not websocket_runtime.all_running
                )
                if not drifts and not websocket_stalled:
                    if reconcile_result.orders_checked:
                        return RunnerResult(
                            notes=(
                                "live reconcile "
                                f"checked={reconcile_result.orders_checked} "
                                f"filled={reconcile_result.orders_filled} "
                                f"open={reconcile_result.orders_open} "
                                f"errors={reconcile_result.orders_error} "
                                f"rebuilt_positions={reconcile_result.rebuilt_positions}",
                            )
                        )
                    if kill_switch_exit.enabled:
                        return RunnerResult(
                            notes=(
                                "live kill-switch exits "
                                f"cancel_all={kill_switch_exit.cancel_all_status} "
                                f"sells={kill_switch_exit.sells_filled}/{kill_switch_exit.sells_attempted} "
                                f"missed={kill_switch_exit.sells_missed}",
                            )
                        )
                    if repaired:
                        return RunnerResult(
                            notes=(
                                f"live reconcile watchdog repaired {repaired} local/CLOB drift items",
                            )
                        )
                    return RunnerResult()
                health = evaluate_live_startup_health(
                    market_websocket_connected=not args.no_websockets and not websocket_stalled,
                    user_websocket_connected=not args.no_websockets and not websocket_stalled,
                    rest_fallback_available=True,
                    credentials_valid=True,
                    balance_allowance_ok=True,
                    stale_submitted_orders=0,
                    local_clob_drift_count=len(drifts),
                )
                freeze_new_entries_for_health_failures(
                    tick_db, health, alert_sink=alert_sink
                )
                return RunnerResult(
                    notes=(
                        f"live reconcile watchdog froze entries: {len(drifts)} local/CLOB drift items",
                    )
                )
            try:
                run_scheduled_paper_loop(
                    db,
                    fetch_since_midnight=lambda: _fetch_since_midnight(db),
                    fetch_bulletin=lambda: _fetch_bulletin(
                        db, event_queue=low_latency_queue
                    ),
                    fetch_current_temperature=lambda: _fetch_current_temperature(
                        db, event_queue=low_latency_queue
                    ),
                    learned_forecast_times=lambda: list_hko_update_times(db, "ocf_station"),
                    learned_actual_times=lambda: list_hko_update_times(db, "aws_gis_actual"),
                    discover_market=lambda target: _discover_markets_for_forecast_dates(
                        db, target, event_queue=low_latency_queue
                    ),
                    fetch_orderbooks=lambda target: _fetch_orderbooks(db, None, quiet=not args.verbose),
                    base_sleep_seconds=args.sleep,
                    max_ticks=args.ticks,
                    quiet=not args.verbose,
                    run_tick_fn=lambda tick_db, today_hkt: run_live_tick(
                        tick_db,
                        client,
                        today_hkt=today_hkt,
                        order_cap_usd=Settings.live_scheduler_order_cap_usd,
                        book_cache=websocket_runtime.book_cache
                        if websocket_runtime is not None
                        else None,
                    ),
                    low_latency_event_queue=low_latency_queue,
                    fast_event_handler=lambda tick_db, today_hkt: run_live_tick(
                        tick_db,
                        client,
                        today_hkt=today_hkt,
                        order_cap_usd=Settings.live_scheduler_order_cap_usd,
                        book_cache=websocket_runtime.book_cache
                        if websocket_runtime is not None
                        else None,
                    ),
                    reconcile_watchdog_fn=reconcile_watchdog,
                    aws_actual_poll_fetch=aws_actual_poll_fetch,
                    aws_actual_poll_learned_times=aws_actual_poll_learned_times,
                    output_label="live-scheduler",
                    alert_sink=alert_sink,
                )
            finally:
                if websocket_runtime is not None:
                    websocket_runtime.stop(timeout=5)
                    print("live websocket runtime stopped", flush=True)
        return 0
    if args.command == "live-kill-switch":
        migrate(db)
        if args.block_new_entries and args.allow_new_entries:
            print("choose only one of --block-new-entries or --allow-new-entries")
            return 2
        if args.exit_on_kill_switch and args.no_exit_on_kill_switch:
            print("choose only one of --exit-on-kill-switch or --no-exit-on-kill-switch")
            return 2
        if args.block_new_entries:
            set_live_setting(db, "block_new_entries", True)
        if args.allow_new_entries:
            set_live_setting(db, "block_new_entries", False)
        if args.exit_on_kill_switch:
            set_live_setting(db, "cancel_open_orders_and_exit_positions", True)
        if args.no_exit_on_kill_switch:
            set_live_setting(db, "cancel_open_orders_and_exit_positions", False)
        print(
            "live kill switch "
            f"block_new_entries={live_setting_enabled(db, 'block_new_entries')} "
            "cancel_open_orders_and_exit_positions="
            f"{live_setting_enabled(db, 'cancel_open_orders_and_exit_positions')}"
        )
        return 0
    if args.command == "paper-scheduler":
        migrate(db)
        if not args.no_startup_backup:
            backup_path = backup_sqlite_database(db_path)
            print(f"created startup backup {backup_path}")
        low_latency_queue = LowLatencyEventQueue()
        aws_actual_poll_fetch = lambda: _fetch_current_temperature_for_path(
            db_path, event_queue=low_latency_queue
        )
        aws_actual_poll_learned_times = lambda: _list_hko_update_times_for_path(
            db_path, "aws_gis_actual"
        )
        fast_worker = FastDecisionWorker(
            db_path=db_path,
            event_queue=low_latency_queue,
        )
        fast_worker.start()
        try:
            run_scheduled_paper_loop(
                db,
                fetch_since_midnight=lambda: _fetch_since_midnight(db),
                fetch_bulletin=lambda: _fetch_bulletin(db, event_queue=low_latency_queue),
                fetch_current_temperature=lambda: _fetch_current_temperature(
                    db, event_queue=low_latency_queue
                ),
                learned_forecast_times=lambda: list_hko_update_times(db, "ocf_station"),
                learned_actual_times=lambda: list_hko_update_times(db, "aws_gis_actual"),
                discover_market=lambda target: _discover_markets_for_forecast_dates(
                    db, target, event_queue=low_latency_queue
                ),
                fetch_orderbooks=lambda target: _fetch_orderbooks(db, None, quiet=not args.verbose),
                base_sleep_seconds=args.sleep,
                max_ticks=args.ticks,
                quiet=not args.verbose,
                low_latency_event_queue=low_latency_queue,
                aws_actual_poll_fetch=aws_actual_poll_fetch,
                aws_actual_poll_learned_times=aws_actual_poll_learned_times,
            )
        finally:
            fast_worker.stop(timeout=5)
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


def _fetch_current_temperature_for_path(
    db_path: Path, event_queue: LowLatencyEventQueue | None = None
) -> str:
    worker_db = connect(db_path)
    try:
        return _fetch_current_temperature(worker_db, event_queue=event_queue)
    finally:
        worker_db.close()


def _list_hko_update_times_for_path(db_path: Path, source: str):
    worker_db = connect(db_path)
    try:
        return list_hko_update_times(worker_db, source)
    finally:
        worker_db.close()


def _store_hko_raw_snapshot(db, response) -> object:
    return store_raw_snapshot(
        db,
        "hko",
        response.url,
        response.text,
        response.headers,
        fetch_started_at_utc=response.fetch_started_at_utc,
        headers_received_at_utc=response.headers_received_at_utc,
        payload_received_at_utc=response.payload_received_at_utc,
        response_elapsed_ms=response.response_elapsed_ms,
    )


def _fetch_since_midnight(db) -> str:
    response = fetch_response(SINCE_MIDNIGHT_URL)
    obs_snapshot = _store_hko_raw_snapshot(db, response)
    store_hko_observation(db, obs_snapshot.id, parse_since_midnight_csv(response.text))
    return response.text


def _fetch_current_temperature(db, event_queue: LowLatencyEventQueue | None = None) -> str:
    try:
        response = fetch_response(AWS_GIS_READINGS_URL)
        observation = parse_aws_gis_current_temperature(response.text)
    except Exception as aws_error:
        try:
            response = fetch_response(RHRREAD_URL)
            observation = parse_rhrread_temperature_json(response.text)
        except Exception as fallback_error:
            raise RuntimeError(
                "AWS GIS actual fetch failed and rhrread observation fallback failed: "
                f"{type(aws_error).__name__}: {aws_error}; "
                f"{type(fallback_error).__name__}: {fallback_error}"
            ) from aws_error
        snapshot = _store_hko_raw_snapshot(db, response)
        store_hko_current_temperature(db, snapshot.id, observation, event_queue=event_queue)
        record_hko_update_minute(
            db,
            "rhrread_actual",
            observation.observed_at_hkt,
            {
                "kind": "payload_header",
                "value": observation.observed_at_hkt.isoformat(),
                "endpoint": response.url,
            },
        )
        raise RuntimeError(
            "AWS GIS actual fetch failed; stored rhrread observation fallback only: "
            f"{type(aws_error).__name__}: {aws_error}"
        ) from aws_error
    snapshot = _store_hko_raw_snapshot(db, response)
    store_hko_current_temperature(db, snapshot.id, observation, event_queue=event_queue)
    _record_aws_actual_update_minutes(db, response, observation)
    return response.text


def _record_aws_actual_update_minutes(db, response, observation) -> None:
    seen: set[str] = set()

    def record(update_time: datetime, evidence: dict) -> None:
        minute = update_time.strftime("%H:%M")
        if minute in seen:
            return
        seen.add(minute)
        record_hko_update_minute(db, "aws_gis_actual", update_time, evidence)

    record(
        observation.observed_at_hkt,
        {
            "kind": "payload_header",
            "value": observation.observed_at_hkt.isoformat(),
            "endpoint": response.url,
        },
    )
    header_last_modified = parse_http_datetime_hkt(response.headers.get("Last-Modified"))
    if header_last_modified is not None:
        record(
            header_last_modified,
            {
                "kind": "http_Last-Modified",
                "value": response.headers.get("Last-Modified"),
                "etag": response.headers.get("Etag") or response.headers.get("ETag"),
                "payload_observed_at_hkt": observation.observed_at_hkt.isoformat(),
                "endpoint": response.url,
            },
        )


def _fetch_bulletin(db, *, event_queue=None) -> str:
    snapshot_hash, _forecasts = _fetch_ocf_forecast(db, event_queue=event_queue)
    return snapshot_hash


def _fetch_ocf_forecast(db, *, event_queue=None) -> tuple[str, list]:
    try:
        response = fetch_response(AWS_GIS_FORECAST_URL)
    except Exception:
        response = fetch_response(OCF_STATION_URL)
    ocf_snapshot = _store_hko_raw_snapshot(db, response)
    forecasts, samples = parse_ocf_station_json(response.text)
    store_hko_forecasts(db, ocf_snapshot.id, forecasts)
    store_ocf_forecast_samples(
        db, ocf_snapshot.id, samples, event_queue=event_queue
    )
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
    flw_snapshot = _store_hko_raw_snapshot(db, flw_response)
    flw_forecast = parse_flw_page(flw_response.text)
    payload = flw_response.text
    if flw_forecast.parse_warning:
        flw_data_response = fetch_response(FLW_PAGE_DATA_URL)
        flw_snapshot = _store_hko_raw_snapshot(db, flw_data_response)
        flw_forecast = parse_flw_page_data_json(flw_data_response.text)
        payload = flw_data_response.text
    store_hko_forecasts(db, flw_snapshot.id, [flw_forecast])
    return payload


def _discover_market(db, target_date, *, event_queue=None) -> bool:
    discovered = False
    for slug in event_slugs_for_date(target_date):
        event = fetch_hk_temperature_event(slug)
        if not event:
            continue
        markets = parse_event_markets(event)
        for market in markets:
            store_polymarket_event(db, market, event_queue=event_queue)
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
        discovered = True
    return discovered


def _discover_markets_for_forecast_dates(db, today_hkt, *, event_queue=None) -> int:
    discovered = 0
    for forecast_date in list_hko_forecast_dates(db, today_hkt.isoformat()):
        if _discover_market(
            db, date.fromisoformat(forecast_date), event_queue=event_queue
        ):
            discovered += 1
    return discovered


def _fetch_orderbooks(
    db,
    target_date=None,
    quiet: bool = False,
    max_workers: int = 16,
) -> None:
    outcomes = (
        list_outcomes_for_date(db, target_date.isoformat())
        if target_date is not None
        else list_outcomes_from_date(db, datetime.now(HKT).date().isoformat())
    )
    requests = []
    for outcome_index, outcome in enumerate(outcomes):
        requests.append((outcome_index, outcome, "YES", outcome["yes_token_id"]))
        requests.append((outcome_index, outcome, "NO", outcome["no_token_id"]))
    if not requests:
        return

    books = {}
    errors = {}
    worker_count = max(1, min(max_workers, len(requests)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(fetch_orderbook, token_id): (outcome_index, outcome, side, token_id)
            for outcome_index, outcome, side, token_id in requests
        }
        for future in as_completed(future_map):
            outcome_index, outcome, side, token_id = future_map[future]
            try:
                books[(outcome_index, side)] = future.result()
            except Exception as exc:
                errors[(outcome_index, side)] = exc

    for outcome_index, outcome in enumerate(outcomes):
        yes_book = books.get((outcome_index, "YES"))
        no_book = books.get((outcome_index, "NO"))
        if yes_book is not None:
            store_orderbook(db, outcome["yes_token_id"], yes_book)
        else:
            exc = errors.get((outcome_index, "YES"))
            if quiet:
                print(f"orderbook warning {outcome['label']} YES: {exc}")
            else:
                print(f"{outcome['label']} | YES error {exc}")
        if no_book is not None:
            store_orderbook(db, outcome["no_token_id"], no_book)
        else:
            exc = errors.get((outcome_index, "NO"))
            if quiet:
                print(f"orderbook warning {outcome['label']} NO: {exc}")
            else:
                print(f"{outcome['label']} | NO error {exc}")
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


def _fmt_seconds(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}s"


def _hko_source_timing_report(
    db,
    *,
    endpoint_contains: str | None = None,
    limit: int = 200,
) -> str:
    filters = ["source = 'hko'"]
    params: list[object] = []
    if endpoint_contains:
        filters.append("endpoint like ?")
        params.append(f"%{endpoint_contains}%")
    params.append(limit)
    rows = db.execute(
        f"""
        select endpoint, fetched_at_utc, http_last_modified, fetch_started_at_utc,
               headers_received_at_utc, payload_received_at_utc, response_elapsed_ms
        from raw_snapshots
        where {" and ".join(filters)}
        order by coalesce(fetch_started_at_utc, fetched_at_utc) desc, id desc
        limit ?
        """,
        params,
    ).fetchall()
    ordered = list(reversed(rows))
    response_ms = [
        float(row["response_elapsed_ms"])
        for row in ordered
        if row["response_elapsed_ms"] is not None
    ]
    fetch_offsets = Counter()
    last_modified_offsets = Counter()
    for row in ordered:
        fetch_started = _parse_iso_datetime(row["fetch_started_at_utc"] or row["fetched_at_utc"])
        if fetch_started is not None:
            fetch_offsets[f"{fetch_started.second:02d}"] += 1
        last_modified = row["http_last_modified"]
        if last_modified:
            try:
                parsed_last_modified = parsedate_to_datetime(last_modified)
            except (TypeError, ValueError):
                parsed_last_modified = None
            if parsed_last_modified is not None:
                last_modified_offsets[f"{parsed_last_modified.minute:02d}"] += 1

    lines = [
        f"hko source timing rows={len(ordered)}",
        (
            "response_ms "
            f"p50={_fmt_ms(_nearest_rank(response_ms, 50))} "
            f"p95={_fmt_ms(_nearest_rank(response_ms, 95))} "
            f"p99={_fmt_ms(_nearest_rank(response_ms, 99))}"
        ),
        f"fetch_second_offsets={_format_counter(fetch_offsets)}",
        f"last_modified_minute_offsets={_format_counter(last_modified_offsets)}",
    ]
    if ordered:
        first = ordered[0]
        last = ordered[-1]
        lines.append(
            "window="
            f"{first['fetch_started_at_utc'] or first['fetched_at_utc']}.."
            f"{last['fetch_started_at_utc'] or last['fetched_at_utc']}"
        )
        lines.append(f"latest_endpoint={last['endpoint']}")
    return "\n".join(lines)


def _low_latency_readiness_report(
    db,
    *,
    hko_endpoint_contains: str | None = "latestReadings",
    hko_limit: int = 200,
) -> tuple[str, dict[str, object]]:
    latency_pairs = [
        ("db_committed", "decision_started"),
        ("decision_started", "order_submitted"),
        ("order_submitted", "fill_confirmed"),
        ("db_committed", "decision_completed"),
    ]
    lines = ["low latency readiness report", "latency:"]
    for start_stage, end_stage in latency_pairs:
        summary = latency_duration_summary(db, start_stage, end_stage)
        lines.append(
            f"{start_stage} -> {end_stage} "
            f"count={summary['count']} "
            f"p50={_fmt_seconds(summary['p50_seconds'])} "
            f"p95={_fmt_seconds(summary['p95_seconds'])} "
            f"p99={_fmt_seconds(summary['p99_seconds'])}"
        )
    commit_to_decision = latency_duration_summary(db, "db_committed", "decision_started")
    decision_to_submit = latency_duration_summary(db, "decision_started", "order_submitted")
    submit_to_fill = latency_duration_summary(db, "order_submitted", "fill_confirmed")
    orderbook_age = _decision_orderbook_age_summary(db)
    hko_timing_count = _hko_source_timing_count(
        db,
        endpoint_contains=hko_endpoint_contains,
    )
    live = live_dashboard_stats(db)
    counts = live["counts"]
    gates = [
        _latency_threshold_gate(
            "hko_commit_to_decision_under_1s",
            commit_to_decision,
            threshold_seconds=1.0,
        ),
        _latency_observed_gate(
            "decision_to_submit_observed",
            decision_to_submit,
        ),
        _latency_observed_gate(
            "submit_to_fill_observed",
            submit_to_fill,
        ),
        _value_threshold_gate(
            "orderbook_age_under_cap",
            orderbook_age,
            threshold_seconds=Settings.live_orderbook_cache_max_age_seconds,
        ),
        _count_observed_gate("hko_source_timing_observed", hko_timing_count),
        _live_money_state_gate(live),
    ]
    missing_gates = [gate["name"] for gate in gates if gate["status"] != "pass"]
    lines.extend(
        [
            "evidence gates:",
            *[str(gate["line"]) for gate in gates],
            "live:",
            (
                f"live orders total={counts['orders']} "
                f"submitted={counts['submitted']} "
                f"error={counts['error']}"
            ),
            (
                f"live open_positions={live['open_positions']} "
                f"open_exposure_usd={live['open_exposure_usd']:.2f} "
                f"missing_bid_positions={live['missing_bid_positions']}"
            ),
            (
                "kill_switch "
                f"block_new_entries={live['block_new_entries']} "
                "exit_on_kill_switch="
                f"{live['cancel_open_orders_and_exit_positions']}"
            ),
            "hko:",
            _hko_source_timing_report(
                db,
                endpoint_contains=hko_endpoint_contains,
                limit=hko_limit,
            ),
        ]
    )
    return "\n".join(lines), {
        "all_passed": not missing_gates,
        "missing_gates": missing_gates,
    }


def _decision_orderbook_age_summary(db) -> dict[str, object]:
    rows = db.execute(
        """
        select details_json
        from paper_decisions
        where details_json is not null
        order by id desc
        """
    ).fetchall()
    ages: list[float] = []
    for row in rows:
        try:
            details = json.loads(row["details_json"] or "{}")
        except json.JSONDecodeError:
            continue
        value = details.get("orderbook_state_age_seconds")
        if isinstance(value, (int, float)):
            ages.append(float(value))
    return {
        "count": len(ages),
        "p50_seconds": _nearest_rank(ages, 50),
        "p95_seconds": _nearest_rank(ages, 95),
        "p99_seconds": _nearest_rank(ages, 99),
    }


def _hko_source_timing_count(
    db,
    *,
    endpoint_contains: str | None = None,
) -> int:
    filters = ["source = 'hko'"]
    params: list[object] = []
    if endpoint_contains:
        filters.append("endpoint like ?")
        params.append(f"%{endpoint_contains}%")
    row = db.execute(
        f"""
        select count(*) as count
        from raw_snapshots
        where {" and ".join(filters)}
        """,
        params,
    ).fetchone()
    return int(row["count"] or 0)


def _latency_threshold_gate(
    name: str,
    summary: dict[str, object],
    *,
    threshold_seconds: float,
) -> dict[str, object]:
    p95 = summary["p95_seconds"]
    count = int(summary["count"])
    passed = count > 0 and p95 is not None and float(p95) <= threshold_seconds
    status = "pass" if passed else "missing"
    line = (
        f"gate {name}={status} count={count} "
        f"p95={_fmt_seconds(p95)} threshold={_fmt_seconds(threshold_seconds)}"
    )
    return {"name": name, "status": status, "line": line}


def _latency_observed_gate(name: str, summary: dict[str, object]) -> dict[str, object]:
    count = int(summary["count"])
    status = "pass" if count > 0 else "missing"
    line = f"gate {name}={status} count={count} p95={_fmt_seconds(summary['p95_seconds'])}"
    return {"name": name, "status": status, "line": line}


def _value_threshold_gate(
    name: str,
    summary: dict[str, object],
    *,
    threshold_seconds: float,
) -> dict[str, object]:
    p95 = summary["p95_seconds"]
    count = int(summary["count"])
    passed = count > 0 and p95 is not None and float(p95) <= threshold_seconds
    status = "pass" if passed else "missing"
    line = (
        f"gate {name}={status} count={count} "
        f"p95={_fmt_seconds(p95)} threshold={_fmt_seconds(threshold_seconds)}"
    )
    return {"name": name, "status": status, "line": line}


def _count_observed_gate(name: str, count: int) -> dict[str, object]:
    status = "pass" if count > 0 else "missing"
    line = f"gate {name}={status} count={count}"
    return {"name": name, "status": status, "line": line}


def _live_money_state_gate(live: dict[str, object]) -> dict[str, object]:
    counts = live["counts"]
    submitted = int(counts["submitted"])
    error = int(counts["error"])
    missing_bid_positions = int(live["missing_bid_positions"])
    clear = submitted == 0 and error == 0 and missing_bid_positions == 0
    status = "pass" if clear else "missing"
    line = (
        f"gate live_money_state_clear={status} "
        f"submitted={submitted} "
        f"error={error} "
        f"missing_bid_positions={missing_bid_positions}"
    )
    return {"name": "live_money_state_clear", "status": status, "line": line}


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _nearest_rank(values: list[float], percentile: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, (percentile * len(ordered) + 99) // 100 - 1))
    return ordered[index]


def _fmt_ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}"


def _format_counter(counter: Counter) -> str:
    if not counter:
        return "n/a"
    return ", ".join(f"{key}:{count}" for key, count in counter.items())


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
