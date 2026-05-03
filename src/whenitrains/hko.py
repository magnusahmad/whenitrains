from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
from io import StringIO
from typing import Any
from urllib.request import Request, urlopen


HKT = timezone(timedelta(hours=8))
SINCE_MIDNIGHT_URL = (
    "https://data.weather.gov.hk/weatherAPI/hko_data/csdi/dataset/"
    "latest_since_midnight_maxmin_csdi_4.csv"
)
FND_URL = "https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=fnd&lang=en"
FLW_URL = "https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=flw&lang=en"


@dataclass(frozen=True)
class HkoObservation:
    observed_at_hkt: datetime
    station: str
    since_midnight_max_c: float
    since_midnight_min_c: float
    raw: dict[str, str]


@dataclass(frozen=True)
class HkoForecast:
    source_type: str
    forecast_date_hkt: date | None
    forecast_min_c: int | None
    forecast_max_c: int | None
    weather_text: str = ""
    wind_text: str = ""
    psr: str = ""
    update_time: str | None = None
    parse_warning: bool = False
    raw: dict[str, Any] | None = None


def fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "whenitrains/0.1"})
    with urlopen(request, timeout=15) as response:
        return response.read().decode("utf-8-sig")


def fetch_json(url: str) -> dict[str, Any]:
    return json.loads(fetch_text(url))


def parse_since_midnight_csv(text: str) -> HkoObservation:
    reader = csv.DictReader(StringIO(text.lstrip("\ufeff")))
    for row in reader:
        if row.get("Automatic Weather Station") == "HK Observatory":
            year = int(row["Date time (Year)"])
            month = int(row["Date time (Month)"])
            day = int(row["Date time (Day)"])
            hour = int(row["Date time (Hour)"])
            minute = int(row["Date time (Minute)"])
            observed_at = datetime(year, month, day, hour, minute, tzinfo=HKT)
            return HkoObservation(
                observed_at_hkt=observed_at,
                station=row["Automatic Weather Station"],
                since_midnight_max_c=float(
                    row["Maximum Air Temperature Since Midnight(degree Celsius)"]
                ),
                since_midnight_min_c=float(
                    row["Minimum Air Temperature Since Midnight(degree Celsius)"]
                ),
                raw=row,
            )
    raise ValueError("HK Observatory row not found in since-midnight CSV")


def parse_fnd_forecasts(payload: dict[str, Any]) -> list[HkoForecast]:
    rows: list[HkoForecast] = []
    for item in payload.get("weatherForecast", []):
        forecast_date = datetime.strptime(item["forecastDate"], "%Y%m%d").date()
        rows.append(
            HkoForecast(
                source_type="fnd",
                forecast_date_hkt=forecast_date,
                forecast_min_c=item.get("forecastMintemp", {}).get("value"),
                forecast_max_c=item.get("forecastMaxtemp", {}).get("value"),
                weather_text=item.get("forecastWeather", ""),
                wind_text=item.get("forecastWind", ""),
                psr=item.get("PSR", ""),
                update_time=payload.get("updateTime"),
                raw=item,
            )
        )
    return rows


def parse_flw_forecast(payload: dict[str, Any]) -> HkoForecast:
    desc = payload.get("forecastDesc", "")
    match = re.search(
        r"between\s+(-?\d+)\s+and\s+(-?\d+)\s+degrees", desc, flags=re.IGNORECASE
    )
    if not match:
        return HkoForecast(
            source_type="flw",
            forecast_date_hkt=None,
            forecast_min_c=None,
            forecast_max_c=None,
            weather_text=desc,
            update_time=payload.get("updateTime"),
            parse_warning=True,
            raw=payload,
        )
    return HkoForecast(
        source_type="flw",
        forecast_date_hkt=None,
        forecast_min_c=int(match.group(1)),
        forecast_max_c=int(match.group(2)),
        weather_text=desc,
        update_time=payload.get("updateTime"),
        parse_warning=False,
        raw=payload,
    )
