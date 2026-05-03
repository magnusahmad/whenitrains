from __future__ import annotations

import csv
import html
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
FLW_PAGE_URL = "https://www.weather.gov.hk/en/wxinfo/currwx/flw.htm"
FLW_PAGE_DATA_URL = "https://www.weather.gov.hk/json/DYN_DAT_MINDS_FLW.json"


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


def parse_flw_page(text: str) -> HkoForecast:
    plain = _html_to_text(text)
    time_match = re.search(
        r"Bulletin updated at\s+(\d{2}):(\d{2})\s+HKT\s+"
        r"(\d{1,2})/([A-Za-z]{3})/(\d{4})",
        plain,
        flags=re.IGNORECASE,
    )
    update_time = None
    forecast_date = None
    warning = False
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        day = int(time_match.group(3))
        month = datetime.strptime(time_match.group(4).title(), "%b").month
        year = int(time_match.group(5))
        dt = datetime(year, month, day, hour, minute, tzinfo=HKT)
        update_time = dt.isoformat()
        forecast_date = dt.date()
    else:
        warning = True

    range_match = re.search(
        r"(?:between|ranging between)\s+(-?\d+)\s+and\s+(-?\d+)\s+degrees",
        plain,
        flags=re.IGNORECASE,
    )
    if not range_match:
        warning = True
        min_c = None
        max_c = None
    else:
        min_c = int(range_match.group(1))
        max_c = int(range_match.group(2))

    return HkoForecast(
        source_type="flw_page",
        forecast_date_hkt=forecast_date,
        forecast_min_c=min_c,
        forecast_max_c=max_c,
        weather_text=plain,
        update_time=update_time,
        parse_warning=warning,
        raw={"text": plain},
    )


def parse_flw_page_data_json(text: str) -> HkoForecast:
    payload = json.loads(text)
    data = payload.get("DYN_DAT_MINDS_FLW", {})
    date_text = data.get("BulletinDate", {}).get("Val_Eng", "")
    time_text = data.get("BulletinTime", {}).get("Val_Eng", "")
    update_text = ""
    if re.fullmatch(r"\d{8}", date_text) and re.fullmatch(r"\d{4}", time_text):
        dt = datetime.strptime(date_text + time_text, "%Y%m%d%H%M").replace(tzinfo=HKT)
        update_text = dt.strftime("Bulletin updated at %H:%M HKT %d/%b/%Y")

    parts = [
        update_text,
        data.get("FLW_WxForecastGeneralSituation", {}).get("Val_Eng", ""),
        data.get("FLW_WxForecastPeriod", {}).get("Val_Eng", ""),
        data.get("FLW_WxForecastWxDesc", {}).get("Val_Eng", ""),
        (
            data.get("FLW_WxOutlookTitle", {}).get("Val_Eng", "")
            + " : "
            + data.get("FLW_WxOutlookContent", {}).get("Val_Eng", "")
        ).strip(" :"),
    ]
    rendered_text = " ".join(part for part in parts if part)
    forecast = parse_flw_page(rendered_text)
    return HkoForecast(
        source_type=forecast.source_type,
        forecast_date_hkt=forecast.forecast_date_hkt,
        forecast_min_c=forecast.forecast_min_c,
        forecast_max_c=forecast.forecast_max_c,
        weather_text=rendered_text,
        update_time=forecast.update_time,
        parse_warning=forecast.parse_warning,
        raw=payload,
    )


def _html_to_text(text: str) -> str:
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()
