"""Domain data models produced by the sources and consumed by the renderer.

All weather values are stored in SI units (°C, km/h) at full precision; rounding to whole
degrees happens only at display time in :mod:`kindle_dash_gen_nyc.format`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


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
    conditions: str  # short forecast, e.g. "Partly Sunny"
    precip_probability: int | None  # % chance of precip for the hour


@dataclass(frozen=True, kw_only=True)
class WeatherReport:
    """Current conditions plus near-term forecast for one location (all SI units)."""

    temperature: Temperature  # current conditions
    conditions: str  # current short forecast, e.g. "Partly Cloudy"
    humidity: int | None  # relative humidity, %
    dewpoint: float | None  # °C
    wind_speed_kmh: float | None  # wind speed, km/h
    wind_direction: str  # e.g. "SW" (empty if unknown)
    precip_probability: int | None  # % chance of precip this hour
    raining: bool | None  # from latest station observation; None if unavailable
    observed_conditions: str | None  # station text description, e.g. "Light Rain"
    high: Temperature | None  # daytime high for high_low_date
    low: Temperature | None  # overnight low for high_low_date
    high_low_date: date  # the day the high/low apply to
    forecast: str  # near-term short forecast text
    forecast_name: str  # period label, e.g. "This Afternoon", "Tonight"
    hourly: list[HourlyForecast]  # upcoming hours
    as_of: datetime
    location_name: str | None = None  # e.g. "New York, NY"
