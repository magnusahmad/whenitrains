from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .hko import HKT
from .storage import connect, live_dashboard_stats


ACTIVE_PAPER_ORDER_FILTER = """
not exists (
    select 1 from paper_order_exclusions poe where poe.order_id = po.id
)
"""


def _to_unix(iso_ts: str | None) -> int | None:
    if not iso_ts:
        return None
    text = str(iso_ts)
    if len(text) == 14 and text.isdigit():
        try:
            compact = datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=HKT)
            return int(compact.timestamp())
        except ValueError:
            return None
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError:
        return None


def _to_hkt_display(iso_ts: str | None) -> str | None:
    if not iso_ts:
        return None
    text = str(iso_ts)
    if len(text) == 14 and text.isdigit():
        try:
            return datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=HKT).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(text).astimezone(HKT).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return text


def _compact_temp(value) -> str:
    number = _optional_float(value)
    if number is None:
        return "n/a"
    return f"{number:g}°C"


def _ensure_paper_order_exclusions(db: sqlite3.Connection) -> None:
    db.execute(
        """
        create table if not exists paper_order_exclusions (
            order_id integer primary key,
            tag text not null,
            reason text not null,
            created_at_utc text not null
        )
        """
    )


def active_paper_positions(db: sqlite3.Connection) -> dict[str, dict]:
    _ensure_paper_order_exclusions(db)
    orders = db.execute(
        f"""
        select po.id, po.outcome_id, po.side, po.simulated_fill_price,
               po.simulated_fill_size_usd
        from paper_orders po
        where po.status = 'filled'
          and po.simulated_fill_price is not null
          and po.simulated_fill_size_usd is not null
          and {ACTIVE_PAPER_ORDER_FILTER}
        order by po.created_at_utc asc, po.id asc
        """
    ).fetchall()
    positions: dict[str, dict] = {}
    for row in orders:
        token = row["outcome_id"]
        side = row["side"] or ""
        price = _optional_float(row["simulated_fill_price"])
        usd = _optional_float(row["simulated_fill_size_usd"])
        if price is None or price <= 0 or usd is None:
            continue
        pos = positions.setdefault(
            token, {"outcome_id": token, "net_shares": 0.0, "avg_price": 0.0, "realized_pnl": 0.0}
        )
        shares = float(pos["net_shares"])
        avg = float(pos["avg_price"])
        if side.startswith("BUY"):
            bought = usd / price
            new_shares = shares + bought
            pos["net_shares"] = new_shares
            pos["avg_price"] = (avg * shares + usd) / new_shares if new_shares > 0 else 0.0
        elif side == "SELL":
            sold = min(usd / price, shares)
            pos["realized_pnl"] = float(pos["realized_pnl"]) + usd - sold * avg
            remaining = shares - sold
            pos["net_shares"] = remaining
            pos["avg_price"] = avg if remaining > 0 else 0.0
    return positions


def dashboard_stats(db: sqlite3.Connection) -> dict:
    _ensure_paper_order_exclusions(db)
    today_hkt = datetime.now(HKT).date().isoformat()
    forecast = latest_decimal_forecast_stats(db, today_hkt)
    obs = latest_observation_stats(db)
    counts = {
        "hko_forecasts": db.execute(
            """
            select count(*) from (
                select distinct forecast_date_hkt, forecast_max_c, update_time
                from hko_forecasts
                where source_type in ('ocf_station', 'flw_page')
            )
            """
        ).fetchone()[0],
        "markets": db.execute("select count(*) from markets").fetchone()[0],
        "outcomes": db.execute("select count(*) from outcomes").fetchone()[0],
        "orderbooks": db.execute("select count(*) from orderbook_snapshots").fetchone()[0],
        "buy_filled": db.execute(
            f"""
            select count(*)
            from paper_orders po
            where po.side like 'BUY_%'
              and po.status = 'filled'
              and {ACTIVE_PAPER_ORDER_FILTER}
            """
        ).fetchone()[0],
        "buy_missed": db.execute(
            "select count(*) from paper_decisions where action = 'BUY' and status = 'missed'"
        ).fetchone()[0],
        "sell_filled": db.execute(
            f"""
            select count(*)
            from paper_orders po
            where po.side = 'SELL'
              and po.status = 'filled'
              and {ACTIVE_PAPER_ORDER_FILTER}
            """
        ).fetchone()[0],
        "sell_missed": db.execute(
            "select count(*) from paper_decisions where action = 'SELL' and status = 'missed'"
        ).fetchone()[0],
    }
    positions = active_paper_positions(db)
    realized = sum(float(pos["realized_pnl"]) for pos in positions.values())
    executable_unrealized = 0.0
    worst_case_open_loss = 0.0
    open_positions = 0
    for pos in positions.values():
        shares = float(pos["net_shares"])
        if shares <= 0:
            continue
        open_positions += 1
        avg_price = float(pos["avg_price"])
        worst_case_open_loss += shares * avg_price
        bid = _latest_bid(db, str(pos["outcome_id"])) or 0.0
        executable_unrealized += shares * (bid - avg_price)
    return {
        "latest_forecast": forecast,
        "latest_observation": obs,
        "counts": counts,
        "open_positions": open_positions,
        "realized_pnl": realized,
        "executable_unrealized_pnl": executable_unrealized,
        "total_profit": realized + executable_unrealized,
        "worst_case_open_loss": worst_case_open_loss,
    }


def latest_observation_stats(db: sqlite3.Connection) -> dict | None:
    since_midnight = db.execute(
        """
        select observed_at_hkt, since_midnight_min_c, since_midnight_max_c
        from hko_current_observations
        where since_midnight_min_c is not null
           or since_midnight_max_c is not null
        order by observed_at_hkt desc, id desc
        limit 1
        """
    ).fetchone()
    current = db.execute(
        """
        select observed_at_hkt, station, temperature_c
        from hko_current_observations
        where temperature_c is not null
        order by observed_at_hkt desc, id desc
        limit 1
        """
    ).fetchone()
    if since_midnight is None and current is None:
        return None
    return {
        "observed_at_hkt": since_midnight["observed_at_hkt"] if since_midnight else current["observed_at_hkt"],
        "since_midnight_min_c": since_midnight["since_midnight_min_c"] if since_midnight else None,
        "since_midnight_max_c": since_midnight["since_midnight_max_c"] if since_midnight else None,
        "temperature_c": current["temperature_c"] if current else None,
        "temperature_observed_at_hkt": current["observed_at_hkt"] if current else None,
        "temperature_station": current["station"] if current else None,
    }


def latest_decimal_forecast_stats(
    db: sqlite3.Connection, forecast_date_hkt: str
) -> dict | None:
    row = db.execute(
        """
        select forecast_date_hkt, fetched_at_utc, raw_min_c, raw_max_c,
               forecast_min_c, forecast_max_c, hourly_temperatures_json,
               raw_daily_forecast
        from ocf_forecast_samples
        where forecast_date_hkt = ?
        order by fetched_at_utc desc, id desc
        limit 1
        """,
        (forecast_date_hkt,),
    ).fetchone()
    if row is None:
        fallback = db.execute(
            """
            select forecast_date_hkt, forecast_min_c, forecast_max_c, update_time, parse_warning
            from hko_forecasts
            where source_type = 'ocf_station'
              and forecast_date_hkt = ?
            order by id desc
            limit 1
            """,
            (forecast_date_hkt,),
        ).fetchone()
        return dict(fallback) if fallback else None

    hourly_values = _hourly_values_from_json(
        row["forecast_date_hkt"], row["hourly_temperatures_json"]
    )
    forecast_high = max(hourly_values) if hourly_values else _optional_float(row["raw_max_c"])
    forecast_low = min(hourly_values) if hourly_values else _optional_float(row["raw_min_c"])
    return {
        "forecast_date_hkt": row["forecast_date_hkt"],
        "forecast_min_c": forecast_low,
        "forecast_max_c": forecast_high,
        "display_forecast_min_c": row["forecast_min_c"],
        "display_forecast_max_c": row["forecast_max_c"],
        "update_time": _forecast_sample_update_time(row),
        "fetched_at_utc": row["fetched_at_utc"],
        "parse_warning": 0,
    }


def _hourly_values_from_json(
    target_date_hkt: str, hourly_temperatures_json: str | None
) -> list[float]:
    try:
        rows = json.loads(hourly_temperatures_json or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(rows, list):
        return []
    values = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        hour_text = str(item.get("forecast_hour_hkt") or "")
        if not hour_text.startswith(target_date_hkt):
            continue
        value = _optional_float(item.get("temperature_c"))
        if value is not None:
            values.append(value)
    return values


def _forecast_sample_update_time(row: sqlite3.Row) -> str | None:
    try:
        raw = json.loads(row["raw_daily_forecast"] or "{}")
    except json.JSONDecodeError:
        raw = {}
    if isinstance(raw, dict) and raw.get("LastModified"):
        return str(raw["LastModified"])
    return row["fetched_at_utc"]


def forecast_series(
    db: sqlite3.Connection,
    target_date_hkt: str,
    exact_raw: bool = False,
    value_kind: str = "max",
) -> list[dict]:
    sample_series = _effective_forecast_sample_series(db, target_date_hkt, value_kind)
    if sample_series:
        return sample_series

    column = "forecast_min_c" if value_kind == "min" else "forecast_max_c"
    rows = db.execute(
        f"""
        select update_time, {column} as forecast_value_c, raw_forecast
        from hko_forecasts
        where source_type = 'ocf_station'
          and forecast_date_hkt = ?
          and {column} is not null
          and coalesce(parse_warning, 0) = 0
          and update_time is not null
        order by update_time asc
        """,
        (target_date_hkt,),
    ).fetchall()
    seen: dict[int, float] = {}
    series: list[dict] = []
    for row in rows:
        ts = _to_unix(row["update_time"])
        if ts is None:
            continue
        value = float(row["forecast_value_c"])
        if exact_raw:
            raw_value = _raw_forecast_value(row["raw_forecast"], value_kind)
            value = raw_value or value
        seen[ts] = value
    return [{"time": ts, "value": value} for ts, value in sorted(seen.items())]


def _effective_forecast_sample_series(
    db: sqlite3.Connection, target_date_hkt: str, value_kind: str
) -> list[dict]:
    rows = db.execute(
        """
        select forecast_date_hkt, fetched_at_utc, raw_min_c, raw_max_c,
               hourly_temperatures_json, raw_daily_forecast
        from ocf_forecast_samples
        where forecast_date_hkt = ?
        order by fetched_at_utc asc, id asc
        """,
        (target_date_hkt,),
    ).fetchall()
    seen: dict[int, float] = {}
    for row in rows:
        update_time = _forecast_sample_update_time(row)
        ts = _to_unix(update_time)
        if ts is None:
            continue
        hourly_values = _hourly_values_from_json(
            row["forecast_date_hkt"], row["hourly_temperatures_json"]
        )
        if hourly_values:
            value = min(hourly_values) if value_kind == "min" else max(hourly_values)
        else:
            raw_column = "raw_min_c" if value_kind == "min" else "raw_max_c"
            value = _optional_float(row[raw_column])
        if value is not None:
            seen[ts] = value
    return [{"time": ts, "value": value} for ts, value in sorted(seen.items())]


def _raw_forecast_value(raw_forecast: str | None, value_kind: str = "max") -> float | None:
    if not raw_forecast:
        return None
    try:
        raw = json.loads(raw_forecast)
        key = "ForecastMinimumTemperature" if value_kind == "min" else "ForecastMaximumTemperature"
        value = raw.get(key)
        return None if value is None else float(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def observation_series(db: sqlite3.Connection, target_date_hkt: str) -> tuple[list[dict], list[dict], list[dict]]:
    rows = db.execute(
        """
        select observed_at_hkt, since_midnight_min_c, since_midnight_max_c, temperature_c
        from hko_current_observations
        where substr(observed_at_hkt, 1, 10) = ?
          and (since_midnight_min_c is not null or since_midnight_max_c is not null or temperature_c is not null)
        order by observed_at_hkt asc, id asc
        """,
        (target_date_hkt,),
    ).fetchall()
    min_seen: dict[int, float] = {}
    max_seen: dict[int, float] = {}
    cur_seen: dict[int, float] = {}
    for row in rows:
        ts = _to_unix(row["observed_at_hkt"])
        if ts is None:
            continue
        if row["since_midnight_min_c"] is not None:
            value = float(row["since_midnight_min_c"])
            if ts not in min_seen or value < min_seen[ts]:
                min_seen[ts] = value
        if row["since_midnight_max_c"] is not None:
            value = float(row["since_midnight_max_c"])
            if ts not in max_seen or value > max_seen[ts]:
                max_seen[ts] = value
        if row["temperature_c"] is not None:
            cur_seen[ts] = float(row["temperature_c"])
    actual_min = [{"time": t, "value": v} for t, v in sorted(min_seen.items())]
    actual_max = [{"time": t, "value": v} for t, v in sorted(max_seen.items())]
    current = [{"time": t, "value": v} for t, v in sorted(cur_seen.items())]
    return actual_min, actual_max, current


def hourly_forecast_series(db: sqlite3.Connection, target_date_hkt: str) -> list[dict]:
    row = db.execute(
        """
        select hourly_temperatures_json
        from ocf_forecast_samples
        where forecast_date_hkt = ?
          and hourly_temperatures_json is not null
          and hourly_temperatures_json != '[]'
        order by fetched_at_utc desc, id desc
        limit 1
        """,
        (target_date_hkt,),
    ).fetchone()
    if row is None:
        return []
    try:
        hourly = json.loads(row["hourly_temperatures_json"] or "[]")
    except json.JSONDecodeError:
        return []
    points: dict[int, float] = {}
    for item in hourly:
        ts = _to_unix(item.get("forecast_hour_hkt"))
        value = _optional_float(item.get("temperature_c"))
        if ts is not None and value is not None:
            points[ts] = value
    return [{"time": ts, "value": value} for ts, value in sorted(points.items())]


def hourly_actual_series(db: sqlite3.Connection, target_date_hkt: str) -> list[dict]:
    rows = db.execute(
        """
        select observed_at_hkt, temperature_c
        from hko_current_observations
        where substr(observed_at_hkt, 1, 10) = ?
          and temperature_c is not null
        order by observed_at_hkt asc, id asc
        """,
        (target_date_hkt,),
    ).fetchall()
    hourly_current: dict[int, float] = {}
    for row in rows:
        try:
            observed = datetime.fromisoformat(row["observed_at_hkt"]).astimezone(HKT)
        except (TypeError, ValueError):
            continue
        hour = observed.replace(minute=0, second=0, microsecond=0)
        ts = int(hour.timestamp())
        if row["temperature_c"] is not None:
            hourly_current[ts] = float(row["temperature_c"])
    return [{"time": ts, "value": value} for ts, value in sorted(hourly_current.items())]


def hourly_error_series(
    hourly_forecast: list[dict], hourly_actual: list[dict]
) -> list[dict]:
    forecast_by_hour = {point["time"]: point["value"] for point in hourly_forecast}
    return [
        {"time": point["time"], "value": point["value"] - forecast_by_hour[point["time"]]}
        for point in hourly_actual
        if point["time"] in forecast_by_hour
    ]


def _optional_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def top_token_price_series(
    db: sqlite3.Connection,
    target_date_hkt: str,
    side: str = "YES",
    limit: int | None = 3,
    sort_by_latest_price: bool = False,
    bucket_seconds: int = 60,
    include_trade_tokens: bool = False,
    market_kind: str = "highest",
) -> list[dict]:
    side = side.upper()
    if side not in {"YES", "NO"}:
        side = "YES"
    token_col = "yes_token_id" if side == "YES" else "no_token_id"
    limit_clause = "" if limit is None else "limit ?"
    order_clause = (
        "s.best_ask desc, o.predicate_value_c asc, o.label asc"
        if sort_by_latest_price
        else "o.predicate_value_c asc, o.label asc"
    )
    slug_prefix = (
        "lowest-temperature-in-hong-kong-on-"
        if market_kind == "lowest"
        else "highest-temperature-in-hong-kong-on-"
    )
    params = (
        (target_date_hkt, f"{slug_prefix}%")
        if limit is None
        else (target_date_hkt, f"{slug_prefix}%", limit)
    )
    rows = db.execute(
        f"""
        with latest as (
            select outcome_id, max(id) as latest_id
            from orderbook_snapshots
            where best_ask is not null
            group by outcome_id
        )
        select o.label, o.{token_col} as token_id, s.best_ask
        from outcomes o
        join markets m on m.id = o.market_id
        join latest l on l.outcome_id = o.{token_col}
        join orderbook_snapshots s on s.id = l.latest_id
        where m.target_date_hkt = ?
          and m.slug like ?
        order by {order_clause}
        {limit_clause}
        """,
        params,
    ).fetchall()

    row_records = []
    for row in rows:
        markers = paper_order_markers(db, row["token_id"])
        row_records.append(
            {
                "row": row,
                "latest_price": float(row["best_ask"]),
                "markers": markers,
                "has_trades": bool(markers),
            }
        )
    if include_trade_tokens:
        eligible = [
            record
            for record in row_records
            if 0.01 < record["latest_price"] < 0.99
        ]
        traded = [record for record in row_records if record["has_trades"]]
        traded_tokens = {record["row"]["token_id"] for record in traded}
        selected = list(traded)
        remaining_slots = max(0, 5 - len(selected))
        selected.extend(
            sorted(
                [
                    record
                    for record in eligible
                    if record["row"]["token_id"] not in traded_tokens
                ],
                key=lambda record: (
                    -record["latest_price"],
                    record["row"]["label"],
                ),
            )[:remaining_slots]
        )
        row_records = sorted(
            selected,
            key=lambda record: (
                row_sort_value(record["row"]["label"]),
                record["row"]["label"],
            ),
        )

    series = []
    for record in row_records:
        row = record["row"]
        points = []
        snapshots = db.execute(
            """
            select fetched_at_utc, best_ask
            from orderbook_snapshots
            where outcome_id = ?
              and best_ask is not null
            order by fetched_at_utc asc, id asc
            """,
            (row["token_id"],),
        ).fetchall()
        seen: dict[int, float] = {}
        for snapshot in snapshots:
            ts = _to_unix(snapshot["fetched_at_utc"])
            if ts is not None:
                bucket = ts - (ts % bucket_seconds) if bucket_seconds > 0 else ts
                seen[bucket] = float(snapshot["best_ask"])
        points = [{"time": ts, "value": value} for ts, value in sorted(seen.items())]
        series.append(
            {
                "label": row["label"],
                "side": side,
                "token_id": row["token_id"],
                "latest_price": record["latest_price"],
                "latest_yes": record["latest_price"] if side == "YES" else None,
                "points": points,
                "markers": record["markers"],
            }
        )
    return series


def row_sort_value(label: str) -> float:
    for part in label.replace("°C", "").split():
        try:
            return float(part)
        except ValueError:
            continue
    return 9999.0


def top_yes_price_series(
    db: sqlite3.Connection, target_date_hkt: str, limit: int = 3
) -> list[dict]:
    return top_token_price_series(
        db, target_date_hkt, "YES", limit, sort_by_latest_price=True
    )


def paper_order_markers(db: sqlite3.Connection, token_id: str) -> list[dict]:
    _ensure_paper_order_exclusions(db)
    rows = db.execute(
        f"""
        select po.created_at_utc, po.side, po.simulated_fill_price,
               po.simulated_fill_size_usd, po.status
        from paper_orders po
        where po.outcome_id = ?
          and po.status = 'filled'
          and po.simulated_fill_price is not null
          and po.created_at_utc is not null
          and {ACTIVE_PAPER_ORDER_FILTER}
        order by created_at_utc asc, id asc
        """,
        (token_id,),
    ).fetchall()
    markers = []
    for row in rows:
        ts = _to_unix(row["created_at_utc"])
        if ts is None:
            continue
        is_sell = row["side"] == "SELL"
        fill_price = float(row["simulated_fill_price"])
        fill_size = (
            None
            if row["simulated_fill_size_usd"] is None
            else float(row["simulated_fill_size_usd"])
        )
        decision = _nearest_paper_decision_for_order(db, token_id, row)
        markers.append(
            {
                "time": ts,
                "position": "aboveBar" if is_sell else "belowBar",
                "color": "#ef5350" if is_sell else "#26a69a",
                "shape": "circle",
                "text": "S" if is_sell else "B",
                "price": fill_price,
                "size_usd": fill_size,
                "trade_side": row["side"],
                "decision_time_hkt": (
                    _to_hkt_display(decision["created_at_utc"]) if decision else None
                ),
                "decision_reason": decision["reason"] if decision else None,
                "signal": _decision_signal_summary(decision) if decision else None,
                "signal_time_hkt": _decision_signal_time_hkt(decision) if decision else None,
            }
        )
    return markers


def _nearest_paper_decision_for_order(
    db: sqlite3.Connection, token_id: str, order: sqlite3.Row
) -> sqlite3.Row | None:
    action = "SELL" if order["side"] == "SELL" else "BUY"
    token_side = _token_side_for_order(db, token_id, order["side"])
    if token_side is None:
        return None
    row = db.execute(
        """
        select created_at_utc, event_type, reason, details_json, event_key,
               abs((julianday(created_at_utc) - julianday(?)) * 86400.0) as delta_seconds
        from paper_decisions
        where outcome_id = ?
          and action = ?
          and status = 'filled'
          and side = ?
          and created_at_utc is not null
        order by delta_seconds asc, id desc
        limit 1
        """,
        (order["created_at_utc"], token_id, action, token_side),
    ).fetchone()
    if row is None or float(row["delta_seconds"]) > 10:
        return None
    return row


def _token_side_for_order(
    db: sqlite3.Connection, token_id: str, order_side: str | None
) -> str | None:
    if order_side == "BUY_YES":
        return "YES"
    if order_side == "BUY_NO":
        return "NO"
    row = db.execute(
        """
        select yes_token_id, no_token_id
        from outcomes
        where yes_token_id = ? or no_token_id = ?
        limit 1
        """,
        (token_id, token_id),
    ).fetchone()
    if row is None:
        return None
    if token_id == row["yes_token_id"]:
        return "YES"
    if token_id == row["no_token_id"]:
        return "NO"
    return None


def _decision_details(decision: sqlite3.Row | None) -> dict:
    if decision is None:
        return {}
    try:
        parsed = json.loads(decision["details_json"] or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _decision_signal_summary(decision: sqlite3.Row | None) -> str | None:
    if decision is None:
        return None
    details = _decision_details(decision)
    if details.get("forecast_highest") is not None:
        return f"signal high {_compact_temp(details['forecast_highest'])}"
    if details.get("forecast_lowest") is not None:
        return f"signal low {_compact_temp(details['forecast_lowest'])}"
    if details.get("forecast_max") is not None:
        return f"signal high {_compact_temp(details['forecast_max'])}"
    if details.get("forecast_min") is not None:
        return f"signal low {_compact_temp(details['forecast_min'])}"
    if details.get("current_value") is not None:
        return f"actual signal {_compact_temp(details['current_value'])}"
    event_value = _event_key_new_value(decision["event_key"])
    if event_value is not None:
        label = "signal"
        if str(decision["event_type"] or "").startswith("lowest"):
            label = "signal low"
        elif "forecast" in str(decision["event_type"] or ""):
            label = "signal high"
        return f"{label} {_compact_temp(event_value)}"
    return None


def _decision_signal_time_hkt(decision: sqlite3.Row | None) -> str | None:
    if decision is None:
        return None
    timestamp = _event_key_new_timestamp(decision["event_key"])
    if timestamp is None:
        return _to_hkt_display(decision["created_at_utc"])
    return timestamp


def _event_key_new_value(event_key: str | None) -> float | None:
    if not event_key or "->" not in event_key:
        return None
    right = event_key.split("->", 1)[1]
    value_text = right.rsplit(":", 1)[-1]
    return _optional_float(value_text)


def _event_key_new_timestamp(event_key: str | None) -> str | None:
    if not event_key or "->" not in event_key:
        return None
    right = event_key.split("->", 1)[1]
    if ":" not in right:
        return None
    timestamp_text = right.split(":", 1)[0]
    if len(timestamp_text) != 14 or not timestamp_text.isdigit():
        return None
    return (
        f"{timestamp_text[0:4]}-{timestamp_text[4:6]}-{timestamp_text[6:8]} "
        f"{timestamp_text[8:10]}:{timestamp_text[10:12]}:{timestamp_text[12:14]}"
    )


def forecast_panel(
    db: sqlite3.Connection, target_date: date, lead_days: int, token_side: str = "YES"
) -> dict:
    target_text = target_date.isoformat()
    actual_min, actual_max, current = observation_series(db, target_text)
    hourly_forecast = hourly_forecast_series(db, target_text) if lead_days == 0 else []
    hourly_actual = hourly_actual_series(db, target_text) if lead_days == 0 else []
    top_tokens = top_token_price_series(
        db, target_text, token_side, limit=None, include_trade_tokens=True
    )
    low_tokens = top_token_price_series(
        db,
        target_text,
        token_side,
        limit=None,
        include_trade_tokens=True,
        market_kind="lowest",
    )
    return {
        "lead_days": lead_days,
        "target_date": target_text,
        "forecast": forecast_series(db, target_text, exact_raw=lead_days == 0),
        "forecast_low": forecast_series(
            db, target_text, exact_raw=lead_days == 0, value_kind="min"
        ),
        "actual_min": actual_min if lead_days == 0 else [],
        "actual_max": actual_max if lead_days == 0 else [],
        "current_temp": current if lead_days == 0 else [],
        "hourly_forecast": hourly_forecast,
        "hourly_actual": hourly_actual,
        "hourly_error": hourly_error_series(hourly_forecast, hourly_actual),
        "token_side": token_side.upper() if token_side.upper() in {"YES", "NO"} else "YES",
        "top_tokens": top_tokens,
        "low_tokens": low_tokens,
        "top_yes": top_tokens if token_side.upper() == "YES" else [],
    }


def forecast_panels(
    db: sqlite3.Connection, today: date | None = None, token_side: str = "YES"
) -> dict:
    base = today or datetime.now(HKT).date()
    return {
        "panels": [
            forecast_panel(db, base + timedelta(days=lead), lead, token_side)
            for lead in (0, 1, 2)
        ],
        "available_dates": available_forecast_dates(db),
        "token_side": token_side.upper() if token_side.upper() in {"YES", "NO"} else "YES",
    }


def available_forecast_dates(db: sqlite3.Connection) -> list[str]:
    rows = db.execute(
        """
        select distinct forecast_date_hkt
        from hko_forecasts
        where source_type = 'ocf_station'
          and forecast_date_hkt is not null
          and forecast_max_c is not null
          and coalesce(parse_warning, 0) = 0
        order by forecast_date_hkt asc
        """
    ).fetchall()
    return [r["forecast_date_hkt"] for r in rows]


def pnl_series(db: sqlite3.Connection, bucket_seconds: int = 60) -> dict:
    """Replay paper_orders and orderbook snapshots into realized/unrealized series."""

    _ensure_paper_order_exclusions(db)
    orders = db.execute(
        f"""
        select po.created_at_utc, po.outcome_id, po.side, po.simulated_fill_price,
               po.simulated_fill_size_usd, po.status
        from paper_orders po
        where po.status = 'filled'
          and po.simulated_fill_price is not null
          and po.simulated_fill_size_usd is not null
          and po.created_at_utc is not null
          and {ACTIVE_PAPER_ORDER_FILTER}
        order by created_at_utc asc, id asc
        """
    ).fetchall()
    if not orders:
        return {"realized": [], "unrealized": [], "total": []}

    first_order_ts = _to_unix(orders[0]["created_at_utc"])
    if first_order_ts is None:
        return {"realized": [], "unrealized": [], "total": []}

    held_outcomes = {row["outcome_id"] for row in orders}
    placeholders = ",".join("?" for _ in held_outcomes)
    snapshots = db.execute(
        f"""
        select fetched_at_utc, outcome_id, best_bid
        from orderbook_snapshots
        where best_bid is not null
          and outcome_id in ({placeholders})
        order by fetched_at_utc asc, id asc
        """,
        tuple(held_outcomes),
    ).fetchall()

    events: list[tuple[int, str, sqlite3.Row]] = []
    for row in orders:
        ts = _to_unix(row["created_at_utc"])
        if ts is None:
            continue
        events.append((ts, "order", row))
    for row in snapshots:
        ts = _to_unix(row["fetched_at_utc"])
        if ts is None or ts < first_order_ts:
            continue
        events.append((ts, "book", row))
    events.sort(key=lambda e: (e[0], 0 if e[1] == "order" else 1))

    positions: dict[str, tuple[float, float]] = {}
    bids: dict[str, float] = {}
    realized = 0.0
    realized_buckets: dict[int, float] = {}
    unrealized_buckets: dict[int, float] = {}

    def bucket_key(ts: int) -> int:
        return ts - (ts % bucket_seconds)

    def current_unrealized() -> float:
        total = 0.0
        for token, (shares, avg) in positions.items():
            if shares <= 0:
                continue
            bid = bids.get(token)
            if bid is None:
                continue
            total += shares * (bid - avg)
        return total

    for ts, kind, row in events:
        if kind == "order":
            token = row["outcome_id"]
            side = row["side"] or ""
            price = float(row["simulated_fill_price"])
            usd = float(row["simulated_fill_size_usd"])
            shares, avg = positions.get(token, (0.0, 0.0))
            if side.startswith("BUY") and price > 0:
                bought_shares = usd / price
                new_shares = shares + bought_shares
                new_avg = (
                    (avg * shares + usd) / new_shares if new_shares > 0 else 0.0
                )
                positions[token] = (new_shares, new_avg)
            elif side == "SELL":
                sold_shares = usd / price if price > 0 else shares
                sold_shares = min(sold_shares, shares)
                realized += usd - sold_shares * avg
                remaining = shares - sold_shares
                positions[token] = (remaining, avg if remaining > 0 else 0.0)
        elif kind == "book":
            bids[row["outcome_id"]] = float(row["best_bid"])
        b = bucket_key(ts)
        realized_buckets[b] = realized
        unrealized_buckets[b] = current_unrealized()

    realized_series = [{"time": t, "value": v} for t, v in sorted(realized_buckets.items())]
    unrealized_series = [
        {"time": t, "value": v} for t, v in sorted(unrealized_buckets.items())
    ]
    total_series = [
        {"time": t, "value": realized_buckets[t] + unrealized_buckets[t]}
        for t in sorted(realized_buckets.keys())
    ]
    return {
        "realized": realized_series,
        "unrealized": unrealized_series,
        "total": total_series,
    }


def paper_trade_rows(db: sqlite3.Connection, view: str) -> dict:
    _ensure_paper_order_exclusions(db)
    view = view if view in {"open", "realized", "unrealized"} else "open"
    active_positions = active_paper_positions(db)
    if view in {"open", "unrealized"}:
        tokens = [
            str(pos["outcome_id"])
            for pos in active_positions.values()
            if float(pos["net_shares"]) > 0
        ]
    else:
        token_rows = db.execute(
            f"""
            select distinct po.outcome_id
            from paper_orders po
            where po.side = 'SELL'
              and po.status = 'filled'
              and {ACTIVE_PAPER_ORDER_FILTER}
            """
        ).fetchall()
        tokens = [row["outcome_id"] for row in token_rows]
    titles = {
        "open": "Open Position Trades",
        "realized": "Realized PnL Trades",
        "unrealized": "Unrealized PnL Trades",
    }
    if not tokens:
        return {"view": view, "title": titles[view], "rows": []}

    placeholders = ",".join("?" for _ in tokens)
    rows = db.execute(
        f"""
        select po.id, po.created_at_utc, po.outcome_id, po.side,
               po.limit_price, po.size_usd, po.simulated_fill_price,
               po.simulated_fill_size_usd, po.status, po.reason,
               o.label, m.target_date_hkt,
               case
                   when po.outcome_id = o.yes_token_id then 'YES'
                   when po.outcome_id = o.no_token_id then 'NO'
                   else null
               end as token_side,
               p.net_shares, p.avg_price, p.realized_pnl
        from paper_orders po
        left join outcomes o
          on po.outcome_id = o.yes_token_id or po.outcome_id = o.no_token_id
        left join markets m on m.id = o.market_id
        left join paper_positions p on p.outcome_id = po.outcome_id
        where po.outcome_id in ({placeholders})
          and po.status = 'filled'
          and (? != 'realized' or po.side = 'SELL')
          and {ACTIVE_PAPER_ORDER_FILTER}
        order by po.created_at_utc desc, po.id desc
        """,
        tuple(tokens) + (view,),
    ).fetchall()
    realized_by_order_id = _realized_pnl_by_sell_order_id(db, tokens)

    result = []
    for row in rows:
        fill_price = _optional_float(row["simulated_fill_price"])
        fill_size = _optional_float(row["simulated_fill_size_usd"]) or 0.0
        shares = fill_size / fill_price if fill_price and fill_price > 0 else None
        latest_bid = _latest_bid(db, row["outcome_id"])
        active_pos = active_positions.get(row["outcome_id"], {})
        net_shares = _optional_float(active_pos.get("net_shares")) or 0.0
        avg_price = _optional_float(active_pos.get("avg_price")) or 0.0
        unrealized = (
            shares * (latest_bid - fill_price)
            if row["side"] != "SELL"
            and shares is not None
            and fill_price is not None
            and latest_bid is not None
            and net_shares > 1e-8
            else 0.0
        )
        realized_pnl = (
            realized_by_order_id.get(int(row["id"]), 0.0)
            if row["side"] == "SELL"
            else _optional_float(active_pos.get("realized_pnl")) or 0.0
        )
        result.append(
            {
                "id": row["id"],
                "created_at_utc": row["created_at_utc"],
                "created_at_hkt": _to_hkt_display(row["created_at_utc"]),
                "target_date_hkt": row["target_date_hkt"],
                "label": row["label"] or row["outcome_id"],
                "token_side": row["token_side"],
                "action": row["side"],
                "outcome_id": row["outcome_id"],
                "limit_price": row["limit_price"],
                "fill_price": fill_price,
                "fill_size_usd": fill_size,
                "shares": shares,
                "net_shares": net_shares,
                "avg_price": avg_price,
                "latest_bid": latest_bid,
                "realized_pnl": realized_pnl,
                "unrealized_pnl": unrealized,
                "reason": row["reason"],
            }
        )
    return {"view": view, "title": titles[view], "rows": result}


def _realized_pnl_by_sell_order_id(
    db: sqlite3.Connection, tokens: list[str]
) -> dict[int, float]:
    if not tokens:
        return {}
    _ensure_paper_order_exclusions(db)
    placeholders = ",".join("?" for _ in tokens)
    orders = db.execute(
        f"""
        select po.id, po.outcome_id, po.side, po.simulated_fill_price,
               po.simulated_fill_size_usd
        from paper_orders po
        where po.outcome_id in ({placeholders})
          and po.status = 'filled'
          and po.simulated_fill_price is not null
          and po.simulated_fill_size_usd is not null
          and {ACTIVE_PAPER_ORDER_FILTER}
        order by po.created_at_utc asc, po.id asc
        """,
        tuple(tokens),
    ).fetchall()
    positions: dict[str, tuple[float, float]] = {}
    realized_by_order_id: dict[int, float] = {}
    for row in orders:
        token = row["outcome_id"]
        side = row["side"] or ""
        price = _optional_float(row["simulated_fill_price"])
        usd = _optional_float(row["simulated_fill_size_usd"])
        if price is None or price <= 0 or usd is None:
            continue
        shares, avg = positions.get(token, (0.0, 0.0))
        if side.startswith("BUY"):
            bought_shares = usd / price
            new_shares = shares + bought_shares
            new_avg = (
                (avg * shares + usd) / new_shares if new_shares > 0 else 0.0
            )
            positions[token] = (new_shares, new_avg)
        elif side == "SELL":
            sold_shares = min(usd / price, shares)
            realized = usd - sold_shares * avg
            realized_by_order_id[int(row["id"])] = realized
            remaining = shares - sold_shares
            positions[token] = (remaining, avg if remaining > 0 else 0.0)
    return realized_by_order_id


def _latest_bid(db: sqlite3.Connection, token_id: str) -> float | None:
    row = db.execute(
        """
        select best_bid
        from orderbook_snapshots
        where outcome_id = ? and best_bid is not null
        order by fetched_at_utc desc, id desc
        limit 1
        """,
        (token_id,),
    ).fetchone()
    return None if row is None else float(row["best_bid"])


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>whenitrains paper dashboard</title>
<meta name="viewport" content="width=device-width,initial-scale=1" />
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
  :root {
    --bg: #0e1116;
    --panel: #161b22;
    --border: #30363d;
    --text: #e6edf3;
    --muted: #7d8590;
    --accent: #f0b400;
    --pos: #26a69a;
    --neg: #ef5350;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font: 13px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  .banner {
    background: repeating-linear-gradient(
      45deg, #4a3700, #4a3700 12px, #5a4500 12px, #5a4500 24px
    );
    color: var(--accent);
    padding: 8px 16px;
    font-weight: 700;
    letter-spacing: 0.5px;
    text-align: center;
    border-bottom: 1px solid var(--accent);
    text-transform: uppercase;
    font-size: 12px;
  }
  header {
    padding: 10px 16px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 16px;
  }
  header h1 {
    margin: 0;
    font-size: 14px;
    font-weight: 600;
  }
  header .meta {
    color: var(--muted);
    font-size: 12px;
  }
  .stats {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 1px;
    background: var(--border);
    border-bottom: 1px solid var(--border);
  }
  .stat {
    background: var(--panel);
    padding: 10px 14px;
  }
  .stat.clickable {
    cursor: pointer;
  }
  .stat.clickable:hover {
    background: #1c2330;
  }
  .stat .label {
    color: var(--muted);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .stat .value {
    font-size: 18px;
    font-weight: 600;
    margin-top: 2px;
    font-variant-numeric: tabular-nums;
  }
  .stat .value.pos { color: var(--pos); }
  .stat .value.neg { color: var(--neg); }
  .controls {
    padding: 10px 16px;
    display: flex;
    gap: 12px;
    align-items: center;
    border-bottom: 1px solid var(--border);
    background: var(--panel);
  }
  .controls label { color: var(--muted); font-size: 12px; }
  .controls select, .controls button {
    background: #0d1117;
    color: var(--text);
    border: 1px solid var(--border);
    padding: 4px 10px;
    border-radius: 4px;
    font: inherit;
    cursor: pointer;
  }
  .chart-section {
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
  }
  .chart-section h2 {
    margin: 0 0 8px;
    font-size: 12px;
    text-transform: uppercase;
    color: var(--muted);
    letter-spacing: 0.5px;
    font-weight: 600;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .chart-section h2 .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 14px;
    text-transform: none;
    letter-spacing: 0;
  }
  .chart-section h2 .legend span {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    color: var(--text);
    font-weight: 500;
  }
  .chart-section h2 .legend [data-series-key] {
    border: 0;
    background: transparent;
    color: var(--text);
    font: inherit;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 0;
    cursor: pointer;
  }
  .chart-section h2 .legend [data-series-key].off {
    color: var(--muted);
    opacity: 0.45;
  }
  .chart-section h2 .legend i {
    width: 10px; height: 10px; border-radius: 2px; display: inline-block;
  }
  .chart {
    position: relative;
    width: 100%;
    height: 380px;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 4px;
    overflow: hidden;
  }
  .drilldown {
    display: none;
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
  }
  .drilldown.active {
    display: block;
  }
  .drilldown-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 10px;
  }
  .drilldown button {
    background: #0d1117;
    color: var(--text);
    border: 1px solid var(--border);
    padding: 4px 10px;
    border-radius: 4px;
    font: inherit;
    cursor: pointer;
  }
  .drilldown h2 {
    margin: 0;
    font-size: 12px;
    text-transform: uppercase;
    color: var(--muted);
    letter-spacing: 0.5px;
  }
  .drilldown table {
    width: 100%;
    border-collapse: collapse;
    background: var(--panel);
    border: 1px solid var(--border);
    font-variant-numeric: tabular-nums;
  }
  .drilldown th, .drilldown td {
    padding: 7px 9px;
    border-bottom: 1px solid var(--border);
    text-align: left;
    white-space: nowrap;
  }
  .drilldown th {
    color: var(--muted);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.4px;
  }
  .drilldown .table-wrap {
    overflow-x: auto;
  }
  .drilldown .empty {
    color: var(--muted);
    background: var(--panel);
    border: 1px solid var(--border);
    padding: 14px;
  }
  .trade-bubble {
    position: absolute;
    z-index: 10;
    width: 20px;
    height: 20px;
    border-radius: 999px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #0d1117;
    font-size: 11px;
    font-weight: 800;
    line-height: 1;
    transform: translate(-50%, -50%);
    pointer-events: auto;
    box-shadow: 0 0 0 2px #0d1117, 0 3px 10px rgba(0, 0, 0, 0.45);
  }
  .trade-bubble.buy { background: #26a69a; }
  .trade-bubble.sell { background: #ef5350; color: #ffffff; }
  .empty-overlay {
    color: var(--muted);
    font-size: 13px;
    padding: 14px;
    text-align: center;
  }
  .lead-label {
    color: var(--text);
    font-weight: 700;
    margin-right: 8px;
  }
  .chart-tooltip {
    position: fixed;
    z-index: 20;
    min-width: 180px;
    max-width: 320px;
    padding: 8px 10px;
    border: 1px solid var(--border);
    border-radius: 4px;
    background: rgba(13, 17, 23, 0.96);
    color: var(--text);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
    pointer-events: none;
    display: none;
    font-size: 12px;
  }
  .chart-tooltip .time {
    color: var(--muted);
    margin-bottom: 5px;
    font-variant-numeric: tabular-nums;
  }
  .chart-tooltip .row {
    display: flex;
    justify-content: space-between;
    gap: 14px;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .chart-tooltip .name {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .chart-tooltip i {
    width: 8px;
    height: 8px;
    border-radius: 2px;
    display: inline-block;
    flex: 0 0 auto;
  }
</style>
</head>
<body>
  <div class="banner">⚠ Paper Trading Mode — simulated fills only, no real orders sent</div>
  <header>
    <h1>whenitrains · HK temperature paper desk</h1>
    <span class="meta" id="last-update">loading…</span>
  </header>

  <div class="stats" id="stats"></div>

  <div class="controls">
    <button id="refresh-btn">Refresh now</button>
    <label for="token-side">Polymarket side</label>
    <select id="token-side">
      <option value="YES">YES tokens</option>
      <option value="NO">NO tokens</option>
    </select>
    <span class="meta" id="autorefresh">auto-refresh every 15s</span>
  </div>

  <section class="drilldown" id="trade-drilldown">
    <div class="drilldown-head">
      <h2 id="trade-drilldown-title">Trades</h2>
      <button id="close-drilldown">Back to charts</button>
    </div>
    <div id="trade-drilldown-body"></div>
  </section>

  <section class="chart-section">
    <h2>
      <span><span class="lead-label">D+0 High</span><span id="d0-date"></span></span>
      <span class="legend">
        <button type="button" data-series-key="forecastHigh"><i style="background:#f0b400"></i>Bot signal high</button>
        <button type="button" data-series-key="hourlyForecast"><i style="background:#c084fc"></i>Latest hourly forecast</button>
        <button type="button" data-series-key="hourlyActual"><i style="background:#f97316"></i>Hourly actual</button>
        <button type="button" data-series-key="actualMax"><i style="background:#26a69a"></i>Since-midnight max</button>
        <button type="button" data-series-key="currentTemp"><i style="background:#5b9bd5"></i>Current temperature</button>
        <span id="d0-legend"></span>
      </span>
    </h2>
    <div id="d0-chart" class="chart"></div>
  </section>

  <section class="chart-section">
    <h2>
      <span><span class="lead-label">D+0 Low</span><span id="l0-date"></span></span>
      <span class="legend">
        <button type="button" data-series-key="forecastLow"><i style="background:#38bdf8"></i>Bot signal low</button>
        <button type="button" data-series-key="lowHourlyForecast"><i style="background:#c084fc"></i>Latest hourly forecast</button>
        <button type="button" data-series-key="lowHourlyActual"><i style="background:#f97316"></i>Hourly actual</button>
        <button type="button" data-series-key="actualMin"><i style="background:#2dd4bf"></i>Since-midnight min</button>
        <button type="button" data-series-key="lowCurrentTemp"><i style="background:#5b9bd5"></i>Current temperature</button>
        <span id="l0-legend"></span>
      </span>
    </h2>
    <div id="l0-chart" class="chart"></div>
  </section>

  <div id="chart-tooltip" class="chart-tooltip"></div>

  <section class="chart-section">
    <h2>
      <span><span class="lead-label">D+1 High</span><span id="d1-date"></span></span>
      <span class="legend" id="d1-legend"></span>
    </h2>
    <div id="d1-chart" class="chart"></div>
  </section>

  <section class="chart-section">
    <h2>
      <span><span class="lead-label">D+1 Low</span><span id="l1-date"></span></span>
      <span class="legend" id="l1-legend"></span>
    </h2>
    <div id="l1-chart" class="chart"></div>
  </section>

  <section class="chart-section">
    <h2>
      <span><span class="lead-label">D+2 High</span><span id="d2-date"></span></span>
      <span class="legend" id="d2-legend"></span>
    </h2>
    <div id="d2-chart" class="chart"></div>
  </section>

  <section class="chart-section">
    <h2>
      <span><span class="lead-label">D+2 Low</span><span id="l2-date"></span></span>
      <span class="legend" id="l2-legend"></span>
    </h2>
    <div id="l2-chart" class="chart"></div>
  </section>

  <section class="chart-section">
    <h2>
      Paper PnL ($)
      <span class="legend">
        <span><i style="background:#26a69a"></i>Realized</span>
        <span><i style="background:#5b9bd5"></i>Unrealized</span>
        <span><i style="background:#f0b400"></i>Total</span>
      </span>
    </h2>
    <div id="pnl-chart" class="chart"></div>
  </section>

<script>
const HKT_OFFSET_SEC = 8 * 3600;
const chartTimeToUnixSeconds = (time) => {
  if (typeof time === "number") return time;
  if (typeof time === "string") return Math.floor(Date.parse(time + "T00:00:00Z") / 1000);
  if (time && typeof time === "object") {
    return Math.floor(Date.UTC(time.year, time.month - 1, time.day) / 1000);
  }
  return null;
};
const fmtHKT = (sec) => {
  const unixSec = chartTimeToUnixSeconds(sec);
  if (unixSec == null || Number.isNaN(unixSec)) return "";
  const d = new Date((unixSec + HKT_OFFSET_SEC) * 1000);
  const iso = d.toISOString();
  return iso.slice(0, 10) + " " + iso.slice(11, 16) + " HKT";
};
const fmtHKTTime = (time) => fmtHKT(time).slice(11, 16);
const fmtHKTUpdate = (value) => {
  const text = String(value || "");
  if (/^\d{14}$/.test(text)) {
    return `${text.slice(0,4)}-${text.slice(4,6)}-${text.slice(6,8)} ${text.slice(8,10)}:${text.slice(10,12)}:${text.slice(12,14)} HKT`;
  }
  if (text.length >= 16) return text.slice(0, 16).replace("T", " ") + " HKT";
  return "n/a";
};

function makeChart(elementId, dualAxis=false) {
  const chart = LightweightCharts.createChart(document.getElementById(elementId), {
  layout: { background: { color: "#161b22" }, textColor: "#e6edf3" },
  grid: {
    vertLines: { color: "#21262d" },
    horzLines: { color: "#21262d" },
  },
  timeScale: {
    timeVisible: true,
    secondsVisible: false,
    borderColor: "#30363d",
    tickMarkFormatter: fmtHKTTime,
  },
  rightPriceScale: { borderColor: "#30363d", visible: true },
  leftPriceScale: { borderColor: "#30363d", visible: dualAxis },
  crosshair: {
    mode: LightweightCharts.CrosshairMode.Normal,
    horzLine: { visible: false, labelVisible: false },
    vertLine: { color: "#7d8590", style: LightweightCharts.LineStyle.Dotted },
  },
  localization: { timeFormatter: fmtHKT },
  handleScroll: {
    mouseWheel: false,
    pressedMouseMove: true,
    horzTouchDrag: true,
    vertTouchDrag: true,
  },
  handleScale: {
    mouseWheel: false,
    pinch: true,
    axisPressedMouseMove: true,
    allowShiftDragZoom: true,
  },
  });
  return chart;
}

const charts = {
  0: { chart: makeChart("d0-chart", true), series: [] },
  1: { chart: makeChart("d1-chart", true), series: [] },
  2: { chart: makeChart("d2-chart", true), series: [] },
};
const lowCharts = {
  0: { chart: makeChart("l0-chart", true), series: [] },
  1: { chart: makeChart("l1-chart", true), series: [] },
  2: { chart: makeChart("l2-chart", true), series: [] },
};
const d0ForecastSeries = charts[0].chart.addLineSeries({
  color: "#f0b400", lineWidth: 2, lineType: LightweightCharts.LineType.WithSteps,
  priceFormat: { type: "price", precision: 1, minMove: 0.1 }, priceScaleId: "right",
  pointMarkersVisible: true,
  pointMarkersRadius: 2,
});
const l0ForecastLowSeries = lowCharts[0].chart.addLineSeries({
  color: "#38bdf8", lineWidth: 2, lineType: LightweightCharts.LineType.WithSteps,
  priceFormat: { type: "price", precision: 1, minMove: 0.1 }, priceScaleId: "right",
  pointMarkersVisible: true,
  pointMarkersRadius: 2,
});
const d0HourlyForecastSeries = charts[0].chart.addLineSeries({
  color: "#c084fc", lineWidth: 1, lineType: LightweightCharts.LineType.WithSteps,
  priceFormat: { type: "price", precision: 1, minMove: 0.1 }, priceScaleId: "right",
  priceLineVisible: false,
});
const d0HourlyActualSeries = charts[0].chart.addLineSeries({
  color: "#f97316", lineWidth: 4, lineType: LightweightCharts.LineType.WithSteps,
  priceFormat: { type: "price", precision: 1, minMove: 0.1 }, priceScaleId: "right",
  priceLineVisible: false,
  pointMarkersVisible: true,
  pointMarkersRadius: 2,
});
const d0ActualMaxSeries = charts[0].chart.addLineSeries({
  color: "#26a69a", lineWidth: 2,
  priceFormat: { type: "price", precision: 1, minMove: 0.1 }, priceScaleId: "right",
});
const l0HourlyForecastSeries = lowCharts[0].chart.addLineSeries({
  color: "#c084fc", lineWidth: 1, lineType: LightweightCharts.LineType.WithSteps,
  priceFormat: { type: "price", precision: 1, minMove: 0.1 }, priceScaleId: "right",
  priceLineVisible: false,
});
const l0HourlyActualSeries = lowCharts[0].chart.addLineSeries({
  color: "#f97316", lineWidth: 4, lineType: LightweightCharts.LineType.WithSteps,
  priceFormat: { type: "price", precision: 1, minMove: 0.1 }, priceScaleId: "right",
  priceLineVisible: false,
  pointMarkersVisible: true,
  pointMarkersRadius: 2,
});
const l0ActualMinSeries = lowCharts[0].chart.addLineSeries({
  color: "#2dd4bf", lineWidth: 2,
  priceFormat: { type: "price", precision: 1, minMove: 0.1 }, priceScaleId: "right",
});
const l0CurrentTempSeries = lowCharts[0].chart.addLineSeries({
  color: "#5b9bd5", lineWidth: 3, lineStyle: LightweightCharts.LineStyle.Dotted,
  priceFormat: { type: "price", precision: 1, minMove: 0.1 }, priceScaleId: "right",
  pointMarkersVisible: true,
  pointMarkersRadius: 3,
});
const d0CurrentTempSeries = charts[0].chart.addLineSeries({
  color: "#5b9bd5", lineWidth: 3, lineStyle: LightweightCharts.LineStyle.Dotted,
  priceFormat: { type: "price", precision: 1, minMove: 0.1 }, priceScaleId: "right",
  pointMarkersVisible: true,
  pointMarkersRadius: 3,
});

const pnlChart = LightweightCharts.createChart(document.getElementById("pnl-chart"), {
  layout: { background: { color: "#161b22" }, textColor: "#e6edf3" },
  grid: {
    vertLines: { color: "#21262d" },
    horzLines: { color: "#21262d" },
  },
  timeScale: {
    timeVisible: true,
    secondsVisible: false,
    borderColor: "#30363d",
    tickMarkFormatter: fmtHKTTime,
  },
  rightPriceScale: { borderColor: "#30363d" },
  crosshair: {
    mode: LightweightCharts.CrosshairMode.Normal,
    horzLine: { visible: false, labelVisible: false },
    vertLine: { color: "#7d8590", style: LightweightCharts.LineStyle.Dotted },
  },
  localization: {
    timeFormatter: fmtHKT,
    priceFormatter: (p) => (p >= 0 ? "$" : "-$") + Math.abs(p).toFixed(2),
  },
  handleScroll: {
    mouseWheel: false,
    pressedMouseMove: true,
    horzTouchDrag: true,
    vertTouchDrag: true,
  },
  handleScale: { mouseWheel: false, pinch: true, axisPressedMouseMove: true },
});
const realizedSeries = pnlChart.addLineSeries({
  color: "#26a69a", lineWidth: 2,
  lineType: LightweightCharts.LineType.WithSteps,
});
const unrealizedSeries = pnlChart.addLineSeries({
  color: "#5b9bd5", lineWidth: 1,
});
const totalSeries = pnlChart.addLineSeries({
  color: "#f0b400", lineWidth: 2,
});
let d0ForecastData = [];
let d0HourlyForecastData = [];
let d0HourlyActualData = [];
let d0HourlyErrorData = [];
let d0ActualMaxData = [];
let d0CurrentTempData = [];
let l0ForecastLowData = [];
let l0HourlyForecastData = [];
let l0HourlyActualData = [];
let l0ActualMinData = [];
let l0CurrentTempData = [];
let realizedData = [];
let unrealizedData = [];
let totalData = [];
let tokenSide = "YES";
const fittedCharts = new Set();
const seriesVisibility = {
  forecastHigh: true,
  forecastLow: true,
  lowHourlyForecast: true,
  lowHourlyActual: false,
  hourlyForecast: true,
  hourlyActual: false,
  actualMin: true,
  actualMax: true,
  currentTemp: true,
};
const d0SeriesByKey = {
  forecastHigh: d0ForecastSeries,
  hourlyForecast: d0HourlyForecastSeries,
  hourlyActual: d0HourlyActualSeries,
  actualMax: d0ActualMaxSeries,
  currentTemp: d0CurrentTempSeries,
};
const l0SeriesByKey = {
  forecastLow: l0ForecastLowSeries,
  lowHourlyForecast: l0HourlyForecastSeries,
  lowHourlyActual: l0HourlyActualSeries,
  actualMin: l0ActualMinSeries,
  lowCurrentTemp: l0CurrentTempSeries,
};

window.addEventListener("resize", () => {
  renderAllTradeBubbles();
});

function fmtMoney(v) {
  if (v == null || isNaN(v)) return "n/a";
  const sign = v < 0 ? "-" : "";
  return sign + "$" + Math.abs(v).toFixed(2);
}
function fmtTemp(v) {
  if (v == null || isNaN(v)) return "n/a";
  return Number(v).toString() + "°C";
}
function classForMoney(v) {
  if (v == null || isNaN(v) || v === 0) return "";
  return v > 0 ? "pos" : "neg";
}

function renderStats(stats) {
  const f = stats.latest_forecast || {};
  const o = stats.latest_observation || {};
  const cells = [
    { label: "Forecast date",       value: f.forecast_date_hkt || "n/a" },
    { label: "Bot signal high",     value: fmtTemp(f.forecast_max_c) },
    { label: "Bot signal low",      value: fmtTemp(f.forecast_min_c) },
    { label: "Forecast updated",    value: fmtHKTUpdate(f.update_time) },
    { label: "Since-midnight min",  value: o.since_midnight_min_c != null ? fmtTemp(o.since_midnight_min_c) : (o.temperature_c != null ? fmtTemp(o.temperature_c) + " (cur)" : "n/a") },
    { label: "Since-midnight max",  value: o.since_midnight_max_c != null ? fmtTemp(o.since_midnight_max_c) : (o.temperature_c != null ? fmtTemp(o.temperature_c) + " (cur)" : "n/a") },
    { label: "Current temp",        value: o.temperature_c != null ? fmtTemp(o.temperature_c) : "n/a" },
    { label: "Observed at",         value: o.observed_at_hkt ? o.observed_at_hkt.slice(11,16) + " HKT" : "n/a" },
    { label: "Open positions",      value: String(stats.open_positions ?? 0), drilldown: "open" },
    { label: "Realized PnL",        value: fmtMoney(stats.realized_pnl), cls: classForMoney(stats.realized_pnl), drilldown: "realized" },
    { label: "Unrealized PnL",      value: fmtMoney(stats.executable_unrealized_pnl), cls: classForMoney(stats.executable_unrealized_pnl), drilldown: "unrealized" },
    { label: "Total profit est.",   value: fmtMoney(stats.total_profit), cls: classForMoney(stats.total_profit) },
    { label: "Worst-case open loss", value: fmtMoney(-Math.abs(stats.worst_case_open_loss || 0)), cls: stats.worst_case_open_loss ? "neg" : "" },
    { label: "Buys filled / missed", value: stats.counts.buy_filled + " / " + stats.counts.buy_missed },
    { label: "Sells filled / missed", value: stats.counts.sell_filled + " / " + stats.counts.sell_missed },
    { label: "Markets / outcomes",  value: stats.counts.markets + " / " + stats.counts.outcomes },
    { label: "Orderbook snapshots", value: String(stats.counts.orderbooks) },
  ];
  document.getElementById("stats").innerHTML = cells.map(c =>
    `<div class="stat ${c.drilldown ? "clickable" : ""}" ${c.drilldown ? `data-drilldown="${c.drilldown}" tabindex="0" role="button"` : ""}><div class="label">${c.label}</div><div class="value ${c.cls||""}">${c.value}</div></div>`
  ).join("");
  bindDrilldownTiles();
}

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(url + " " + res.status);
  return res.json();
}

let activeDrilldown = null;

function setChartsVisible(visible) {
  document.querySelectorAll(".chart-section").forEach(section => {
    section.style.display = visible ? "" : "none";
  });
}

function fmtCell(value, kind) {
  if (value == null || value === "") return "";
  if (kind === "money") return fmtMoney(Number(value));
  if (kind === "price") return Number(value).toFixed(3);
  if (kind === "shares") return Number(value).toFixed(4);
  return escapeHtml(String(value));
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, ch => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));
}

function renderTradeTable(payload) {
  const root = document.getElementById("trade-drilldown");
  document.getElementById("trade-drilldown-title").textContent = payload.title || "Trades";
  const body = document.getElementById("trade-drilldown-body");
  if (!payload.rows || payload.rows.length === 0) {
    body.innerHTML = '<div class="empty">No filled paper trades for this view.</div>';
    root.classList.add("active");
    setChartsVisible(false);
    return;
  }
  const rows = payload.rows.map(row => `
    <tr>
      <td>${fmtCell(row.created_at_hkt || row.created_at_utc)}</td>
      <td>${fmtCell(row.target_date_hkt)}</td>
      <td>${fmtCell(row.label)}</td>
      <td>${fmtCell(row.token_side)}</td>
      <td>${fmtCell(row.action)}</td>
      <td>${fmtCell(row.fill_price, "price")}</td>
      <td>${fmtCell(row.fill_size_usd, "money")}</td>
      <td>${fmtCell(row.shares, "shares")}</td>
      <td>${fmtCell(row.net_shares, "shares")}</td>
      <td>${fmtCell(row.avg_price, "price")}</td>
      <td>${fmtCell(row.latest_bid, "price")}</td>
      <td>${fmtCell(row.realized_pnl, "money")}</td>
      <td>${fmtCell(row.unrealized_pnl, "money")}</td>
      <td>${fmtCell(row.reason)}</td>
    </tr>
  `).join("");
  body.innerHTML = `
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Time HKT</th><th>Date</th><th>Outcome</th><th>Token</th><th>Action</th>
            <th>Fill</th><th>USD</th><th>Shares</th><th>Open shares</th>
            <th>Avg entry</th><th>Bid</th><th>Realized</th><th>Unrealized</th><th>Reason</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `;
  root.classList.add("active");
  setChartsVisible(false);
}

async function showTradeDrilldown(view) {
  activeDrilldown = view;
  const payload = await fetchJSON(`/api/paper-trades?view=${encodeURIComponent(view)}`);
  renderTradeTable(payload);
}

function closeTradeDrilldown() {
  activeDrilldown = null;
  document.getElementById("trade-drilldown").classList.remove("active");
  setChartsVisible(true);
  renderAllTradeBubbles();
}

function bindDrilldownTiles() {
  document.querySelectorAll("[data-drilldown]").forEach(tile => {
    if (tile.dataset.drilldownBound === "1") return;
    tile.dataset.drilldownBound = "1";
    const open = () => showTradeDrilldown(tile.dataset.drilldown);
    tile.addEventListener("click", open);
    tile.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        open();
      }
    });
  });
}

const oddsColors = ["#ef5350", "#ab47bc", "#66bb6a", "#ffb74d", "#4dd0e1"];
const tooltip = document.getElementById("chart-tooltip");
let tooltipTimer = null;
let tooltipState = null;

function resetLeadChart(lead) {
  for (const s of charts[lead].series) {
    charts[lead].chart.removeSeries(s.series);
    if (s.markerSeries) charts[lead].chart.removeSeries(s.markerSeries);
  }
  charts[lead].series = [];
  for (const s of lowCharts[lead].series) {
    lowCharts[lead].chart.removeSeries(s.series);
    if (s.markerSeries) lowCharts[lead].chart.removeSeries(s.markerSeries);
  }
  lowCharts[lead].series = [];
}

function chartValueAt(points, time) {
  if (!points || !points.length || time == null) return null;
  let lo = 0;
  let hi = points.length - 1;
  let best = -1;
  while (lo <= hi) {
    const mid = Math.floor((lo + hi) / 2);
    if (points[mid].time <= time) {
      best = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return best >= 0 ? points[best].value : null;
}

function formatValue(value, kind) {
  if (value == null || Number.isNaN(value)) return "n/a";
  if (kind === "money") return fmtMoney(value);
  if (kind === "odds") return value.toFixed(3);
  if (kind === "delta") return (value > 0 ? "+" : "") + value.toFixed(1) + "°C";
  return value.toFixed(1) + "°C";
}

function visibleData(key, data) {
  return seriesVisibility[key] ? data : [];
}

function lineDataForDisplay(points, spanSeconds = 600) {
  if (!points || points.length !== 1) return points || [];
  return [
    points[0],
    { time: points[0].time + spanSeconds, value: points[0].value },
  ];
}

function applySeriesVisibility() {
  Object.entries(d0SeriesByKey).forEach(([key, series]) => {
    series.applyOptions({ visible: seriesVisibility[key] });
  });
  Object.entries(l0SeriesByKey).forEach(([key, series]) => {
    series.applyOptions({ visible: seriesVisibility[key] });
  });
  document.querySelectorAll("[data-series-key]").forEach((button) => {
    const key = button.dataset.seriesKey;
    const visible = seriesVisibility[key] !== false;
    button.classList.toggle("off", !visible);
    button.setAttribute("aria-pressed", visible ? "true" : "false");
  });
  Object.values(charts).forEach((chartState) => {
    chartState.series.forEach((descriptor) => {
      const visible = seriesVisibility[descriptor.key] !== false;
      descriptor.series.applyOptions({ visible });
      if (descriptor.markerSeries) descriptor.markerSeries.applyOptions({ visible });
    });
  });
  Object.values(lowCharts).forEach((chartState) => {
    chartState.series.forEach((descriptor) => {
      const visible = seriesVisibility[descriptor.key] !== false;
      descriptor.series.applyOptions({ visible });
      if (descriptor.markerSeries) descriptor.markerSeries.applyOptions({ visible });
    });
  });
  renderAllTradeBubbles();
}

function isSeriesVisible(key) {
  return seriesVisibility[key] !== false;
}

function legendButton(key, color, label) {
  if (!(key in seriesVisibility)) seriesVisibility[key] = true;
  const off = seriesVisibility[key] ? "" : " off";
  return `<button type="button" class="${off}" data-series-key="${key}" aria-pressed="${seriesVisibility[key] ? "true" : "false"}"><i style="background:${color}"></i>${label}</button>`;
}

function bindSeriesToggleButtons(root = document) {
  root.querySelectorAll("[data-series-key]").forEach((button) => {
    if (button.dataset.toggleBound === "1") return;
    button.dataset.toggleBound = "1";
    button.addEventListener("click", () => {
      const key = button.dataset.seriesKey;
      seriesVisibility[key] = !isSeriesVisible(key);
      applySeriesVisibility();
    });
  });
}

function installModifierWheelZoom(containerId, chart) {
  const container = document.getElementById(containerId);
  container.addEventListener("wheel", (event) => {
    if (!event.metaKey && !event.ctrlKey) return;
    event.preventDefault();
    const range = chart.timeScale().getVisibleLogicalRange();
    if (!range) return;
    const rect = container.getBoundingClientRect();
    const cursorX = event.clientX - rect.left;
    const span = range.to - range.from;
    const cursorLogical = chart.timeScale().coordinateToLogical(cursorX);
    if (cursorLogical == null) return;
    const cursorRatio = Math.min(Math.max((cursorLogical - range.from) / span, 0), 1);
    const factor = event.deltaY < 0 ? 0.85 : 1.15;
    const nextSpan = Math.max(10, span * factor);
    chart.timeScale().setVisibleLogicalRange({
      from: cursorLogical - nextSpan * cursorRatio,
      to: cursorLogical + nextSpan * (1 - cursorRatio),
    });
    renderAllTradeBubbles();
  }, { passive: false });
}

function fitChartOnce(key, chart) {
  if (fittedCharts.has(key)) return;
  chart.timeScale().fitContent();
  fittedCharts.add(key);
}

function hktWallClockUnix(dateText, hour = 0, minute = 0, second = 0) {
  const parts = String(dateText || "").split("-").map(Number);
  if (parts.length !== 3 || parts.some(Number.isNaN)) return null;
  const [year, month, day] = parts;
  return Math.floor(Date.UTC(year, month - 1, day, hour, minute, second) / 1000) - HKT_OFFSET_SEC;
}

function hktDayRange(dateText) {
  const from = hktWallClockUnix(dateText, 0, 0, 0);
  const to = hktWallClockUnix(dateText, 23, 59, 59);
  if (from == null || to == null) return null;
  return { from, to };
}

function setHktDayVisibleRange(chart, dateText) {
  const range = hktDayRange(dateText);
  if (!range) {
    chart.timeScale().fitContent();
    return;
  }
  chart.timeScale().setVisibleRange(range);
}

function nearestTrade(trades, time) {
  if (!trades || !trades.length || time == null) return null;
  let best = null;
  let bestDelta = Infinity;
  for (const trade of trades) {
    const delta = Math.abs(trade.time - time);
    if (delta < bestDelta) {
      best = trade;
      bestDelta = delta;
    }
  }
  return bestDelta <= 300 ? best : null;
}

function markerOnlySeries(chart, markers) {
  if (!markers || !markers.length) return null;
  const s = chart.addLineSeries({
    color: "rgba(0, 0, 0, 0)",
    lineWidth: 1,
    lineVisible: false,
    pointMarkersVisible: false,
    crosshairMarkerVisible: false,
    lastValueVisible: false,
    priceLineVisible: false,
    priceFormat: { type: "price", precision: 2, minMove: 0.01 },
    priceScaleId: "left",
  });
  s.setData(markers.map(m => ({ time: m.time, value: m.price })));
  return s;
}

function renderTradeBubbles(lead) {
  renderTradeBubblesForChart(charts, `d${lead}-chart`, lead);
  renderTradeBubblesForChart(lowCharts, `l${lead}-chart`, lead);
}

function renderTradeBubblesForChart(chartMap, containerId, lead) {
  const container = document.getElementById(containerId);
  if (!container) return;
  container.querySelectorAll(".trade-bubble").forEach(el => el.remove());
  const chart = chartMap[lead].chart;
  const width = container.clientWidth;
  const height = container.clientHeight;
  for (const descriptor of chartMap[lead].series) {
    for (const marker of descriptor.markers || []) {
      const coordinateSeries = descriptor.markerSeries || descriptor.series;
      if (!coordinateSeries || !coordinateSeries.priceToCoordinate) continue;
      if (!isSeriesVisible(descriptor.key)) continue;
      const x = chart.timeScale().timeToCoordinate(marker.time);
      const y = coordinateSeries.priceToCoordinate(marker.price);
      if (x == null || y == null || x < 0 || y < 0 || x > width || y > height) continue;
      const isSell = marker.text === "S";
      const bubble = document.createElement("div");
      bubble.className = `trade-bubble ${isSell ? "sell" : "buy"}`;
      bubble.textContent = marker.text;
      const signalText = marker.signal ? ` · ${marker.signal}` : "";
      const signalTimeText = marker.signal_time_hkt ? ` · signal ${marker.signal_time_hkt} HKT` : "";
      bubble.title = `${descriptor.name} ${marker.text} @ ${marker.price.toFixed(3)}${marker.size_usd != null ? " · $" + marker.size_usd.toFixed(2) : ""}${signalText}${signalTimeText}`;
      bubble.style.left = `${x}px`;
      bubble.style.top = `${y}px`;
      container.appendChild(bubble);
    }
  }
}

function renderAllTradeBubbles() {
  requestAnimationFrame(() => {
    renderTradeBubbles(0);
    renderTradeBubbles(1);
    renderTradeBubbles(2);
  });
}

function showTooltip(state) {
  if (!state || (state.values.length === 0 && state.trades.length === 0)) {
    tooltip.style.display = "none";
    return;
  }
  tooltip.innerHTML = [
    `<div class="time">${fmtHKT(state.time)}</div>`,
    ...state.values.map(v =>
      `<div class="row"><span class="name"><i style="background:${v.color}"></i>${v.name}</span><span>${formatValue(v.value, v.kind)}</span></div>`
    ),
    ...state.trades.map(t => {
      const tradeBits = [
        `${t.text} @ ${t.price.toFixed(3)}`,
        t.size_usd != null ? "$" + t.size_usd.toFixed(2) : null,
        t.signal || null,
        t.signal_time_hkt ? "signal " + t.signal_time_hkt + " HKT" : null,
      ].filter(Boolean).join(" · ");
      return `<div class="row"><span class="name"><i style="background:${t.color}"></i>${t.name}</span><span>${tradeBits}</span></div>`;
    }),
  ].join("");
  tooltip.style.display = "block";
  const pad = 14;
  const rect = tooltip.getBoundingClientRect();
  let left = state.clientX + 14;
  let top = state.clientY + 14;
  if (left + rect.width + pad > window.innerWidth) left = state.clientX - rect.width - 14;
  if (top + rect.height + pad > window.innerHeight) top = state.clientY - rect.height - 14;
  tooltip.style.left = Math.max(pad, left) + "px";
  tooltip.style.top = Math.max(pad, top) + "px";
}

function scheduleTooltip(state) {
  tooltipState = state;
  if (tooltipTimer) clearTimeout(tooltipTimer);
  tooltip.style.display = "none";
  tooltipTimer = setTimeout(() => showTooltip(tooltipState), 1000);
}

function hideTooltip() {
  tooltipState = null;
  if (tooltipTimer) clearTimeout(tooltipTimer);
  tooltipTimer = null;
  tooltip.style.display = "none";
}

function attachTooltip(chart, containerId, descriptorsFn) {
  const container = document.getElementById(containerId);
  chart.subscribeCrosshairMove((param) => {
    if (!param || param.time == null || !param.point) {
      hideTooltip();
      return;
    }
    const rect = container.getBoundingClientRect();
    const values = descriptorsFn()
      .map(d => ({ ...d, value: chartValueAt(d.data, param.time) }))
      .filter(d => d.value != null);
    const trades = descriptorsFn()
      .map(d => ({ descriptor: d, trade: nearestTrade(d.markers, param.time) }))
      .filter(item => item.trade != null)
      .map(item => ({
        ...item.trade,
        name: item.descriptor.name,
        color: item.trade.color || item.descriptor.color,
      }));
    scheduleTooltip({
      time: param.time,
      clientX: rect.left + param.point.x,
      clientY: rect.top + param.point.y,
      values,
      trades,
    });
  });
  container.addEventListener("mouseleave", hideTooltip);
}

attachTooltip(charts[0].chart, "d0-chart", () => [
  { name: "Bot signal high", color: "#f0b400", kind: "temp", data: visibleData("forecastHigh", d0ForecastData) },
  { name: "Latest hourly forecast", color: "#c084fc", kind: "temp", data: visibleData("hourlyForecast", d0HourlyForecastData) },
  { name: "Hourly actual", color: "#f97316", kind: "temp", data: visibleData("hourlyActual", d0HourlyActualData) },
  { name: "Actual - forecast", color: "#94a3b8", kind: "delta", data: visibleData("hourlyActual", d0HourlyErrorData) },
  { name: "Since-midnight max", color: "#26a69a", kind: "temp", data: visibleData("actualMax", d0ActualMaxData) },
  { name: "Current temperature", color: "#5b9bd5", kind: "temp", data: visibleData("currentTemp", d0CurrentTempData) },
  ...charts[0].series.map(s => ({ name: s.name, color: s.color, kind: "odds", data: visibleData(s.key, s.data), markers: isSeriesVisible(s.key) ? s.markers : [] })),
]);
attachTooltip(lowCharts[0].chart, "l0-chart", () => [
  { name: "Bot signal low", color: "#38bdf8", kind: "temp", data: visibleData("forecastLow", l0ForecastLowData) },
  { name: "Latest hourly forecast", color: "#c084fc", kind: "temp", data: visibleData("lowHourlyForecast", l0HourlyForecastData) },
  { name: "Hourly actual", color: "#f97316", kind: "temp", data: visibleData("lowHourlyActual", l0HourlyActualData) },
  { name: "Since-midnight min", color: "#2dd4bf", kind: "temp", data: visibleData("actualMin", l0ActualMinData) },
  { name: "Current temperature", color: "#5b9bd5", kind: "temp", data: visibleData("lowCurrentTemp", l0CurrentTempData) },
  ...lowCharts[0].series.map(s => ({ name: s.name, color: s.color, kind: "odds", data: visibleData(s.key, s.data), markers: isSeriesVisible(s.key) ? s.markers : [] })),
]);
attachTooltip(charts[1].chart, "d1-chart", () =>
  charts[1].series.map(s => ({ name: s.name, color: s.color, kind: s.kind, data: visibleData(s.key, s.data), markers: isSeriesVisible(s.key) ? s.markers : [] }))
);
attachTooltip(lowCharts[1].chart, "l1-chart", () =>
  lowCharts[1].series.map(s => ({ name: s.name, color: s.color, kind: s.kind, data: visibleData(s.key, s.data), markers: isSeriesVisible(s.key) ? s.markers : [] }))
);
attachTooltip(charts[2].chart, "d2-chart", () =>
  charts[2].series.map(s => ({ name: s.name, color: s.color, kind: s.kind, data: visibleData(s.key, s.data), markers: isSeriesVisible(s.key) ? s.markers : [] }))
);
attachTooltip(lowCharts[2].chart, "l2-chart", () =>
  lowCharts[2].series.map(s => ({ name: s.name, color: s.color, kind: s.kind, data: visibleData(s.key, s.data), markers: isSeriesVisible(s.key) ? s.markers : [] }))
);
attachTooltip(pnlChart, "pnl-chart", () => [
  { name: "Realized", color: "#26a69a", kind: "money", data: realizedData },
  { name: "Unrealized", color: "#5b9bd5", kind: "money", data: unrealizedData },
  { name: "Total", color: "#f0b400", kind: "money", data: totalData },
]);
Object.values(charts).forEach(c => {
  c.chart.timeScale().subscribeVisibleTimeRangeChange(renderAllTradeBubbles);
});
Object.values(lowCharts).forEach(c => {
  c.chart.timeScale().subscribeVisibleTimeRangeChange(renderAllTradeBubbles);
});

function renderLeadPanel(panel) {
  const lead = panel.lead_days;
  document.getElementById(`d${lead}-date`).textContent = panel.target_date;
  document.getElementById(`l${lead}-date`).textContent = panel.target_date;
  if (lead === 0) {
    resetLeadChart(0);
    d0ForecastData = panel.forecast;
    l0ForecastLowData = panel.forecast_low || [];
    d0HourlyForecastData = panel.hourly_forecast || [];
    d0HourlyActualData = lineDataForDisplay(panel.hourly_actual || [], 600);
    d0HourlyErrorData = panel.hourly_error || [];
    l0HourlyForecastData = panel.hourly_forecast || [];
    l0HourlyActualData = lineDataForDisplay(panel.hourly_actual || [], 600);
    l0ActualMinData = panel.actual_min || [];
    d0ActualMaxData = panel.actual_max;
    d0CurrentTempData = lineDataForDisplay(panel.current_temp || [], 600);
    l0CurrentTempData = lineDataForDisplay(panel.current_temp || [], 600);
    d0ForecastSeries.setData(d0ForecastData);
    l0ForecastLowSeries.setData(l0ForecastLowData);
    d0HourlyForecastSeries.setData(d0HourlyForecastData);
    d0HourlyActualSeries.setData(d0HourlyActualData);
    l0HourlyForecastSeries.setData(l0HourlyForecastData);
    l0HourlyActualSeries.setData(l0HourlyActualData);
    l0ActualMinSeries.setData(l0ActualMinData);
    d0ActualMaxSeries.setData(d0ActualMaxData);
    d0CurrentTempSeries.setData(d0CurrentTempData);
    l0CurrentTempSeries.setData(l0CurrentTempData);
    const legend = [];
    panel.top_tokens.forEach((item, idx) => {
      const color = oddsColors[idx % oddsColors.length];
      const key = `d0-token-${item.token_id}`;
      const s = charts[0].chart.addLineSeries({
        color,
        lineWidth: 1,
        priceFormat: { type: "price", precision: 2, minMove: 0.01 },
        priceScaleId: "left",
        priceLineVisible: false,
      });
      s.setData(item.points);
      const markerSeries = markerOnlySeries(charts[0].chart, item.markers || []);
      charts[0].series.push({ key, series: s, markerSeries, name: `${item.label} ${item.side}`, color, kind: "odds", data: item.points, markers: item.markers || [] });
      legend.push(legendButton(key, color, `${item.label} ${item.side} (${item.latest_price.toFixed(2)})`));
    });
    document.getElementById("d0-legend").innerHTML = legend.join("");
    bindSeriesToggleButtons(document.getElementById("d0-legend"));
    const lowLegend = [];
    (panel.low_tokens || []).forEach((item, idx) => {
      const color = oddsColors[idx % oddsColors.length];
      const key = `l0-token-${item.token_id}`;
      const s = lowCharts[0].chart.addLineSeries({
        color,
        lineWidth: 1,
        priceFormat: { type: "price", precision: 2, minMove: 0.01 },
        priceScaleId: "left",
        priceLineVisible: false,
      });
      s.setData(item.points);
      const markerSeries = markerOnlySeries(lowCharts[0].chart, item.markers || []);
      lowCharts[0].series.push({ key, series: s, markerSeries, name: `${item.label} ${item.side}`, color, kind: "odds", data: item.points, markers: item.markers || [] });
      lowLegend.push(legendButton(key, color, `${item.label} ${item.side} (${item.latest_price.toFixed(2)})`));
    });
    document.getElementById("l0-legend").innerHTML = lowLegend.join("");
    bindSeriesToggleButtons(document.getElementById("l0-legend"));
    applySeriesVisibility();
    setHktDayVisibleRange(charts[0].chart, panel.target_date);
    setHktDayVisibleRange(lowCharts[0].chart, panel.target_date);
    return;
  }

  resetLeadChart(lead);
  const forecast = charts[lead].chart.addLineSeries({
    color: "#f0b400",
    lineWidth: 2,
    lineType: LightweightCharts.LineType.WithSteps,
    priceFormat: { type: "price", precision: 1, minMove: 0.1 },
    priceScaleId: "right",
    pointMarkersVisible: true,
    pointMarkersRadius: 2,
  });
  forecast.setData(panel.forecast);
  charts[lead].series.push({ series: forecast, name: "Bot signal high", color: "#f0b400", kind: "temp", data: panel.forecast });
  const forecastLow = lowCharts[lead].chart.addLineSeries({
    color: "#38bdf8",
    lineWidth: 2,
    lineType: LightweightCharts.LineType.WithSteps,
    priceFormat: { type: "price", precision: 1, minMove: 0.1 },
    priceScaleId: "right",
    pointMarkersVisible: true,
    pointMarkersRadius: 2,
  });
  forecastLow.setData(panel.forecast_low || []);
  lowCharts[lead].series.push({ series: forecastLow, name: "Bot signal low", color: "#38bdf8", kind: "temp", data: panel.forecast_low || [] });

  const legend = [
    `<span><i style="background:#f0b400"></i>Bot signal high (right °C)</span>`
  ];
  panel.top_tokens.forEach((item, idx) => {
    const color = oddsColors[idx % oddsColors.length];
    const key = `d${lead}-token-${item.token_id}`;
    const s = charts[lead].chart.addLineSeries({
      color,
      lineWidth: 1,
      priceFormat: { type: "price", precision: 2, minMove: 0.01 },
      priceScaleId: "left",
      priceLineVisible: false,
    });
    s.setData(item.points);
    const markerSeries = markerOnlySeries(charts[lead].chart, item.markers || []);
    charts[lead].series.push({ key, series: s, markerSeries, name: `${item.label} ${item.side}`, color, kind: "odds", data: item.points, markers: item.markers || [] });
    legend.push(legendButton(key, color, `${item.label} ${item.side} (${item.latest_price.toFixed(2)})`));
  });
  document.getElementById(`d${lead}-legend`).innerHTML = legend.join("");
  bindSeriesToggleButtons(document.getElementById(`d${lead}-legend`));
  const lowLegend = [
    `<span><i style="background:#38bdf8"></i>Bot signal low (right °C)</span>`
  ];
  (panel.low_tokens || []).forEach((item, idx) => {
    const color = oddsColors[idx % oddsColors.length];
    const key = `l${lead}-token-${item.token_id}`;
    const s = lowCharts[lead].chart.addLineSeries({
      color,
      lineWidth: 1,
      priceFormat: { type: "price", precision: 2, minMove: 0.01 },
      priceScaleId: "left",
      priceLineVisible: false,
    });
    s.setData(item.points);
    const markerSeries = markerOnlySeries(lowCharts[lead].chart, item.markers || []);
    lowCharts[lead].series.push({ key, series: s, markerSeries, name: `${item.label} ${item.side}`, color, kind: "odds", data: item.points, markers: item.markers || [] });
    lowLegend.push(legendButton(key, color, `${item.label} ${item.side} (${item.latest_price.toFixed(2)})`));
  });
  document.getElementById(`l${lead}-legend`).innerHTML = lowLegend.join("");
  bindSeriesToggleButtons(document.getElementById(`l${lead}-legend`));
  applySeriesVisibility();
  if (panel.forecast.length || panel.top_tokens.some(s => s.points.length)) {
    fitChartOnce(`d${lead}`, charts[lead].chart);
  }
  if ((panel.forecast_low || []).length || (panel.low_tokens || []).some(s => s.points.length)) {
    fitChartOnce(`l${lead}`, lowCharts[lead].chart);
  }
}

async function loadAll() {
  const [stats, temp, pnl] = await Promise.all([
    fetchJSON("/api/stats"),
    fetchJSON(`/api/forecast-panels?side=${encodeURIComponent(tokenSide)}`),
    fetchJSON("/api/pnl"),
  ]);
  renderStats(stats);

  temp.panels.forEach(renderLeadPanel);
  renderAllTradeBubbles();

  realizedData = pnl.realized;
  unrealizedData = pnl.unrealized;
  totalData = pnl.total;
  realizedSeries.setData(realizedData);
  unrealizedSeries.setData(unrealizedData);
  totalSeries.setData(totalData);
  if (pnl.realized.length) fitChartOnce("pnl", pnlChart);

  document.getElementById("last-update").textContent =
    "updated " + new Date().toLocaleTimeString();
  if (activeDrilldown) {
    showTradeDrilldown(activeDrilldown);
  }
}

document.getElementById("refresh-btn").addEventListener("click", () => {
  loadAll();
});
document.getElementById("close-drilldown").addEventListener("click", closeTradeDrilldown);
document.getElementById("token-side").addEventListener("change", (event) => {
  tokenSide = event.target.value === "NO" ? "NO" : "YES";
  fittedCharts.clear();
  loadAll();
});
bindSeriesToggleButtons();
Object.values(charts).forEach((chartState, idx) => {
  installModifierWheelZoom(`d${idx}-chart`, chartState.chart);
});
Object.values(lowCharts).forEach((chartState, idx) => {
  installModifierWheelZoom(`l${idx}-chart`, chartState.chart);
});
installModifierWheelZoom("pnl-chart", pnlChart);

applySeriesVisibility();
loadAll();
setInterval(() => loadAll(), 15000);
</script>
</body>
</html>
"""


def _resolve_target_date(db: sqlite3.Connection, requested: str | None) -> str:
    if requested:
        return requested
    today = datetime.now(HKT).date().isoformat()
    dates = available_forecast_dates(db)
    if today in dates:
        return today
    return dates[-1] if dates else today


LIVE_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>whenitrains live dashboard</title>
<style>
  body { margin: 0; background: #0f1419; color: #d7dde5; font: 14px/1.4 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  header { padding: 18px 24px; border-bottom: 1px solid #30363d; display: flex; justify-content: space-between; align-items: baseline; }
  h1 { font-size: 18px; margin: 0; font-weight: 650; }
  main { padding: 20px 24px; max-width: 1200px; margin: 0 auto; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 18px; }
  .card { border: 1px solid #30363d; border-radius: 6px; padding: 12px; background: #151b23; }
  .label { color: #8b949e; font-size: 12px; }
  .value { font-size: 22px; margin-top: 4px; }
  table { width: 100%; border-collapse: collapse; background: #151b23; border: 1px solid #30363d; }
  th, td { padding: 8px 10px; border-bottom: 1px solid #30363d; text-align: left; white-space: nowrap; }
  th { color: #8b949e; font-weight: 600; font-size: 12px; }
  a { color: #7cc7ff; text-decoration: none; }
  .on { color: #ffb86b; }
  .off { color: #7ee787; }
</style>
</head>
<body>
<header>
  <h1>whenitrains · live desk</h1>
  <a href="/">paper dashboard</a>
</header>
<main>
  <section class="grid" id="stats"></section>
  <h2>Recent live orders</h2>
  <table>
    <thead><tr><th>Time</th><th>Label</th><th>Side</th><th>Status</th><th>Limit</th><th>Fill</th><th>Size</th><th>Reason</th></tr></thead>
    <tbody id="orders"></tbody>
  </table>
</main>
<script>
function money(v) { return v == null ? "n/a" : "$" + Number(v).toFixed(2); }
function num(v) { return v == null ? "n/a" : Number(v).toFixed(4); }
async function refresh() {
  const res = await fetch("/api/live/stats", { cache: "no-store" });
  const data = await res.json();
  const stats = [
    ["Open positions", data.open_positions],
    ["Open exposure", money(data.open_exposure_usd)],
    ["Realized PnL", money(data.realized_pnl)],
    ["Orders", data.counts.orders],
    ["Filled", data.counts.filled],
    ["Submitted", data.counts.submitted],
    ["Rejected", data.counts.rejected],
    ["Blocked", data.counts.blocked],
    ["Errors", data.counts.error],
    ["Block entries", data.block_new_entries ? "ON" : "OFF"],
    ["Exit on kill", data.cancel_open_orders_and_exit_positions ? "ON" : "OFF"],
  ];
  document.getElementById("stats").innerHTML = stats.map(([label, value]) =>
    `<div class="card"><div class="label">${label}</div><div class="value ${value === "ON" ? "on" : value === "OFF" ? "off" : ""}">${value}</div></div>`
  ).join("");
  document.getElementById("orders").innerHTML = data.recent_orders.map(o =>
    `<tr><td>${o.created_at_utc || ""}</td><td>${o.label || o.outcome_id}</td><td>${o.side}</td><td>${o.status}</td><td>${num(o.limit_price)}</td><td>${num(o.fill_price)}</td><td>${money(o.fill_size_usd || o.requested_size_usd)}</td><td>${o.reason || o.error || ""}</td></tr>`
  ).join("");
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""


def _build_handler(db_path: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            return

        def _send_json(self, payload: dict | list, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802 - stdlib signature
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            try:
                if path == "/" or path == "/index.html":
                    self._send_html(INDEX_HTML)
                    return
                if path == "/live":
                    self._send_html(LIVE_HTML)
                    return
                db = connect(db_path)
                try:
                    if path == "/api/stats":
                        self._send_json(dashboard_stats(db))
                        return
                    if path == "/api/forecast-vs-actual":
                        requested = query.get("date", [None])[0]
                        target = _resolve_target_date(db, requested)
                        forecast = forecast_series(db, target)
                        actual_min, actual_max, current = observation_series(db, target)
                        self._send_json(
                            {
                                "target_date": target,
                                "available_dates": available_forecast_dates(db),
                                "forecast": forecast,
                                "forecast_low": forecast_series(db, target, value_kind="min"),
                                "actual_min": actual_min,
                                "actual_max": actual_max,
                                "current_temp": current,
                            }
                        )
                        return
                    if path == "/api/forecast-panels":
                        token_side = query.get("side", ["YES"])[0]
                        self._send_json(forecast_panels(db, token_side=token_side))
                        return
                    if path == "/api/pnl":
                        self._send_json(pnl_series(db))
                        return
                    if path == "/api/paper-trades":
                        view = query.get("view", ["open"])[0]
                        self._send_json(paper_trade_rows(db, view))
                        return
                    if path == "/api/live/stats":
                        self._send_json(live_dashboard_stats(db))
                        return
                    self.send_error(404, "not found")
                finally:
                    db.close()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return
            except Exception as exc:  # pragma: no cover - defensive
                self.send_error(500, f"server error: {exc}")

    return Handler


def serve(db_path: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    handler = _build_handler(db_path)
    server = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    print(f"whenitrains dashboard serving at {url} (db={db_path})")
    print("press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()
