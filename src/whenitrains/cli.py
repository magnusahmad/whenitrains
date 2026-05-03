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
from .storage import (
    connect,
    migrate,
    store_hko_forecasts,
    store_hko_observation,
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
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
