"""Tests for display formatting helpers."""

from datetime import date, datetime, timedelta

import pytest

from kindle_dash_gen.format import (
    format_eta,
    format_reading,
    format_temp,
    format_wind,
    weather_icon,
)
from kindle_dash_gen.models import Temperature, WeatherReport

_NOW = datetime(2026, 7, 1, 12, 0, 0)


def _report(*, conditions: str, observed: str | None, raining: bool | None) -> WeatherReport:
    """Minimal WeatherReport carrying only the fields weather_icon reads."""
    return WeatherReport(
        temperature=Temperature(20.0, None),
        conditions=conditions,
        humidity=None,
        dewpoint=None,
        wind_speed_kmh=None,
        wind_direction="",
        precip_probability=None,
        raining=raining,
        observed_conditions=observed,
        high=None,
        low=None,
        high_low_date=date(2026, 7, 1),
        forecast="",
        forecast_name="",
        hourly=[],
        as_of=_NOW,
    )


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
    report = _report(conditions=conditions, observed=observed, raining=raining)
    assert weather_icon(report) == expected
