"""Tests for the OpenRouter dashboard prompt renderer."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from kindle_dash_gen_nyc.models import (
    DashboardData,
    Direction,
    HourlyForecast,
    StationBoard,
    Temperature,
    TrainArrival,
    WeatherReport,
)
from kindle_dash_gen_nyc.render.prompt import render_prompt

NOW = datetime(2026, 7, 1, 14, 0, 0)

_MISSING = object()  # sentinel distinguishing "use the default" from an explicit None/[]


def _weather() -> WeatherReport:
    return WeatherReport(
        temperature=Temperature(30.0, 32.0),
        conditions="Partly Sunny",
        humidity=65,
        dewpoint=20.0,
        wind_speed_kmh=10.0,
        wind_direction="SW",
        precip_probability=20,
        raining=False,
        observed_conditions="Clear",
        high=Temperature(34.0, 36.0),
        low=Temperature(24.0, None),
        high_low_date=date(2026, 7, 1),
        forecast="Sunny with a chance of showers.",
        forecast_name="This Afternoon",
        hourly=[
            HourlyForecast(
                time=NOW + timedelta(hours=1),
                temperature=Temperature(31.0, 33.0),
                conditions="Sunny",
                precip_probability=10,
            ),
            HourlyForecast(
                time=NOW + timedelta(hours=2),
                temperature=Temperature(32.0, None),
                conditions="Partly Cloudy",
                precip_probability=None,
            ),
        ],
        as_of=NOW,
        location_name="New York, NY",
    )


def _boards() -> list[StationBoard]:
    return [
        StationBoard(
            name="Union Sq",
            arrivals_by_direction={
                Direction.NORTH: [
                    TrainArrival(
                        route="N",
                        direction=Direction.NORTH,
                        destination="Astoria",
                        arrival=NOW + timedelta(minutes=3),
                    ),
                ],
                Direction.SOUTH: [
                    TrainArrival(
                        route="R",
                        direction=Direction.SOUTH,
                        destination="Bay Ridge",
                        arrival=NOW + timedelta(minutes=5),
                    ),
                ],
            },
        ),
        StationBoard(
            name="Astor Pl",
            arrivals_by_direction={
                Direction.NORTH: [
                    TrainArrival(
                        route="6",
                        direction=Direction.NORTH,
                        destination="Pelham Bay Park",
                        arrival=NOW + timedelta(minutes=8),
                    ),
                ],
            },
        ),
    ]


def _dashboard(weather=_MISSING, boards=_MISSING) -> DashboardData:
    return DashboardData(
        weather=_weather() if weather is _MISSING else weather,
        boards=_boards() if boards is _MISSING else boards,
        generated_at=NOW,
    )


def test_dense_prompt_renders() -> None:
    # The prompt wording is an iterating artifact; assert only that it renders non-empty.
    prompt = render_prompt(_dashboard(), units="us", width=1072, height=1448, aspect="3:4")

    assert isinstance(prompt, str)
    assert len(prompt) > 0


def test_override_template_path_is_used(tmp_path: Path) -> None:
    custom = tmp_path / "custom.j2"
    custom.write_text("CUSTOM {{ width }}x{{ height }} temp={{ format_temp(20.0, units) }}")

    prompt = render_prompt(
        _dashboard(), units="si", width=800, height=600, aspect="4:3", template=str(custom)
    )

    assert prompt == "CUSTOM 800x600 temp=20°C"


def test_unknown_template_spec_raises() -> None:
    with pytest.raises(ValueError):
        render_prompt(
            _dashboard(), units="us", width=1448, height=1072, aspect="4:3", template="nope"
        )


def test_handles_missing_weather_and_empty_boards() -> None:
    data = _dashboard(
        weather=None,
        boards=[StationBoard(name="Empty St", arrivals_by_direction={})],
    )

    prompt = render_prompt(data, units="us", width=1072, height=1448, aspect="3:4")

    assert isinstance(prompt, str)
    assert len(prompt) > 0
    assert "Empty St" in prompt
