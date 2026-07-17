"""Tests for display formatting helpers."""

from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from kindle_dash_gen.format import (
    format_aqi,
    format_eta,
    format_reading,
    format_temp,
    format_wind,
    weather_icon,
)

_NOW = datetime(2026, 7, 1, 12, 0, 0)


@dataclass(frozen=True)
class _Temp:
    """A minimal temperature stand-in: the formatters accept any real/feels_like structure."""

    real: float
    feels_like: float | None


@pytest.mark.parametrize(
    "celsius,units,expected",
    [
        (0, "us", "32°F"),
        (0, "si", "0°C"),
        (100, "both", "212°F / 100°C"),
        (23.4, "us", "74°F"),  # rounded
        (None, "us", "—"),
    ],
)
def test_format_temp(celsius, units, expected) -> None:
    assert format_temp(celsius, units) == expected


@pytest.mark.parametrize(
    "kmh,direction,units,expected",
    [
        (16, "SW", "us", "10 mph SW"),  # 16 kmph ~= 10 mph
        (16, "SW", "si", "16 kmph SW"),
        (16, "SW", "both", "10 mph / 16 kmph SW"),
        (10, "", "si", "10 kmph"),  # no direction -> no trailing space
        (None, "SW", "us", "—"),
    ],
)
def test_format_wind(kmh, direction, units, expected) -> None:
    assert format_wind(kmh, direction, units) == expected


@pytest.mark.parametrize(
    "aqi,expected",
    [
        (0, "AQI 0 · Good"),
        (50, "AQI 50 · Good"),  # inclusive upper bound
        (51, "AQI 51 · Moderate"),
        (110, "AQI 110 · Unhealthy (Sensitive)"),
        (175, "AQI 175 · Unhealthy"),
        (250, "AQI 250 · Very Unhealthy"),
        (400, "AQI 400 · Hazardous"),  # 301+
        (None, "—"),
    ],
)
def test_format_aqi(aqi, expected) -> None:
    assert format_aqi(aqi) == expected


@pytest.mark.parametrize(
    "temp,units,expected",
    [
        (_Temp(31, 40.6), "us", "88°F ⟨105°F⟩"),
        (_Temp(31, 31.2), "si", "31°C"),  # feels-like rounds to same -> omitted
        (_Temp(20, None), "us", "68°F"),  # no apparent temp
        (None, "us", "—"),
    ],
)
def test_format_reading(temp, units, expected) -> None:
    assert format_reading(temp, units) == expected


@pytest.mark.parametrize(
    "delta_minutes,expected",
    [(3, "3 min"), (0, "0 min"), (-5, "0 min"), (2.4, "2 min"), (2.6, "3 min")],
)
def test_format_eta(delta_minutes: float, expected: str) -> None:
    assert format_eta(_NOW + timedelta(minutes=delta_minutes), _NOW) == expected


@pytest.mark.parametrize(
    "conditions,observed,raining,expected",
    [
        ("Clear", None, False, "sunny"),  # default, no keyword
        ("Mostly Cloudy", None, False, "cloudy"),
        ("Light Rain", "Rain", True, "rain"),
        ("Snow Showers", None, False, "snow"),  # snow beats the "showers" rain keyword
        ("Clear", None, True, "rain"),  # raining observation overrides a keyword-less default
        ("Sunny", "Patchy Fog", False, "cloudy"),  # observed text wins over forecast
    ],
)
def test_weather_icon(conditions, observed, raining, expected) -> None:
    assert weather_icon(observed, conditions, raining) == expected
