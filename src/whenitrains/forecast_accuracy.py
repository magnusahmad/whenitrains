from __future__ import annotations

import csv
import html
import json
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from urllib.parse import quote

from .hko import HKT, fetch_text


FND_RSS_URL = "https://rss.weather.gov.hk/rss/SeveralDaysWeatherForecast_v2.xml"
HISTORICAL_LIST_URL = "https://api.data.gov.hk/v1/historical-archive/list-file-versions"
HISTORICAL_FILE_URL = "https://api.data.gov.hk/v1/historical-archive/get-file"
HKO_DAILY_MAX_URL = (
    "https://data.weather.gov.hk/weatherAPI/opendata/opendata.php"
    "?dataType=CLMMAXT&station=HKO&rformat=csv"
)


@dataclass(frozen=True)
class HistoricalForecast:
    issued_at_hkt: datetime
    target_date: date
    forecast_max_c: float
    source_timestamp: str


@dataclass(frozen=True)
class ForecastAccuracyRow:
    lead_days: int
    issued_at_hkt: datetime
    target_date: date
    forecast_max_c: float
    actual_max_c: float

    @property
    def error_c(self) -> float:
        return self.actual_max_c - self.forecast_max_c


@dataclass(frozen=True)
class ForecastAccuracySummary:
    lead_days: int
    sample_count: int
    mean_error_c: float
    mae_c: float
    rmse_c: float
    exact_integer_bucket_rate: float
    within_0_5c_rate: float
    within_1c_rate: float
    within_2c_rate: float


def build_forecast_accuracy_report(
    start: date,
    end: date,
    cache_dir: Path,
    lead_days: tuple[int, ...] = (0, 1, 2),
) -> tuple[list[ForecastAccuracyRow], list[ForecastAccuracySummary]]:
    forecasts = load_historical_fnd_forecasts(start - timedelta(days=max(lead_days)), end, cache_dir)
    actuals = load_hko_daily_max_actuals(cache_dir)
    rows = match_forecasts_to_actuals(forecasts, actuals, start, end, lead_days)
    summaries = summarize_accuracy(rows, lead_days)
    return rows, summaries


def load_historical_fnd_forecasts(start: date, end: date, cache_dir: Path) -> list[HistoricalForecast]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    timestamps = _analysis_timestamps(historical_timestamps(start, end, cache_dir))
    forecasts: list[HistoricalForecast] = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(historical_file, timestamp, cache_dir): timestamp
            for timestamp in timestamps
        }
        for future in as_completed(futures):
            timestamp = futures[future]
            try:
                text = future.result()
            except Exception as exc:
                print(
                    f"historical forecast warning {timestamp}: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                continue
            forecasts.extend(parse_fnd_rss_forecasts(text, timestamp))
    return forecasts


def _analysis_timestamps(timestamps: list[str]) -> list[str]:
    selected = set(_latest_timestamp_per_day(timestamps))
    selected.update(_latest_pre_evening_timestamp_per_day(timestamps))
    return sorted(selected)


def _latest_timestamp_per_day(timestamps: list[str]) -> list[str]:
    latest: dict[str, str] = {}
    for timestamp in timestamps:
        day = timestamp.split("-", 1)[0]
        if day not in latest or timestamp > latest[day]:
            latest[day] = timestamp
    return [latest[day] for day in sorted(latest)]


def _latest_pre_evening_timestamp_per_day(timestamps: list[str]) -> list[str]:
    latest: dict[str, str] = {}
    for timestamp in timestamps:
        day, clock = timestamp.split("-", 1)
        if clock >= "1800":
            continue
        if day not in latest or timestamp > latest[day]:
            latest[day] = timestamp
    return [latest[day] for day in sorted(latest)]


def historical_timestamps(start: date, end: date, cache_dir: Path) -> list[str]:
    cache_path = cache_dir / f"fnd-rss-timestamps-{start:%Y%m%d}-{end:%Y%m%d}.json"
    if cache_path.exists():
        payload = json.loads(cache_path.read_text())
    else:
        url = (
            f"{HISTORICAL_LIST_URL}?url={quote(FND_RSS_URL, safe='')}"
            f"&start={start:%Y%m%d}&end={end:%Y%m%d}"
        )
        payload = json.loads(_fetch_text_with_retries(url))
        cache_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return list(payload.get("timestamps") or [])


def historical_file(timestamp: str, cache_dir: Path) -> str:
    cache_path = cache_dir / "fnd-rss" / f"{timestamp}.xml"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if cache_path.exists():
        return cache_path.read_text()
    url = f"{HISTORICAL_FILE_URL}?url={quote(FND_RSS_URL, safe='')}&time={timestamp}"
    text = _fetch_text_with_retries(url)
    cache_path.write_text(text)
    return text


def load_hko_daily_max_actuals(cache_dir: Path) -> dict[date, float]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "hko-daily-max.csv"
    if cache_path.exists():
        text = cache_path.read_text()
    else:
        text = _fetch_text_with_retries(HKO_DAILY_MAX_URL)
        cache_path.write_text(text)
    return parse_hko_daily_max_csv(text)


def parse_fnd_rss_forecasts(text: str, source_timestamp: str) -> list[HistoricalForecast]:
    title_match = re.search(
        r"Bulletin updated at\s+(\d{2}):(\d{2})\s+HKT\s+(\d{1,2})/([A-Za-z]{3})/(\d{4})",
        text,
        re.IGNORECASE,
    )
    if not title_match:
        pub_match = re.search(r"<pubDate>([^<]+)</pubDate>", text, re.IGNORECASE)
        if not pub_match:
            return []
        issued_at = datetime.strptime(pub_match.group(1), "%a, %d %b %Y %H:%M:%S %Z")
        issued_at = issued_at.replace(tzinfo=timezone.utc).astimezone(HKT)
    else:
        issued_at = datetime(
            int(title_match.group(5)),
            datetime.strptime(title_match.group(4).title(), "%b").month,
            int(title_match.group(3)),
            int(title_match.group(1)),
            int(title_match.group(2)),
            tzinfo=HKT,
        )

    desc_match = re.search(r"<description><!\[CDATA\[(.*?)\]\]></description>", text, re.DOTALL)
    if not desc_match:
        return []
    description = _plain_text(desc_match.group(1))
    year = issued_at.year
    forecasts: list[HistoricalForecast] = []
    pattern = re.compile(
        r"Date/Month:\s*(\d{2})/(\d{2})\s*\([^)]+\).*?"
        r"Temp range:\s*(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)\s*C",
        re.IGNORECASE | re.DOTALL,
    )
    previous_month = issued_at.month
    current_year = year
    for match in pattern.finditer(description):
        day = int(match.group(1))
        month = int(match.group(2))
        if month < previous_month - 6:
            current_year += 1
        previous_month = month
        forecasts.append(
            HistoricalForecast(
                issued_at_hkt=issued_at,
                target_date=date(current_year, month, day),
                forecast_max_c=float(match.group(4)),
                source_timestamp=source_timestamp,
            )
        )
    return forecasts


def parse_hko_daily_max_csv(text: str) -> dict[date, float]:
    cleaned = text.lstrip("\ufeff")
    lines = cleaned.splitlines()
    header_index = next(
        i for i, line in enumerate(lines) if "Year" in line and "Month" in line and "Day" in line
    )
    reader = csv.DictReader(StringIO("\n".join(lines[header_index:])))
    actuals: dict[date, float] = {}
    for row in reader:
        try:
            year = int(row["年/Year"])
            month = int(row["月/Month"])
            day = int(row["日/Day"])
            value = float(row["數值/Value"])
        except (KeyError, TypeError, ValueError):
            continue
        actuals[date(year, month, day)] = value
    return actuals


def match_forecasts_to_actuals(
    forecasts: list[HistoricalForecast],
    actuals: dict[date, float],
    start: date,
    end: date,
    lead_days: tuple[int, ...],
) -> list[ForecastAccuracyRow]:
    latest_by_target_and_lead: dict[tuple[date, int], HistoricalForecast] = {}
    for forecast in forecasts:
        if not (start <= forecast.target_date <= end):
            continue
        lead = (forecast.target_date - forecast.issued_at_hkt.date()).days
        if lead not in lead_days:
            continue
        key = (forecast.target_date, lead)
        existing = latest_by_target_and_lead.get(key)
        if existing is None or forecast.issued_at_hkt > existing.issued_at_hkt:
            latest_by_target_and_lead[key] = forecast

    rows: list[ForecastAccuracyRow] = []
    for (target, lead), forecast in sorted(latest_by_target_and_lead.items()):
        actual = actuals.get(target)
        if actual is None:
            continue
        rows.append(
            ForecastAccuracyRow(
                lead_days=lead,
                issued_at_hkt=forecast.issued_at_hkt,
                target_date=target,
                forecast_max_c=forecast.forecast_max_c,
                actual_max_c=actual,
            )
        )
    return rows


def summarize_accuracy(
    rows: list[ForecastAccuracyRow], lead_days: tuple[int, ...]
) -> list[ForecastAccuracySummary]:
    summaries: list[ForecastAccuracySummary] = []
    for lead in lead_days:
        subset = [row for row in rows if row.lead_days == lead]
        if not subset:
            summaries.append(ForecastAccuracySummary(lead, 0, math.nan, math.nan, math.nan, math.nan, math.nan, math.nan, math.nan))
            continue
        errors = [row.error_c for row in subset]
        abs_errors = [abs(error) for error in errors]
        summaries.append(
            ForecastAccuracySummary(
                lead_days=lead,
                sample_count=len(subset),
                mean_error_c=sum(errors) / len(errors),
                mae_c=sum(abs_errors) / len(abs_errors),
                rmse_c=math.sqrt(sum(error * error for error in errors) / len(errors)),
                exact_integer_bucket_rate=_rate(
                    int(row.actual_max_c) == int(row.forecast_max_c) for row in subset
                ),
                within_0_5c_rate=_rate(error <= 0.5 for error in abs_errors),
                within_1c_rate=_rate(error <= 1.0 for error in abs_errors),
                within_2c_rate=_rate(error <= 2.0 for error in abs_errors),
            )
        )
    return summaries


def render_accuracy_report(
    rows: list[ForecastAccuracyRow],
    summaries: list[ForecastAccuracySummary],
    start: date,
    end: date,
) -> str:
    lines = [
        f"HKO 9-day max-temp forecast accuracy, {start.isoformat()} to {end.isoformat()}",
        "Source: data.gov.hk historical RSS 9-day forecasts vs HKO CLMMAXT daily max actuals.",
        "",
        "lead_days,n,mean_error_c,mae_c,rmse_c,bucket_hit,within_0.5c,within_1c,within_2c",
    ]
    for item in summaries:
        lines.append(
            ",".join(
                [
                    str(item.lead_days),
                    str(item.sample_count),
                    _fmt_float(item.mean_error_c),
                    _fmt_float(item.mae_c),
                    _fmt_float(item.rmse_c),
                    _fmt_pct(item.exact_integer_bucket_rate),
                    _fmt_pct(item.within_0_5c_rate),
                    _fmt_pct(item.within_1c_rate),
                    _fmt_pct(item.within_2c_rate),
                ]
            )
        )
    if rows:
        lines.extend(["", "Worst 10 absolute errors:", "lead_days,target_date,issued_at_hkt,forecast_max_c,actual_max_c,error_c"])
        worst = sorted(rows, key=lambda row: abs(row.error_c), reverse=True)[:10]
        for row in worst:
            lines.append(
                f"{row.lead_days},{row.target_date.isoformat()},{row.issued_at_hkt.isoformat()},"
                f"{row.forecast_max_c:g},{row.actual_max_c:g},{row.error_c:.1f}"
            )
    return "\n".join(lines)


def _plain_text(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<p\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", html.unescape(text)).strip()


def _rate(values) -> float:
    items = list(values)
    return sum(1 for item in items if item) / len(items)


def _fmt_float(value: float) -> str:
    return "" if math.isnan(value) else f"{value:.3f}"


def _fmt_pct(value: float) -> str:
    return "" if math.isnan(value) else f"{value:.1%}"


def _fetch_text_with_retries(url: str, attempts: int = 8) -> str:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return fetch_text(url)
        except Exception as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                break
            time.sleep(2.0 * (attempt + 1))
    assert last_error is not None
    raise last_error
