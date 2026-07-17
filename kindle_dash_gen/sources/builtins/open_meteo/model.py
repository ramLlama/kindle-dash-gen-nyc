"""Data the ``open-meteo`` source produces.

All values are SI (°C, km/h) at full precision; rounding happens only at display time in
:mod:`kindle_dash_gen.format`. These types are owned by the source that produces them (there is no
shared cross-provider weather model): a layout that renders weather reconciles each provider's own
type in its own adapter. This mirrors :mod:`kindle_dash_gen.sources.builtins.nws.model`, but the two
are deliberately independent — Open-Meteo also carries air-quality fields NWS has no equivalent for.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

# WMO weather-interpretation codes → canonical description. This is Open-Meteo's vocabulary, so it
# is owned here alongside the data. The text is purely descriptive: a consumer that wants to display
# conditions calls :func:`wmo_description`. Deciding which *icon* a code maps to is a layout concern
# — a layout classifies the raw ``weather_code`` itself and must not keyword-match this text.
_WMO_DESCRIPTIONS: dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snowfall",
    73: "Moderate snowfall",
    75: "Heavy snowfall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def wmo_description(code: int) -> str:
    """The canonical text for a WMO weather-interpretation code (generic label if unknown)."""
    return _WMO_DESCRIPTIONS.get(code, "Unknown")


@dataclass(frozen=True)
class Temperature:
    """A temperature reading in °C: the actual value and the apparent ("feels like")."""

    real: float
    feels_like: float | None  # None if apparent temperature is unavailable


@dataclass(frozen=True, kw_only=True)
class HourlyForecast:
    """One upcoming hour of forecast."""

    time: datetime
    temperature: Temperature
    weather_code: int  # raw WMO code (see wmo_description); a layout maps it to an icon
    precip_probability: int | None  # % chance of precip for the hour


@dataclass(frozen=True, kw_only=True)
class OpenMeteoData:
    """Current conditions, near-term forecast, and air quality for one location (all SI units).

    A full peer to :class:`~kindle_dash_gen.sources.builtins.nws.model.NwsData` for the fields
    Open-Meteo can supply, plus air-quality fields (``us_aqi`` and particulates, so wildfire smoke
    folds in) that NWS does not provide. Air-quality is a best-effort enrichment: if that endpoint
    fails, its fields are ``None`` while the rest of the report still lands.
    """

    temperature: Temperature  # current conditions
    weather_code: int  # raw WMO code (see wmo_description); a layout maps it to an icon
    humidity: int | None  # relative humidity, %
    dewpoint: float | None  # °C
    wind_speed_kmh: float | None  # wind speed, km/h
    wind_direction: str  # cardinal, e.g. "SW" (empty if unknown)
    precip_probability: int | None  # % chance of precip this hour
    raining: bool | None  # derived from current precipitation; None if unavailable
    high: Temperature | None  # daytime high for high_low_date
    low: Temperature | None  # overnight low for high_low_date
    high_low_date: date  # the day the high/low apply to
    hourly: list[HourlyForecast]  # upcoming hours
    as_of: datetime
    # Air quality (from the air-quality endpoint; all None if that fetch failed).
    us_aqi: int | None  # US Air Quality Index
    pm2_5: float | None  # µg/m³
    pm10: float | None  # µg/m³
    aerosol_optical_depth: float | None  # unitless; elevated by wildfire smoke
    location_name: str | None = None  # Open-Meteo returns no place name, so always None for now
