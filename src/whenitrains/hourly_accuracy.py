from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime
from io import StringIO
from math import ceil, sqrt

from .hko import HKT


@dataclass(frozen=True)
class HourlyAccuracyRow:
    issue_time_hkt: datetime
    target_hour_hkt: datetime
    lead_hours: int
    forecast_temp_c: float
    actual_temp_c: float

    @property
    def error_c(self) -> float:
        return self.actual_temp_c - self.forecast_temp_c


@dataclass(frozen=True)
class HourlyAccuracySummary:
    lead_hours: int
    n: int
    mean_error_c: float
    mae_c: float
    rmse_c: float
    exact_c: float
    within_1c: float


def build_hourly_accuracy_report(db) -> tuple[list[HourlyAccuracyRow], list[HourlyAccuracySummary]]:
    actuals = _actual_temperatures_by_hour(db)
    rows = []
    seen: set[tuple[str, str]] = set()
    for sample in db.execute(
        """
        select raw_daily_forecast, hourly_temperatures_json
        from ocf_forecast_samples
        where hourly_temperatures_json is not null
        order by fetched_at_utc
        """
    ):
        issue_time = _issue_time(sample["raw_daily_forecast"])
        if issue_time is None:
            continue
        for item in json.loads(sample["hourly_temperatures_json"] or "[]"):
            target_hour = datetime.fromisoformat(item["forecast_hour_hkt"]).astimezone(HKT)
            key = (issue_time.isoformat(), target_hour.isoformat())
            if key in seen:
                continue
            seen.add(key)
            actual_temp = actuals.get(target_hour.replace(minute=0, second=0, microsecond=0))
            forecast_temp = _as_float(item.get("temperature_c"))
            if actual_temp is None or forecast_temp is None:
                continue
            lead_hours = int(ceil((target_hour - issue_time).total_seconds() / 3600))
            if lead_hours < 0:
                continue
            rows.append(
                HourlyAccuracyRow(
                    issue_time_hkt=issue_time,
                    target_hour_hkt=target_hour,
                    lead_hours=lead_hours,
                    forecast_temp_c=forecast_temp,
                    actual_temp_c=actual_temp,
                )
            )
    return rows, summarize_hourly_accuracy(rows)


def summarize_hourly_accuracy(rows: list[HourlyAccuracyRow]) -> list[HourlyAccuracySummary]:
    summaries = []
    for lead in sorted({row.lead_hours for row in rows}):
        lead_rows = [row for row in rows if row.lead_hours == lead]
        errors = [row.error_c for row in lead_rows]
        n = len(errors)
        summaries.append(
            HourlyAccuracySummary(
                lead_hours=lead,
                n=n,
                mean_error_c=sum(errors) / n,
                mae_c=sum(abs(error) for error in errors) / n,
                rmse_c=sqrt(sum(error * error for error in errors) / n),
                exact_c=sum(1 for error in errors if error == 0) / n,
                within_1c=sum(1 for error in errors if abs(error) <= 1.0) / n,
            )
        )
    return summaries


def render_hourly_accuracy_report(
    rows: list[HourlyAccuracyRow], summaries: list[HourlyAccuracySummary]
) -> str:
    out = StringIO()
    writer = csv.writer(out)
    writer.writerow(
        ["lead_hours", "n", "mean_error_c", "mae_c", "rmse_c", "exact_c", "within_1c"]
    )
    for summary in summaries:
        writer.writerow(
            [
                summary.lead_hours,
                summary.n,
                _fmt_float(summary.mean_error_c),
                _fmt_float(summary.mae_c),
                _fmt_float(summary.rmse_c),
                _fmt_pct(summary.exact_c),
                _fmt_pct(summary.within_1c),
            ]
        )
    writer.writerow([])
    writer.writerow(
        [
            "issue_time_hkt",
            "target_hour_hkt",
            "lead_hours",
            "forecast_temp_c",
            "actual_temp_c",
            "error_c",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.issue_time_hkt.isoformat(),
                row.target_hour_hkt.isoformat(),
                row.lead_hours,
                _fmt_float(row.forecast_temp_c),
                _fmt_float(row.actual_temp_c),
                _fmt_float(row.error_c),
            ]
        )
    return out.getvalue().strip()


def _actual_temperatures_by_hour(db) -> dict[datetime, float]:
    actuals: dict[datetime, float] = {}
    for row in db.execute(
        """
        select observed_at_hkt, temperature_c
        from hko_current_observations
        where temperature_c is not null
        order by observed_at_hkt
        """
    ):
        observed_at = datetime.fromisoformat(row["observed_at_hkt"]).astimezone(HKT)
        hour = observed_at.replace(minute=0, second=0, microsecond=0)
        if hour not in actuals:
            actuals[hour] = float(row["temperature_c"])
    return actuals


def _issue_time(raw_daily_forecast: str) -> datetime | None:
    try:
        raw = json.loads(raw_daily_forecast or "{}")
    except json.JSONDecodeError:
        return None
    value = str(raw.get("LastModified") or "")
    if len(value) != 14 or not value.isdigit():
        return None
    return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=HKT)


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_float(value: float) -> str:
    return f"{value:.3f}"


def _fmt_pct(value: float) -> str:
    return f"{value:.1%}"
