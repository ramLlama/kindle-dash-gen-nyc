"""The ``open-meteo`` source client and config: global weather + air quality, keyless.

Open-Meteo is a single-step, keyless, global API. Two independent endpoints are fetched
concurrently: ``/v1/forecast`` (current conditions, hourly, daily hi/lo) and the air-quality API
(US AQI + particulates). All data is kept in SI units at full precision; callers round for display.
The produced data type lives in :mod:`.model`.

Times come back in the location's local zone (``timezone=auto``) as naive ISO timestamps, so the
datetimes here are naive local — consistent within this source and with the dashboard's
``generated_at``.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

import niquests
from pydantic import BaseModel, ConfigDict

from kindle_dash_gen.sources.registry import Source
from kindle_dash_gen.sources.toolkit import SourceError

from .model import DailyHighLow, HourlyForecast, OpenMeteoData, Temperature

FORECAST_API = "https://api.open-meteo.com/v1/forecast"
AQI_API = "https://air-quality-api.open-meteo.com/v1/air-quality"

# 16-point compass, indexed by round(degrees / 22.5) % 16, to match NWS's cardinal wind_direction.
_COMPASS = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)  # fmt: skip


class OpenMeteoError(SourceError):
    """Raised when Open-Meteo weather data cannot be fetched or parsed."""


class OpenMeteoClient:
    """Client for the Open-Meteo forecast + air-quality APIs. All returned data is in SI units."""

    def __init__(self, hourly_hours: int = 4) -> None:
        self._hourly_hours = hourly_hours

    async def _get_json(
        self, session: niquests.AsyncSession, url: str, params: dict[str, str]
    ) -> dict:
        try:
            resp = await session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except niquests.exceptions.RequestException as exc:
            raise OpenMeteoError(f"Open-Meteo request failed: {url}") from exc

    async def fetch(self, lat: float, lon: float) -> OpenMeteoData:
        """Fetch current conditions, the near-term forecast, and air quality for a location (SI).

        The forecast and air-quality endpoints are independent, so they are fetched concurrently.
        A forecast failure fails the source; an air-quality failure degrades (AQI fields become
        ``None``) but the rest of the report still lands. ``return_exceptions=True`` lets both
        settle (no orphaned request on a closing session when the forecast fails) — the forecast's
        error is then re-raised, while any air-quality error is dropped to empty enrichment.
        """
        async with niquests.AsyncSession() as session:
            results = await asyncio.gather(
                self._forecast(session, lat, lon),
                self._air_quality(session, lat, lon),
                return_exceptions=True,
            )
        forecast, aqi = results
        if isinstance(forecast, BaseException):
            raise forecast
        # Air quality is best-effort enrichment: degrade any failure to empty rather than fail.
        aqi_current: dict = {} if isinstance(aqi, BaseException) else aqi
        return self._build(forecast, aqi_current)

    async def _forecast(self, session: niquests.AsyncSession, lat: float, lon: float) -> dict:
        params = {
            "latitude": f"{lat}",
            "longitude": f"{lon}",
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,dew_point_2m,"
            "precipitation,weather_code,wind_speed_10m,wind_direction_10m",
            "hourly": "temperature_2m,apparent_temperature,precipitation_probability,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min,"
            "apparent_temperature_max,apparent_temperature_min",
            "timezone": "auto",
            "wind_speed_unit": "kmh",
            "forecast_days": "2",  # today + tomorrow: both days' high/low are always reported
        }
        return await self._get_json(session, FORECAST_API, params)

    async def _air_quality(self, session: niquests.AsyncSession, lat: float, lon: float) -> dict:
        """The current air-quality readings, or ``{}`` if the payload lacks them.

        A *failure* of this endpoint is not handled here: it is left to :meth:`fetch`'s
        ``return_exceptions`` gather, which degrades any air-quality error to empty enrichment.
        """
        params = {
            "latitude": f"{lat}",
            "longitude": f"{lon}",
            "current": "us_aqi,pm2_5,pm10,aerosol_optical_depth",
            "timezone": "auto",
        }
        return (await self._get_json(session, AQI_API, params)).get("current") or {}

    def _build(self, forecast: dict, aqi: dict) -> OpenMeteoData:
        try:
            cur = forecast["current"]
            as_of = datetime.fromisoformat(cur["time"])
            hourly, this_hour_precip = self._hours(forecast["hourly"], as_of)
            precip = _float(cur.get("precipitation"))
            # "Today" is the first day the daily arrays cover (local to the coordinates, since the
            # request uses timezone=auto).
            today = date.fromisoformat(forecast["daily"]["time"][0])
            apparent = _float(cur.get("apparent_temperature"))
            temperature = Temperature(cur["temperature_2m"], apparent)
            return OpenMeteoData(
                temperature=temperature,
                weather_code=int(cur["weather_code"]),
                humidity=_int(cur.get("relative_humidity_2m")),
                dewpoint=_float(cur.get("dew_point_2m")),
                wind_speed_kmh=_float(cur.get("wind_speed_10m")),
                wind_direction=_cardinal(cur.get("wind_direction_10m")),
                precip_probability=this_hour_precip,
                raining=(precip > 0) if precip is not None else None,
                today=_day_high_low(forecast["daily"], today),
                tomorrow=_day_high_low(forecast["daily"], today + timedelta(days=1)),
                hourly=hourly,
                as_of=as_of,
                us_aqi=_int(aqi.get("us_aqi")),
                pm2_5=_float(aqi.get("pm2_5")),
                pm10=_float(aqi.get("pm10")),
                aerosol_optical_depth=_float(aqi.get("aerosol_optical_depth")),
            )
        except (KeyError, ValueError, IndexError, TypeError) as exc:
            # TypeError covers a null where an object/array was expected (a common malformed-JSON
            # shape); without it a bad payload would escape as a non-SourceError and, per the
            # pipeline's isolation, sink the whole render instead of just dropping this source.
            raise OpenMeteoError("unexpected Open-Meteo forecast response") from exc

    def _hours(self, hourly: dict, as_of: datetime) -> tuple[list[HourlyForecast], int | None]:
        """The next ``hourly_hours`` after the current hour, plus the current hour's precip chance.

        Open-Meteo returns whole-day hourly arrays; the current hour is excluded from the strip
        (matching the NWS source) but its precip probability surfaces as the report's "this hour".
        """
        times = [datetime.fromisoformat(t) for t in hourly["time"]]
        temps = hourly["temperature_2m"]
        apparent = hourly["apparent_temperature"]
        precip = hourly["precipitation_probability"]
        codes = hourly["weather_code"]
        this_hour = as_of.replace(minute=0, second=0, microsecond=0)
        upcoming: list[HourlyForecast] = []
        this_hour_precip: int | None = None
        for i, t in enumerate(times):
            if t == this_hour:
                this_hour_precip = _int(precip[i])
            if t > this_hour and len(upcoming) < self._hourly_hours:
                upcoming.append(
                    HourlyForecast(
                        time=t,
                        temperature=Temperature(temps[i], _float(apparent[i])),
                        weather_code=int(codes[i]),
                        precip_probability=_int(precip[i]),
                    )
                )
        return upcoming, this_hour_precip


def _day_high_low(daily: dict, day: date) -> DailyHighLow:
    """The high/low for exactly ``day`` (apparent = that day's apparent max/min).

    A day outside the forecast window reports ``None`` readings rather than substituting another
    day's, so the ``day`` field is always truthful. A ``null`` reading degrades the same way, rather
    than building a ``Temperature`` whose ``real`` is ``None`` despite being typed ``float``.
    """
    days = [date.fromisoformat(d) for d in daily["time"]]
    if day not in days:
        return DailyHighLow(day=day, high=None, low=None)
    idx = days.index(day)
    return DailyHighLow(
        day=day,
        high=_reading(daily["temperature_2m_max"][idx], daily["apparent_temperature_max"][idx]),
        low=_reading(daily["temperature_2m_min"][idx], daily["apparent_temperature_min"][idx]),
    )


def _reading(real: float | None, apparent: float | None) -> Temperature | None:
    """A Temperature, or ``None`` when the provider reported no actual value."""
    return None if real is None else Temperature(real, _float(apparent))


def _cardinal(degrees: float | None) -> str:
    """Convert a wind direction in degrees to a 16-point compass label ("" if unknown)."""
    if degrees is None:
        return ""
    return _COMPASS[round(float(degrees) / 22.5) % 16]


def _float(value: float | int | None) -> float | None:
    """Coerce an optional numeric field to float, preserving None."""
    return None if value is None else float(value)


def _int(value: float | int | None) -> int | None:
    """Coerce an optional numeric field to a rounded int, preserving None."""
    return None if value is None else round(float(value))


class OpenMeteoConfig(BaseModel):
    """Config for the ``[sources.open-meteo]`` table."""

    model_config = ConfigDict(extra="forbid")

    latitude: float
    longitude: float
    hourly_hours: int = 4  # number of upcoming hourly forecasts to include


class OpenMeteoSource(Source[OpenMeteoConfig]):
    """The ``open-meteo`` source: fetches an :class:`OpenMeteoData` for the configured location."""

    Config = OpenMeteoConfig

    def __init__(self, config: OpenMeteoConfig) -> None:
        self._config = config
        self._client = OpenMeteoClient(config.hourly_hours)

    async def fetch(self, now: datetime) -> OpenMeteoData:
        return await self._client.fetch(self._config.latitude, self._config.longitude)
