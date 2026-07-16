"""Display formatting helpers.

Internal data is SI; these convert to the configured display units at output time.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class _TemperatureLike(Protocol):
    """A temperature reading these helpers can format: a real value and an optional feels-like.

    Structural so any source's own temperature type satisfies it (there is no shared weather model);
    this keeps formatting provider-agnostic. Members are read-only properties so a ``frozen``
    dataclass (read-only attributes) matches — a plain annotation would demand a settable attribute.
    """

    @property
    def real(self) -> float: ...

    @property
    def feels_like(self) -> float | None: ...


_KMH_TO_MPH = 0.621371

# Condition-text keywords mapped to the icon category the dashboard draws, in priority order:
# the first category whose keywords match the observed/forecast text wins (snow over rain, etc.).
_ICON_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("snow", ("snow", "flurr", "sleet", "wintry", "ice", "blizzard")),
    ("rain", ("rain", "shower", "storm", "thunder", "drizzle")),
    ("cloudy", ("cloud", "overcast", "fog", "haze", "mist")),
)


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


def format_reading(temp: _TemperatureLike | None, units: str) -> str:
    """Format a temperature reading, appending the feels-like in angle brackets when it differs."""
    if temp is None:
        return "—"
    base = format_temp(temp.real, units)
    # Feels-like goes in angle brackets after the real temp, but only when it differs.
    if temp.feels_like is not None and round(temp.feels_like) != round(temp.real):
        return f"{base} ⟨{format_temp(temp.feels_like, units)}⟩"
    return base


def format_apparent(temp: _TemperatureLike | None, units: str) -> str:
    """Format only the apparent ("feels like") temperature, falling back to the real value."""
    if temp is None:
        return "—"
    value = temp.feels_like if temp.feels_like is not None else temp.real
    return format_temp(value, units)


def weather_icon(observed: str | None, conditions: str | None, raining: bool | None) -> str:
    """Classify current conditions into one of four dashboard icons.

    Returns ``"snow"``, ``"rain"``, ``"cloudy"``, or ``"sunny"`` (the default). The ``raining``
    observation forces ``"rain"`` unless the condition text says snow; otherwise the icon is chosen
    from the observed (falling back to forecast) condition text by keyword. Takes plain strings so
    it stays provider-agnostic; a layout passes whatever its own weather type exposes (a source
    without station observations passes ``observed=None``).
    """
    text = (observed or conditions or "").lower()
    for icon, keywords in _ICON_KEYWORDS:
        if any(word in text for word in keywords):
            return icon
    # No keyword matched: trust a bare "raining" observation before defaulting to sunny.
    if raining is True:
        return "rain"
    return "sunny"


def format_wind(kmh: float | None, direction: str, units: str) -> str:
    """Format a wind speed (input km/h) and direction per display ``units`` (shows "mph"/"kmph")."""
    if kmh is None:
        return "—"
    mph = round(kmh * _KMH_TO_MPH)
    metric = f"{round(kmh)} kmph"
    if units == "si":
        speed = metric
    elif units == "both":
        speed = f"{mph} mph / {metric}"
    else:
        speed = f"{mph} mph"  # "us"
    return f"{speed} {direction}".rstrip()
