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

from .model import HourlyForecast, OpenMeteoData, Temperature

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

    def __init__(self, rollover_hour: int = 20, hourly_hours: int = 4) -> None:
        self._rollover_hour = rollover_hour
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
            "forecast_days": "2",  # today + tomorrow, so the evening high/low rollover has data
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
            high, low, high_low_date = self._high_low(forecast["daily"], as_of)
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
                high=high,
                low=low,
                high_low_date=high_low_date,
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

    def _high_low(
        self, daily: dict, as_of: datetime
    ) -> tuple[Temperature | None, Temperature | None, date]:
        """Today's high/low, rolling to tomorrow after ``rollover_hour`` (apparent = day's max)."""
        days = [date.fromisoformat(d) for d in daily["time"]]
        target = as_of.date()
        if as_of.hour >= self._rollover_hour:
            target = target + timedelta(days=1)
        # Fall back to the first available day if the target isn't in range (short forecast window).
        idx = days.index(target) if target in days else 0
        high = Temperature(
            daily["temperature_2m_max"][idx], _float(daily["apparent_temperature_max"][idx])
        )
        low = Temperature(
            daily["temperature_2m_min"][idx], _float(daily["apparent_temperature_min"][idx])
        )
        return high, low, days[idx]


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
    rollover_hour: int = 20  # after this local hour, high/low show the next day
    hourly_hours: int = 4  # number of upcoming hourly forecasts to include


class OpenMeteoSource(Source[OpenMeteoConfig]):
    """The ``open-meteo`` source: fetches an :class:`OpenMeteoData` for the configured location."""

    Config = OpenMeteoConfig

    def __init__(self, config: OpenMeteoConfig) -> None:
        self._config = config
        self._client = OpenMeteoClient(config.rollover_hour, config.hourly_hours)

    async def fetch(self, now: datetime) -> OpenMeteoData:
        return await self._client.fetch(self._config.latitude, self._config.longitude)
