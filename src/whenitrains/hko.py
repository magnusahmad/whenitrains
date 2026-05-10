from __future__ import annotations

import csv
import html
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from math import floor
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
OCF_STATION_URL = "https://maps.weather.gov.hk/ocf/dat/HKO.xml"
OCF_TEXT_URL = "https://maps.weather.gov.hk/ocf/text_e.html?mode=0&station=HKO"
AWS_GIS_FORECAST_URL = "https://www.hko.gov.hk/wxinfo/awsgis/forecast/HKO.xml"
AWS_GIS_READINGS_URL = "https://www.hko.gov.hk/wxinfo/awsgis/latestReadings_AWS1_v2.txt"
RHRREAD_URL = "https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=rhrread&lang=en"


@dataclass(frozen=True)
class HkoObservation:
    observed_at_hkt: datetime
    station: str
    since_midnight_max_c: float
    since_midnight_min_c: float
    raw: dict[str, str]


@dataclass(frozen=True)
class HkoCurrentTemperature:
    observed_at_hkt: datetime
    station: str
    temperature_c: float
    raw: dict[str, Any]
    since_midnight_max_c: float | None = None
    since_midnight_min_c: float | None = None


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


@dataclass(frozen=True)
class OcfForecastSample:
    forecast_date_hkt: date
    forecast_min_c: int | None
    forecast_max_c: int | None
    raw_min_c: float | None
    raw_max_c: float | None
    hourly_temperatures: list[dict[str, Any]]
    raw: dict[str, Any]


@dataclass(frozen=True)
class FetchResponse:
    url: str
    text: str
    headers: dict[str, str]

    @property
    def http_date(self) -> datetime | None:
        return _parse_http_datetime(self.headers.get("Date"))

    @property
    def http_last_modified(self) -> datetime | None:
        return _parse_http_datetime(self.headers.get("Last-Modified"))

    @property
    def etag(self) -> str | None:
        return self.headers.get("Etag") or self.headers.get("ETag")


def fetch_text(url: str) -> str:
    return fetch_response(url).text


def fetch_response(url: str) -> FetchResponse:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
    }
    if "maps.weather.gov.hk/ocf/" in url:
        headers["Referer"] = OCF_TEXT_URL
    request = Request(url, headers=headers)
    with urlopen(request, timeout=15) as response:
        return FetchResponse(
            url=url,
            text=response.read().decode("utf-8-sig"),
            headers=dict(response.headers.items()),
        )


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


def parse_rhrread_temperature_json(
    text: str, station: str = "Hong Kong Observatory"
) -> HkoCurrentTemperature:
    payload = json.loads(text)
    update_time = datetime.fromisoformat(payload["updateTime"])
    if update_time.tzinfo is None:
        update_time = update_time.replace(tzinfo=HKT)
    temperature_rows = payload.get("temperature", {}).get("data") or []
    for row in temperature_rows:
        if row.get("place") == station:
            return HkoCurrentTemperature(
                observed_at_hkt=update_time.astimezone(HKT),
                station=station,
                temperature_c=float(row["value"]),
                raw={"payload": payload, "temperature_row": row},
            )
    raise ValueError(f"{station} temperature row not found in rhrread JSON")


def parse_aws_gis_current_temperature(
    text: str, station_code: str = "HKO"
) -> HkoCurrentTemperature:
    lines = [line.strip() for line in text.lstrip("\ufeff").splitlines() if line.strip()]
    if len(lines) < 3:
        raise ValueError("AWS GIS readings payload is missing rows")
    observed_at = _parse_aws_gis_header_time(lines[0])
    reader = csv.DictReader(StringIO("\n".join(lines[1:])))
    for row in reader:
        if (row.get("STN") or "").upper() != station_code.upper():
            continue
        value = _parse_aws_gis_float(row.get("TEMP"))
        if value is None:
            raise ValueError(f"{station_code} temperature missing in AWS GIS readings")
        max_temp = _parse_aws_gis_float(row.get("MAXTEMP"))
        min_temp = _parse_aws_gis_float(row.get("MINTEMP"))
        if observed_at.hour == 0 and observed_at.minute == 0:
            max_temp = None
            min_temp = None
        return HkoCurrentTemperature(
            observed_at_hkt=observed_at,
            station=station_code.upper(),
            temperature_c=value,
            raw={"row": row, "header": lines[0]},
            since_midnight_max_c=max_temp,
            since_midnight_min_c=min_temp,
        )
    raise ValueError(f"{station_code} row not found in AWS GIS readings")


def _parse_aws_gis_header_time(header: str) -> datetime:
    match = re.search(
        r"recorded at\s+(\d{1,2}):(\d{2})\s+Hong Kong Time\s+"
        r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})",
        header,
        flags=re.IGNORECASE,
    )
    if not match:
        raise ValueError("AWS GIS readings timestamp not found")
    hour = int(match.group(1))
    minute = int(match.group(2))
    day = int(match.group(3))
    month = datetime.strptime(match.group(4).title(), "%B").month
    year = int(match.group(5))
    return datetime(year, month, day, hour, minute, tzinfo=HKT)


def _parse_aws_gis_float(value: str | None) -> float | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped or stripped.upper() in {"M", "N/A", "9999"}:
        return None
    return float(stripped.rstrip("*"))


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
        max_c = None
    else:
        max_c = int(range_match.group(2))

    return HkoForecast(
        source_type="flw_page",
        forecast_date_hkt=forecast_date,
        forecast_min_c=None,
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


def parse_ocf_station_json(text: str) -> tuple[list[HkoForecast], list[OcfForecastSample]]:
    payload = json.loads(text)
    update_time = _parse_ocf_datetime(payload.get("LastModified"))
    daily_rows = payload.get("DailyForecast") or []
    hourly_by_date = _group_ocf_hourly_temperatures(
        payload.get("HourlyWeatherForecast") or []
    )
    forecasts: list[HkoForecast] = []
    samples: list[OcfForecastSample] = []
    for row in daily_rows:
        forecast_date = _parse_yyyymmdd(row.get("ForecastDate"))
        raw_min = _as_float(row.get("ForecastMinimumTemperature"))
        raw_max = _as_float(row.get("ForecastMaximumTemperature"))
        display_min = _round_table_temperature(raw_min)
        display_max = _round_table_temperature(raw_max)
        warning = forecast_date is None or display_max is None
        raw = dict(row)
        raw["StationCode"] = payload.get("StationCode")
        raw["ModelTime"] = payload.get("ModelTime")
        raw["LastModified"] = payload.get("LastModified")
        forecasts.append(
            HkoForecast(
                source_type="ocf_station",
                forecast_date_hkt=forecast_date,
                forecast_min_c=display_min,
                forecast_max_c=display_max,
                psr=str(row.get("ForecastChanceOfRain") or ""),
                update_time=update_time.isoformat() if update_time else None,
                parse_warning=warning,
                raw=raw,
            )
        )
        if forecast_date is not None:
            samples.append(
                OcfForecastSample(
                    forecast_date_hkt=forecast_date,
                    forecast_min_c=display_min,
                    forecast_max_c=display_max,
                    raw_min_c=raw_min,
                    raw_max_c=raw_max,
                    hourly_temperatures=hourly_by_date.get(forecast_date, []),
                    raw=raw,
                )
            )
    return forecasts, samples


def _parse_ocf_datetime(value: Any) -> datetime | None:
    text = str(value or "")
    if not re.fullmatch(r"\d{14}", text):
        return None
    return datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=HKT)


def parse_http_datetime_hkt(value: str | None) -> datetime | None:
    parsed = _parse_http_datetime(value)
    if parsed is None:
        return None
    return parsed.astimezone(HKT)


def _parse_http_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_yyyymmdd(value: Any) -> date | None:
    text = str(value or "")
    if not re.fullmatch(r"\d{8}", text):
        return None
    return datetime.strptime(text, "%Y%m%d").date()


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_table_temperature(value: float | None) -> int | None:
    if value is None:
        return None
    return floor(value + 0.5)


def _group_ocf_hourly_temperatures(rows: list[dict[str, Any]]) -> dict[date, list[dict[str, Any]]]:
    grouped: dict[date, list[dict[str, Any]]] = {}
    for row in rows:
        hour_text = str(row.get("ForecastHour") or "")
        if not re.fullmatch(r"\d{10}", hour_text):
            continue
        forecast_date = datetime.strptime(hour_text[:8], "%Y%m%d").date()
        grouped.setdefault(forecast_date, []).append(
            {
                "forecast_hour_hkt": datetime.strptime(hour_text, "%Y%m%d%H")
                .replace(tzinfo=HKT)
                .isoformat(),
                "temperature_c": row.get("ForecastTemperature"),
                "raw": row,
            }
        )
    return grouped


def _html_to_text(text: str) -> str:
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()
