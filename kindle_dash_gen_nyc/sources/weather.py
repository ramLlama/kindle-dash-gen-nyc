"""Fetch current conditions and forecast from the National Weather Service API.

NWS is a multi-step API: a ``/points/{lat},{lon}`` lookup returns per-location URLs, then
those URLs return the hourly/daily forecast, gridpoint data (for apparent temperature), and
the list of nearby observation stations. All data here is kept in SI units at full
precision; callers round for display.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime, timedelta

import niquests

from ..models import HourlyForecast, Temperature, WeatherReport

NWS_API = "https://api.weather.gov"

# Present-weather / description keywords that indicate active precipitation.
_RAIN_KEYWORDS = ("rain", "drizzle", "shower", "thunderstorm", "sleet", "snow", "ice")

# (start_time, apparent_temperature_c) windows, sorted ascending by start.
ApparentSeries = list[tuple[datetime, float]]


class WeatherError(RuntimeError):
    """Raised when weather data cannot be fetched or parsed."""


class NwsClient:
    """Client for the NWS forecast API. All returned data is in SI units."""

    def __init__(
        self,
        user_agent: str,
        rollover_hour: int = 20,
        hourly_hours: int = 4,
        session: niquests.Session | None = None,
    ) -> None:
        self._rollover_hour = rollover_hour
        self._hourly_hours = hourly_hours
        self._session = session or niquests.Session()
        # NWS requires a User-Agent identifying the caller.
        self._session.headers["User-Agent"] = user_agent
        self._session.headers["Accept"] = "application/geo+json"

    def _get_json(self, url: str, params: dict[str, str] | None = None) -> dict:
        try:
            resp = self._session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except niquests.exceptions.RequestException as exc:
            raise WeatherError(f"NWS request failed: {url}") from exc

    def fetch(self, lat: float, lon: float) -> WeatherReport:
        """Fetch current conditions and the near-term forecast for a location (SI)."""
        # NWS rejects coordinates with more than 4 decimal places.
        point = self._get_json(f"{NWS_API}/points/{round(lat, 4):.4f},{round(lon, 4):.4f}")
        try:
            props = point["properties"]
            forecast_url = props["forecast"]
            hourly_url = props["forecastHourly"]
            grid_url = props["forecastGridData"]
            stations_url = props["observationStations"]
            rel = props.get("relativeLocation", {}).get("properties", {})
            city, state = rel.get("city"), rel.get("state")
            location_name = (
                f"{city}, {state}" if city is not None and state is not None else city
            )
        except (KeyError, TypeError) as exc:
            raise WeatherError("unexpected NWS /points response") from exc

        params = {"units": "si"}
        hourly = self._get_json(hourly_url, params)
        daily = self._get_json(forecast_url, params)
        apparent = self._apparent_series(grid_url)

        try:
            hourly_periods = hourly["properties"]["periods"]
            daily_periods = daily["properties"]["periods"]
            if len(hourly_periods) == 0 or len(daily_periods) == 0:
                raise WeatherError("NWS returned no forecast periods")
            now = hourly_periods[0]
            today = daily_periods[0]
            as_of = datetime.fromisoformat(now["startTime"])
            high, low, high_low_date = self._high_low(
                daily_periods, datetime.now(as_of.tzinfo), apparent
            )
            raining, observed = self._observation(stations_url)
            return WeatherReport(
                temperature=Temperature(now["temperature"], _apparent_at(apparent, as_of)),
                conditions=now["shortForecast"],
                humidity=_percent(now.get("relativeHumidity")),
                dewpoint=_float(now.get("dewpoint")),
                wind_speed_kmh=_parse_wind_kmh(now.get("windSpeed")),
                wind_direction=now.get("windDirection") or "",
                precip_probability=_percent(now.get("probabilityOfPrecipitation")),
                raining=raining,
                observed_conditions=observed,
                high=high,
                low=low,
                high_low_date=high_low_date,
                forecast=today["shortForecast"],
                forecast_name=today["name"],
                hourly=self._upcoming_hours(hourly_periods, apparent),
                as_of=as_of,
                location_name=location_name,
            )
        except (KeyError, ValueError) as exc:
            raise WeatherError("unexpected NWS forecast response") from exc

    def _high_low(
        self, daily_periods: list[dict], now_local: datetime, apparent: ApparentSeries
    ) -> tuple[Temperature | None, Temperature | None, date]:
        """Pick the target day's high/low, rolling to the next day after ``rollover_hour``."""
        target = now_local.date()
        if now_local.hour >= self._rollover_hour:
            target = target + timedelta(days=1)
        # Use the first daytime/nighttime periods on or after the target day (today's
        # daytime period may already have expired from the feed by evening).
        high_period = next(
            (p for p in daily_periods if p["isDaytime"] and _pdate(p) >= target), None
        )
        low_period = next(
            (p for p in daily_periods if not p["isDaytime"] and _pdate(p) >= target), None
        )
        # Apparent high/low are the max/min feels-like across the period's time window.
        high = _period_temperature(high_period, apparent, max)
        low = _period_temperature(low_period, apparent, min)
        return high, low, target

    def _upcoming_hours(
        self, hourly_periods: list[dict], apparent: ApparentSeries
    ) -> list[HourlyForecast]:
        """The next ``hourly_hours`` hours after the current one."""
        result = []
        for p in hourly_periods[1 : 1 + self._hourly_hours]:
            time = datetime.fromisoformat(p["startTime"])
            result.append(
                HourlyForecast(
                    time=time,
                    temperature=Temperature(p["temperature"], _apparent_at(apparent, time)),
                    conditions=p["shortForecast"],
                    precip_probability=_percent(p.get("probabilityOfPrecipitation")),
                )
            )
        return result

    def _apparent_series(self, grid_url: str) -> ApparentSeries:
        """Parse the gridpoint apparent-temperature time series, or [] if unavailable."""
        try:
            values = self._get_json(grid_url)["properties"]["apparentTemperature"]["values"]
        except (WeatherError, KeyError, TypeError):
            return []
        series: ApparentSeries = []
        for entry in values:
            value = entry.get("value")
            if value is None:
                continue
            # validTime is "<ISO8601 start>/<ISO8601 duration>"; keep the start.
            start = datetime.fromisoformat(entry["validTime"].split("/")[0])
            series.append((start, float(value)))
        series.sort(key=lambda item: item[0])
        return series

    def _observation(self, stations_url: str) -> tuple[bool | None, str | None]:
        """(is-raining, text description) from the nearest station's latest observation.

        Returns (None, None) if the observation cannot be retrieved — it is an enrichment
        and should not fail the whole report.
        """
        try:
            stations = self._get_json(stations_url)["features"]
            if len(stations) == 0:
                return None, None
            obs = self._get_json(f"{stations[0]['id']}/observations/latest")["properties"]
        except (WeatherError, KeyError, IndexError, TypeError):
            return None, None
        text = obs.get("textDescription")
        return _is_raining(obs.get("presentWeather") or [], text), text


def _apparent_at(series: ApparentSeries, target: datetime) -> float | None:
    """Apparent temperature covering ``target`` (the latest window that has started)."""
    current = None
    for start, value in series:
        if start <= target:
            current = value
        else:
            break
    return current


def _period_temperature(
    period: dict | None, apparent: ApparentSeries, extreme: Callable[[list[float]], float]
) -> Temperature | None:
    """Build a Temperature for a daily period, with feels-like as the window extreme."""
    if period is None:
        return None
    start = datetime.fromisoformat(period["startTime"])
    end = datetime.fromisoformat(period["endTime"])
    window = [value for time, value in apparent if start <= time < end]
    feels_like = extreme(window) if len(window) > 0 else None
    return Temperature(period["temperature"], feels_like)


_WIND_RE = re.compile(r"\d+(?:\.\d+)?")


def _parse_wind_kmh(value: str | None) -> float | None:
    """Parse the leading number from an NWS windSpeed string (already km/h in SI)."""
    if value is None:
        return None
    match = _WIND_RE.search(value)
    return float(match.group()) if match is not None else None


def _percent(field: dict | None) -> int | None:
    """Extract and round a percentage measurement (humidity, precip probability)."""
    value = _float(field)
    return None if value is None else round(value)


def _float(field: dict | None) -> float | None:
    """Extract the raw ``value`` from an NWS measurement object."""
    if field is None:
        return None
    value = field.get("value")
    return None if value is None else float(value)


def _pdate(period: dict) -> date:
    return datetime.fromisoformat(period["startTime"]).date()


def _is_raining(present_weather: list[dict], text: str | None) -> bool:
    """Decide whether it is actively precipitating from a station observation."""
    for entry in present_weather:
        weather = (entry.get("weather") or "").lower()
        if any(keyword in weather for keyword in _RAIN_KEYWORDS):
            return True
    # Present weather was reported and none of it was precipitation.
    if len(present_weather) > 0:
        return False
    # Fall back to the human-readable description when structured data is absent.
    if text is not None:
        return any(keyword in text.lower() for keyword in _RAIN_KEYWORDS)
    return False
