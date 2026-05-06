from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .hko import HKT
from .storage import connect, dashboard_stats


def _to_unix(iso_ts: str | None) -> int | None:
    if not iso_ts:
        return None
    try:
        return int(datetime.fromisoformat(iso_ts).timestamp())
    except ValueError:
        return None


def forecast_series(
    db: sqlite3.Connection, target_date_hkt: str, exact_raw: bool = False
) -> list[dict]:
    rows = db.execute(
        """
        select update_time, forecast_max_c, raw_forecast
        from hko_forecasts
        where source_type = 'ocf_station'
          and forecast_date_hkt = ?
          and forecast_max_c is not null
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
        value = float(row["forecast_max_c"])
        if exact_raw:
            value = _raw_forecast_max(row["raw_forecast"]) or value
        seen[ts] = value
    return [{"time": ts, "value": value} for ts, value in sorted(seen.items())]


def _raw_forecast_max(raw_forecast: str | None) -> float | None:
    if not raw_forecast:
        return None
    try:
        raw = json.loads(raw_forecast)
        value = raw.get("ForecastMaximumTemperature")
        return None if value is None else float(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def observation_series(
    db: sqlite3.Connection, target_date_hkt: str
) -> tuple[list[dict], list[dict]]:
    rows = db.execute(
        """
        select observed_at_hkt, since_midnight_max_c, temperature_c
        from hko_current_observations
        where substr(observed_at_hkt, 1, 10) = ?
          and (since_midnight_max_c is not null or temperature_c is not null)
        order by observed_at_hkt asc, id asc
        """,
        (target_date_hkt,),
    ).fetchall()
    max_seen: dict[int, float] = {}
    cur_seen: dict[int, float] = {}
    for row in rows:
        ts = _to_unix(row["observed_at_hkt"])
        if ts is None:
            continue
        if row["since_midnight_max_c"] is not None:
            value = float(row["since_midnight_max_c"])
            if ts not in max_seen or value > max_seen[ts]:
                max_seen[ts] = value
        if row["temperature_c"] is not None:
            cur_seen[ts] = float(row["temperature_c"])
    actual_max = [{"time": t, "value": v} for t, v in sorted(max_seen.items())]
    current = [{"time": t, "value": v} for t, v in sorted(cur_seen.items())]
    return actual_max, current


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
        select observed_at_hkt, temperature_c, since_midnight_max_c
        from hko_current_observations
        where substr(observed_at_hkt, 1, 10) = ?
          and (temperature_c is not null or since_midnight_max_c is not null)
        order by observed_at_hkt asc, id asc
        """,
        (target_date_hkt,),
    ).fetchall()
    hourly_current: dict[int, float] = {}
    hourly_max: dict[int, float] = {}
    for row in rows:
        try:
            observed = datetime.fromisoformat(row["observed_at_hkt"]).astimezone(HKT)
        except (TypeError, ValueError):
            continue
        hour = observed.replace(minute=0, second=0, microsecond=0)
        ts = int(hour.timestamp())
        if row["temperature_c"] is not None:
            hourly_current[ts] = float(row["temperature_c"])
        elif row["since_midnight_max_c"] is not None and ts not in hourly_current:
            hourly_max[ts] = float(row["since_midnight_max_c"])
    hourly = hourly_max | hourly_current
    return [{"time": ts, "value": value} for ts, value in sorted(hourly.items())]


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
    params: tuple[str, ...] | tuple[str, int] = (
        (target_date_hkt,) if limit is None else (target_date_hkt, limit)
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
    rows = db.execute(
        """
        select created_at_utc, side, simulated_fill_price, simulated_fill_size_usd, status
        from paper_orders
        where outcome_id = ?
          and status = 'filled'
          and simulated_fill_price is not null
          and created_at_utc is not null
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
            }
        )
    return markers


def forecast_panel(
    db: sqlite3.Connection, target_date: date, lead_days: int, token_side: str = "YES"
) -> dict:
    target_text = target_date.isoformat()
    actual_max, current = observation_series(db, target_text)
    hourly_forecast = hourly_forecast_series(db, target_text) if lead_days == 0 else []
    hourly_actual = hourly_actual_series(db, target_text) if lead_days == 0 else []
    top_tokens = top_token_price_series(
        db, target_text, token_side, limit=None, include_trade_tokens=True
    )
    return {
        "lead_days": lead_days,
        "target_date": target_text,
        "forecast": forecast_series(db, target_text, exact_raw=lead_days == 0),
        "actual_max": actual_max if lead_days == 0 else [],
        "current_temp": current if lead_days == 0 else [],
        "hourly_forecast": hourly_forecast,
        "hourly_actual": hourly_actual,
        "hourly_error": hourly_error_series(hourly_forecast, hourly_actual),
        "token_side": token_side.upper() if token_side.upper() in {"YES", "NO"} else "YES",
        "top_tokens": top_tokens,
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

    orders = db.execute(
        """
        select created_at_utc, outcome_id, side, simulated_fill_price,
               simulated_fill_size_usd, status
        from paper_orders
        where status = 'filled'
          and simulated_fill_price is not null
          and simulated_fill_size_usd is not null
          and created_at_utc is not null
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
    <h1>whenitrains · HK high-temp paper desk</h1>
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

  <section class="chart-section">
    <h2>
      <span><span class="lead-label">D+0</span><span id="d0-date"></span></span>
      <span class="legend">
        <button type="button" data-series-key="forecastHigh"><i style="background:#f0b400"></i>OCF forecast high</button>
        <button type="button" data-series-key="hourlyForecast"><i style="background:#c084fc"></i>Hourly forecast</button>
        <button type="button" data-series-key="hourlyActual"><i style="background:#f97316"></i>Hourly actual</button>
        <button type="button" data-series-key="hourlyError"><i style="background:#94a3b8"></i>Actual - forecast</button>
        <button type="button" data-series-key="actualMax"><i style="background:#26a69a"></i>Since-midnight max</button>
        <button type="button" data-series-key="currentTemp"><i style="background:#5b9bd5"></i>Current temperature</button>
        <span id="d0-legend"></span>
      </span>
    </h2>
    <div id="d0-chart" class="chart"></div>
  </section>

  <div id="chart-tooltip" class="chart-tooltip"></div>

  <section class="chart-section">
    <h2>
      <span><span class="lead-label">D+1</span><span id="d1-date"></span></span>
      <span class="legend" id="d1-legend"></span>
    </h2>
    <div id="d1-chart" class="chart"></div>
  </section>

  <section class="chart-section">
    <h2>
      <span><span class="lead-label">D+2</span><span id="d2-date"></span></span>
      <span class="legend" id="d2-legend"></span>
    </h2>
    <div id="d2-chart" class="chart"></div>
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
const d0ForecastSeries = charts[0].chart.addLineSeries({
  color: "#f0b400", lineWidth: 2, lineType: LightweightCharts.LineType.WithSteps,
  priceFormat: { type: "price", precision: 1, minMove: 0.1 }, priceScaleId: "right",
});
const d0HourlyForecastSeries = charts[0].chart.addLineSeries({
  color: "#c084fc", lineWidth: 1, lineType: LightweightCharts.LineType.WithSteps,
  priceFormat: { type: "price", precision: 1, minMove: 0.1 }, priceScaleId: "right",
  priceLineVisible: false,
});
const d0HourlyActualSeries = charts[0].chart.addLineSeries({
  color: "#f97316", lineWidth: 2,
  priceFormat: { type: "price", precision: 1, minMove: 0.1 }, priceScaleId: "right",
  priceLineVisible: false,
});
const d0HourlyErrorSeries = charts[0].chart.addHistogramSeries({
  color: "#94a3b8",
  priceFormat: { type: "price", precision: 1, minMove: 0.1 }, priceScaleId: "right",
  priceLineVisible: false,
  base: 0,
});
const d0ActualMaxSeries = charts[0].chart.addLineSeries({
  color: "#26a69a", lineWidth: 2,
  priceFormat: { type: "price", precision: 1, minMove: 0.1 }, priceScaleId: "right",
});
const d0CurrentTempSeries = charts[0].chart.addLineSeries({
  color: "#5b9bd5", lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted,
  priceFormat: { type: "price", precision: 1, minMove: 0.1 }, priceScaleId: "right",
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
let realizedData = [];
let unrealizedData = [];
let totalData = [];
let tokenSide = "YES";
const fittedCharts = new Set();
const seriesVisibility = {
  forecastHigh: true,
  hourlyForecast: true,
  hourlyActual: true,
  hourlyError: true,
  actualMax: true,
  currentTemp: true,
};
const d0SeriesByKey = {
  forecastHigh: d0ForecastSeries,
  hourlyForecast: d0HourlyForecastSeries,
  hourlyActual: d0HourlyActualSeries,
  hourlyError: d0HourlyErrorSeries,
  actualMax: d0ActualMaxSeries,
  currentTemp: d0CurrentTempSeries,
};

window.addEventListener("resize", () => {
  renderAllTradeBubbles();
});

function fmtMoney(v) {
  if (v == null || isNaN(v)) return "n/a";
  const sign = v < 0 ? "-" : "";
  return sign + "$" + Math.abs(v).toFixed(2);
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
    { label: "Forecast high (°C)",  value: f.forecast_max_c != null ? f.forecast_max_c.toFixed(1) : "n/a" },
    { label: "Forecast updated",    value: f.update_time ? f.update_time.slice(11,16) + " HKT" : "n/a" },
    { label: "Since-midnight max",  value: o.since_midnight_max_c != null ? o.since_midnight_max_c.toFixed(1) + "°C" : (o.temperature_c != null ? o.temperature_c.toFixed(1) + "°C (cur)" : "n/a") },
    { label: "Observed at",         value: o.observed_at_hkt ? o.observed_at_hkt.slice(11,16) + " HKT" : "n/a" },
    { label: "Open positions",      value: String(stats.open_positions ?? 0) },
    { label: "Realized PnL",        value: fmtMoney(stats.realized_pnl), cls: classForMoney(stats.realized_pnl) },
    { label: "Unrealized PnL",      value: fmtMoney(stats.executable_unrealized_pnl), cls: classForMoney(stats.executable_unrealized_pnl) },
    { label: "Total profit est.",   value: fmtMoney(stats.total_profit), cls: classForMoney(stats.total_profit) },
    { label: "Worst-case open loss", value: fmtMoney(-Math.abs(stats.worst_case_open_loss || 0)), cls: stats.worst_case_open_loss ? "neg" : "" },
    { label: "Buys filled / missed", value: stats.counts.buy_filled + " / " + stats.counts.buy_missed },
    { label: "Sells filled / missed", value: stats.counts.sell_filled + " / " + stats.counts.sell_missed },
    { label: "Markets / outcomes",  value: stats.counts.markets + " / " + stats.counts.outcomes },
    { label: "Orderbook snapshots", value: String(stats.counts.orderbooks) },
  ];
  document.getElementById("stats").innerHTML = cells.map(c =>
    `<div class="stat"><div class="label">${c.label}</div><div class="value ${c.cls||""}">${c.value}</div></div>`
  ).join("");
}

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(url + " " + res.status);
  return res.json();
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

function applySeriesVisibility() {
  Object.entries(d0SeriesByKey).forEach(([key, series]) => {
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
  const container = document.getElementById(`d${lead}-chart`);
  if (!container) return;
  container.querySelectorAll(".trade-bubble").forEach(el => el.remove());
  const chart = charts[lead].chart;
  const width = container.clientWidth;
  const height = container.clientHeight;
  for (const descriptor of charts[lead].series) {
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
      bubble.title = `${descriptor.name} ${marker.text} @ ${marker.price.toFixed(3)}${marker.size_usd != null ? " · $" + marker.size_usd.toFixed(2) : ""}`;
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
    ...state.trades.map(t =>
      `<div class="row"><span class="name"><i style="background:${t.color}"></i>${t.name}</span><span>${t.text} @ ${t.price.toFixed(3)}${t.size_usd != null ? " · $" + t.size_usd.toFixed(2) : ""}</span></div>`
    ),
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
  { name: "OCF forecast high", color: "#f0b400", kind: "temp", data: visibleData("forecastHigh", d0ForecastData) },
  { name: "Hourly forecast", color: "#c084fc", kind: "temp", data: visibleData("hourlyForecast", d0HourlyForecastData) },
  { name: "Hourly actual", color: "#f97316", kind: "temp", data: visibleData("hourlyActual", d0HourlyActualData) },
  { name: "Actual - forecast", color: "#94a3b8", kind: "delta", data: visibleData("hourlyError", d0HourlyErrorData) },
  { name: "Since-midnight max", color: "#26a69a", kind: "temp", data: visibleData("actualMax", d0ActualMaxData) },
  { name: "Current temperature", color: "#5b9bd5", kind: "temp", data: visibleData("currentTemp", d0CurrentTempData) },
  ...charts[0].series.map(s => ({ name: s.name, color: s.color, kind: "odds", data: visibleData(s.key, s.data), markers: isSeriesVisible(s.key) ? s.markers : [] })),
]);
attachTooltip(charts[1].chart, "d1-chart", () =>
  charts[1].series.map(s => ({ name: s.name, color: s.color, kind: s.kind, data: visibleData(s.key, s.data), markers: isSeriesVisible(s.key) ? s.markers : [] }))
);
attachTooltip(charts[2].chart, "d2-chart", () =>
  charts[2].series.map(s => ({ name: s.name, color: s.color, kind: s.kind, data: visibleData(s.key, s.data), markers: isSeriesVisible(s.key) ? s.markers : [] }))
);
attachTooltip(pnlChart, "pnl-chart", () => [
  { name: "Realized", color: "#26a69a", kind: "money", data: realizedData },
  { name: "Unrealized", color: "#5b9bd5", kind: "money", data: unrealizedData },
  { name: "Total", color: "#f0b400", kind: "money", data: totalData },
]);
Object.values(charts).forEach(c => {
  c.chart.timeScale().subscribeVisibleTimeRangeChange(renderAllTradeBubbles);
});

function renderLeadPanel(panel) {
  const lead = panel.lead_days;
  document.getElementById(`d${lead}-date`).textContent = panel.target_date;
  if (lead === 0) {
    resetLeadChart(0);
    d0ForecastData = panel.forecast;
    d0HourlyForecastData = panel.hourly_forecast || [];
    d0HourlyActualData = panel.hourly_actual || [];
    d0HourlyErrorData = panel.hourly_error || [];
    d0ActualMaxData = panel.actual_max;
    d0CurrentTempData = panel.current_temp;
    d0ForecastSeries.setData(d0ForecastData);
    d0HourlyForecastSeries.setData(d0HourlyForecastData);
    d0HourlyActualSeries.setData(d0HourlyActualData);
    d0HourlyErrorSeries.setData(d0HourlyErrorData);
    d0ActualMaxSeries.setData(d0ActualMaxData);
    d0CurrentTempSeries.setData(d0CurrentTempData);
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
    applySeriesVisibility();
    if (panel.forecast.length || d0HourlyForecastData.length || d0HourlyActualData.length || panel.actual_max.length || panel.current_temp.length || panel.top_tokens.some(s => s.points.length)) {
      fitChartOnce("d0", charts[0].chart);
    }
    return;
  }

  resetLeadChart(lead);
  const forecast = charts[lead].chart.addLineSeries({
    color: "#f0b400",
    lineWidth: 2,
    lineType: LightweightCharts.LineType.WithSteps,
    priceFormat: { type: "price", precision: 1, minMove: 0.1 },
    priceScaleId: "right",
  });
  forecast.setData(panel.forecast);
  charts[lead].series.push({ series: forecast, name: "OCF forecast high", color: "#f0b400", kind: "temp", data: panel.forecast });

  const legend = [
    `<span><i style="background:#f0b400"></i>OCF forecast high (right °C)</span>`
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
  applySeriesVisibility();
  if (panel.forecast.length || panel.top_tokens.some(s => s.points.length)) {
    fitChartOnce(`d${lead}`, charts[lead].chart);
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
}

document.getElementById("refresh-btn").addEventListener("click", () => {
  loadAll();
});
document.getElementById("token-side").addEventListener("change", (event) => {
  tokenSide = event.target.value === "NO" ? "NO" : "YES";
  fittedCharts.clear();
  loadAll();
});
bindSeriesToggleButtons();
Object.values(charts).forEach((chartState, idx) => {
  installModifierWheelZoom(`d${idx}-chart`, chartState.chart);
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
                db = connect(db_path)
                try:
                    if path == "/api/stats":
                        self._send_json(dashboard_stats(db))
                        return
                    if path == "/api/forecast-vs-actual":
                        requested = query.get("date", [None])[0]
                        target = _resolve_target_date(db, requested)
                        forecast = forecast_series(db, target)
                        actual_max, current = observation_series(db, target)
                        self._send_json(
                            {
                                "target_date": target,
                                "available_dates": available_forecast_dates(db),
                                "forecast": forecast,
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
