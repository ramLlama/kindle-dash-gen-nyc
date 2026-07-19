"""Data the ``nws`` source produces.

All values are SI (°C, km/h) at full precision; rounding to whole degrees happens only at
display time in :mod:`kindle_dash_gen.format`. These types are owned by the source that produces
them (there is no shared cross-provider weather model): a layout that renders weather reconciles
each provider's own type in its own adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True, kw_only=True)
class WeatherAlert:
    """One active NWS weather alert (e.g. a Flash Flood Warning).

    Mirrors the CAP alert ``properties`` NWS supplies. Text/classification fields that NWS always
    populates are plain ``str``; ones NWS may omit are ``str | None``. All four timestamps are
    ``datetime | None`` (absent or unparseable degrade to ``None``). Values are carried verbatim;
    presentation is the layout's job.
    """

    event: str  # alert type, e.g. "Flash Flood Warning"
    category: str  # CAP category, e.g. "Met" (meteorological)
    severity: str  # CAP severity: Extreme/Severe/Moderate/Minor/Unknown
    certainty: str  # CAP certainty: Observed/Likely/Possible/Unlikely/Unknown
    urgency: str  # CAP urgency: Immediate/Expected/Future/Past/Unknown
    status: str  # CAP status, e.g. "Actual"
    message_type: str  # CAP msgType, e.g. "Alert"/"Update"/"Cancel"
    area_desc: str  # affected-area text, e.g. "Elko, NV"
    sender_name: str  # issuing office, e.g. "NWS Elko NV"
    headline: str | None  # human-readable summary line
    description: str | None  # multi-paragraph body (WHAT/WHERE/WHEN/IMPACTS)
    instruction: str | None  # preparedness actions ("Turn around, don't drown...")
    response: str | None  # recommended response, e.g. "Avoid"/"Prepare"
    effective: datetime | None  # when the alert was issued/effective
    onset: datetime | None  # when the alerted event begins
    expires: datetime | None  # when the alert message expires
    ends: datetime | None  # when the alerted event ends


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
class DailyHighLow:
    """One calendar day's high/low (the day is local to the forecast location).

    Either reading is ``None`` when the feed no longer carries that period — NWS drops a day's
    daytime period once it has passed, so by evening today's high is genuinely unknown rather than
    substitutable with a later day's.
    """

    day: date
    high: Temperature | None  # daytime high
    low: Temperature | None  # overnight low


@dataclass(frozen=True, kw_only=True)
class NwsData:
    """Current conditions plus near-term forecast for one location, from NWS (all SI units)."""

    temperature: Temperature  # current conditions
    conditions: str  # current short forecast, e.g. "Partly Cloudy"
    humidity: int | None  # relative humidity, %
    dewpoint: float | None  # °C
    wind_speed_kmh: float | None  # wind speed, km/h
    wind_direction: str  # e.g. "SW" (empty if unknown)
    precip_probability: int | None  # % chance of precip this hour
    raining: bool | None  # from latest station observation; None if unavailable
    observed_conditions: str | None  # station text description, e.g. "Light Rain"
    # Both days are always reported; choosing which to display is a layout decision.
    today: DailyHighLow
    tomorrow: DailyHighLow
    forecast: str  # near-term short forecast text
    forecast_name: str  # period label, e.g. "This Afternoon", "Tonight"
    hourly: list[HourlyForecast]  # upcoming hours
    as_of: datetime
    location_name: str | None = None  # e.g. "New York, NY"
    alerts: list[WeatherAlert] = field(default_factory=list)  # active NWS alerts
