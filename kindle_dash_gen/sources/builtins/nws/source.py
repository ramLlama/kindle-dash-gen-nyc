"""The ``nws`` source client and config: current conditions and forecast from the NWS.

NWS is a multi-step API: a ``/points/{lat},{lon}`` lookup returns per-location URLs, then those
URLs return the hourly/daily forecast, gridpoint data (for apparent temperature), and the list of
nearby observation stations. All data here is kept in SI units at full precision; callers round for
display. The produced data type lives in :mod:`.model`.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta

import niquests
from pydantic import BaseModel, ConfigDict

from kindle_dash_gen.sources.registry import Source
from kindle_dash_gen.sources.toolkit import SourceError

from .model import DailyHighLow, HourlyForecast, NwsData, Temperature, WeatherAlert

NWS_API = "https://api.weather.gov"

# Present-weather / description keywords that indicate active precipitation.
_RAIN_KEYWORDS = ("rain", "drizzle", "shower", "thunderstorm", "sleet", "snow", "ice")

# (start_time, apparent_temperature_c) windows, sorted ascending by start.
ApparentSeries = list[tuple[datetime, float]]


class WeatherError(SourceError):
    """Raised when weather data cannot be fetched or parsed."""


# Exceptions swallowed by the best-effort enrichment lookups (apparent-temperature series, latest
# observation): these must degrade to empty rather than fail the whole report. Hoisted into named
# tuples so an inline ``except (A, B):`` isn't reduced by ruff to the confusing bare-tuple form
# ``except A, B:`` under Python 3.14's grammar (valid there, but non-idiomatic and non-portable).
_APPARENT_SWALLOW = (WeatherError, KeyError, TypeError)
_OBSERVATION_SWALLOW = (WeatherError, KeyError, IndexError, TypeError)
_ALERTS_SWALLOW = (WeatherError, KeyError, TypeError, ValueError)
# A single malformed alert feature (e.g. missing ``event``) is skipped, not fatal to the list.
_ALERT_ITEM_SWALLOW = (KeyError, TypeError)


class NwsClient:
    """Client for the NWS forecast API. All returned data is in SI units."""

    def __init__(self, user_agent: str, hourly_hours: int = 4) -> None:
        self._user_agent = user_agent
        self._hourly_hours = hourly_hours

    async def _get_json(
        self,
        session: niquests.AsyncSession,
        url: str,
        params: dict[str, str] | None = None,
    ) -> dict:
        try:
            resp = await session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except niquests.exceptions.RequestException as exc:
            raise WeatherError(f"NWS request failed: {url}") from exc

    async def fetch(self, lat: float, lon: float) -> NwsData:
        """Fetch current conditions and the near-term forecast for a location (SI).

        NWS is a multi-step API: the ``/points`` lookup must complete first (it yields the per-
        location URLs), then the five independent downstream calls — hourly, daily, the apparent-
        temperature grid, the latest observation, and active alerts — are fetched concurrently.
        """
        # NWS requires a User-Agent identifying the caller.
        async with niquests.AsyncSession() as session:
            session.headers["User-Agent"] = self._user_agent
            session.headers["Accept"] = "application/geo+json"
            return await self._fetch(session, lat, lon)

    async def _fetch(self, session: niquests.AsyncSession, lat: float, lon: float) -> NwsData:
        point = await self._get_json(session, f"{NWS_API}/points/{_point(lat, lon)}")
        try:
            props = point["properties"]
            forecast_url = props["forecast"]
            hourly_url = props["forecastHourly"]
            grid_url = props["forecastGridData"]
            stations_url = props["observationStations"]
            rel = props.get("relativeLocation", {}).get("properties", {})
            city, state = rel.get("city"), rel.get("state")
            location_name = f"{city}, {state}" if city is not None and state is not None else city
        except (KeyError, TypeError) as exc:
            raise WeatherError("unexpected NWS /points response") from exc

        params = {"units": "si"}
        # The five downstream calls are independent, so fetch them concurrently.
        hourly, daily, apparent, (raining, observed), alerts = await asyncio.gather(
            self._get_json(session, hourly_url, params),
            self._get_json(session, forecast_url, params),
            self._apparent_series(session, grid_url),
            self._observation(session, stations_url),
            self._alerts(session, lat, lon),
        )

        try:
            hourly_periods = hourly["properties"]["periods"]
            daily_periods = daily["properties"]["periods"]
            if len(hourly_periods) == 0 or len(daily_periods) == 0:
                raise WeatherError("NWS returned no forecast periods")
            now = hourly_periods[0]
            first_period = daily_periods[0]
            as_of = datetime.fromisoformat(now["startTime"])
            # Anchor "today" on the current hour's local date, not on the first daily period's.
            # The first daily period is usually today's, but only because NWS truncates an
            # in-progress period's startTime to roughly now; if it ever emitted the untruncated
            # start, between midnight and ~06:00 the first period would be the *previous* evening's
            # night period and this would silently be yesterday. `as_of` carries the location's UTC
            # offset, so its date is unambiguously the local calendar day — and it matches how the
            # open-meteo source anchors (`daily.time[0]` under `timezone=auto`), so the two
            # providers agree on "today" for a layout that mixes them.
            today = as_of.date()
            return NwsData(
                temperature=Temperature(now["temperature"], _apparent_at(apparent, as_of)),
                conditions=now["shortForecast"],
                humidity=_percent(now.get("relativeHumidity")),
                dewpoint=_float(now.get("dewpoint")),
                wind_speed_kmh=_parse_wind_kmh(now.get("windSpeed")),
                wind_direction=now.get("windDirection") or "",
                precip_probability=_percent(now.get("probabilityOfPrecipitation")),
                raining=raining,
                observed_conditions=observed,
                today=_day_high_low(daily_periods, today, apparent),
                tomorrow=_day_high_low(daily_periods, today + timedelta(days=1), apparent),
                forecast=first_period["shortForecast"],
                forecast_name=first_period["name"],
                hourly=self._upcoming_hours(hourly_periods, apparent),
                as_of=as_of,
                location_name=location_name,
                alerts=alerts,
            )
        except (KeyError, ValueError) as exc:
            raise WeatherError("unexpected NWS forecast response") from exc

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

    async def _apparent_series(
        self, session: niquests.AsyncSession, grid_url: str
    ) -> ApparentSeries:
        """Parse the gridpoint apparent-temperature time series, or [] if unavailable."""
        try:
            grid = await self._get_json(session, grid_url)
            values = grid["properties"]["apparentTemperature"]["values"]
        except _APPARENT_SWALLOW:
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

    async def _observation(
        self, session: niquests.AsyncSession, stations_url: str
    ) -> tuple[bool | None, str | None]:
        """(is-raining, text description) from the nearest station's latest observation.

        Returns (None, None) if the observation cannot be retrieved — it is an enrichment
        and should not fail the whole report.
        """
        try:
            stations = (await self._get_json(session, stations_url))["features"]
            if len(stations) == 0:
                return None, None
            latest = await self._get_json(session, f"{stations[0]['id']}/observations/latest")
            obs = latest["properties"]
        except _OBSERVATION_SWALLOW:
            return None, None
        text = obs.get("textDescription")
        return _is_raining(obs.get("presentWeather") or [], text), text

    async def _alerts(
        self, session: niquests.AsyncSession, lat: float, lon: float
    ) -> list[WeatherAlert]:
        """Active NWS alerts for the point, or [] if unavailable.

        Alerts are enrichment: a failure here degrades to no alerts rather than failing the
        whole report. Every currently-active alert for the point is carried unfiltered (NWS
        returns them in a single response; the layout decides what to render).
        """
        try:
            payload = await self._get_json(
                session, f"{NWS_API}/alerts/active", {"point": _point(lat, lon)}
            )
            features = payload["features"]
        except _ALERTS_SWALLOW:
            return []
        # Isolate per feature: one malformed alert must not drop the valid siblings.
        alerts = []
        for feature in features:
            try:
                alerts.append(_parse_alert(feature["properties"]))
            except _ALERT_ITEM_SWALLOW:
                continue
        return alerts


def _point(lat: float, lon: float) -> str:
    """Format a lat,lon point string; NWS rejects more than 4 decimal places."""
    return f"{round(lat, 4):.4f},{round(lon, 4):.4f}"


def _parse_alert(props: dict) -> WeatherAlert:
    """Build a WeatherAlert from an alert feature's ``properties``.

    ``event`` is required (a feature lacking it is treated as malformed and skipped upstream).
    NWS-populated classification fields default to the CAP ``"Unknown"`` vocabulary when absent;
    the free-text and timestamp fields degrade to ``None``.
    """
    return WeatherAlert(
        event=props["event"],
        category=props.get("category") or "Unknown",
        severity=props.get("severity") or "Unknown",
        certainty=props.get("certainty") or "Unknown",
        urgency=props.get("urgency") or "Unknown",
        status=props.get("status") or "Unknown",
        message_type=props.get("messageType") or "Unknown",
        area_desc=props.get("areaDesc") or "",
        sender_name=props.get("senderName") or "",
        headline=props.get("headline"),
        description=props.get("description"),
        instruction=props.get("instruction"),
        response=props.get("response"),
        effective=_parse_alert_time(props.get("effective")),
        onset=_parse_alert_time(props.get("onset")),
        expires=_parse_alert_time(props.get("expires")),
        ends=_parse_alert_time(props.get("ends")),
    )


def _parse_alert_time(value: str | None) -> datetime | None:
    """Parse an ISO8601 alert timestamp, or None if absent/malformed."""
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError, TypeError:
        return None


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


def _day_high_low(daily_periods: list[dict], day: date, apparent: ApparentSeries) -> DailyHighLow:
    """The high/low for exactly ``day``, from its daytime and nighttime periods.

    Matches the day exactly rather than falling forward to the next available period. NWS drops a
    day's daytime period once it has passed, so from that evening today's high is genuinely unknown
    — reporting ``None`` is honest, where falling forward would return *tomorrow's* high labelled
    with today's date.
    """
    high_period = next((p for p in daily_periods if p["isDaytime"] and _pdate(p) == day), None)
    low_period = next((p for p in daily_periods if not p["isDaytime"] and _pdate(p) == day), None)
    # Apparent high/low are the max/min feels-like across the period's time window.
    return DailyHighLow(
        day=day,
        high=_period_temperature(high_period, apparent, max),
        low=_period_temperature(low_period, apparent, min),
    )


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


class NwsConfig(BaseModel):
    """Config for the ``[sources.nws]`` table."""

    model_config = ConfigDict(extra="forbid")

    latitude: float
    longitude: float
    user_agent: str  # NWS requires a User-Agent identifying the caller
    hourly_hours: int = 4  # number of upcoming hourly forecasts to include


class NwsSource(Source[NwsConfig]):
    """The ``nws`` source: fetches an :class:`NwsData` for the configured location."""

    Config = NwsConfig

    def __init__(self, config: NwsConfig) -> None:
        self._config = config
        self._client = NwsClient(config.user_agent, config.hourly_hours)

    async def fetch(self, now: datetime) -> NwsData:
        return await self._client.fetch(self._config.latitude, self._config.longitude)
