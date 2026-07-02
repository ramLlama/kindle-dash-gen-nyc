"""Tests for display formatting helpers."""

from datetime import datetime, timedelta

import pytest

from kindle_dash_gen_nyc.format import format_eta, format_reading, format_temp, format_wind
from kindle_dash_gen_nyc.models import Temperature

_NOW = datetime(2026, 7, 1, 12, 0, 0)


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
        (16, "SW", "us", "10 mph SW"),  # 16 km/h ~= 10 mph
        (16, "SW", "si", "16 km/h SW"),
        (16, "SW", "both", "10 mph / 16 km/h SW"),
        (10, "", "si", "10 km/h"),  # no direction -> no trailing space
        (None, "SW", "us", "—"),
    ],
)
def test_format_wind(kmh, direction, units, expected) -> None:
    assert format_wind(kmh, direction, units) == expected


@pytest.mark.parametrize(
    "temp,units,expected",
    [
        (Temperature(31, 40.6), "us", "88°F ⟨105°F⟩"),
        (Temperature(31, 31.2), "si", "31°C"),  # feels-like rounds to same -> omitted
        (Temperature(20, None), "us", "68°F"),  # no apparent temp
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
