"""Display formatting helpers.

Internal data is SI; these convert to the configured display units at output time.
"""

from __future__ import annotations

from .models import Temperature

_KMH_TO_MPH = 0.621371


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
    """Format a temperature reading, appending "(feels X)" when it differs from actual."""
    if temp is None:
        return "—"
    base = format_temp(temp.real, units)
    if temp.feels_like is not None and round(temp.feels_like) != round(temp.real):
        return f"{base} (feels {format_temp(temp.feels_like, units)})"
    return base


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
