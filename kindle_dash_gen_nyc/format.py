"""Display formatting helpers.

Internal data is SI; these convert to the configured display units at output time.
"""

from __future__ import annotations

from datetime import datetime

from .models import Temperature

_KMH_TO_MPH = 0.621371


def format_eta(arrival: datetime, now: datetime) -> str:
    """Format an arrival time as whole minutes from now, e.g. "3 min" (never negative)."""
    minutes = max(0, round((arrival - now).total_seconds() / 60))
    return f"{minutes} min"


def _c_to_f(celsius: float) -> int:
    return round(celsius * 9 / 5 + 32)


def format_temp(celsius: float | None, units: str) -> str:
    """Format a Celsius temperature per display ``units`` ('us', 'si', or 'both')."""
    if celsius is None:
        return "—"
    if units == "si":
        return f"{round(celsius)}°C"
    if units == "both":
        return f"{_c_to_f(celsius)}°F / {round(celsius)}°C"
    return f"{_c_to_f(celsius)}°F"  # "us"


def format_reading(temp: Temperature | None, units: str) -> str:
    """Format a temperature reading, appending the feels-like in angle brackets when it differs."""
    if temp is None:
        return "—"
    base = format_temp(temp.real, units)
    # Feels-like goes in angle brackets after the real temp, but only when it differs.
    if temp.feels_like is not None and round(temp.feels_like) != round(temp.real):
        return f"{base} ⟨{format_temp(temp.feels_like, units)}⟩"
    return base


def format_apparent(temp: Temperature | None, units: str) -> str:
    """Format only the apparent ("feels like") temperature, falling back to the real value."""
    if temp is None:
        return "—"
    value = temp.feels_like if temp.feels_like is not None else temp.real
    return format_temp(value, units)


def format_wind(kmh: float | None, direction: str, units: str) -> str:
    """Format a km/h wind speed and direction per display ``units``."""
    if kmh is None:
        return "—"
    mph = round(kmh * _KMH_TO_MPH)
    metric = f"{round(kmh)} km/h"
    if units == "si":
        speed = metric
    elif units == "both":
        speed = f"{mph} mph / {metric}"
    else:
        speed = f"{mph} mph"  # "us"
    return f"{speed} {direction}".rstrip()
