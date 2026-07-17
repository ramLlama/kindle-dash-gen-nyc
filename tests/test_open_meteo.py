"""Tests for the Open-Meteo weather + air-quality source."""

import asyncio

import niquests_mock as nm
import pytest

from kindle_dash_gen.sources.builtins.open_meteo.model import wmo_description
from kindle_dash_gen.sources.builtins.open_meteo.source import (
    AQI_API,
    FORECAST_API,
    OpenMeteoClient,
    OpenMeteoError,
    _cardinal,
)

LAT, LON = 40.7484, -73.9857


def _forecast(time: str = "2026-07-01T14:00", code: int = 61) -> dict:
    """A forecast response; ``time`` is the current hour, ``code`` the current WMO weather code."""
    return {
        "current": {
            "time": time,
            "temperature_2m": 31,
            "apparent_temperature": 33,
            "relative_humidity_2m": 65,
            "dew_point_2m": 23.4,
            "precipitation": 0.5,  # > 0 → raining
            "weather_code": code,
            "wind_speed_10m": 7,
            "wind_direction_10m": 225,  # SW
        },
        "hourly": {
            # Whole-day arrays; the current hour (14:00) is excluded from the strip but supplies
            # "this hour" precip. Upcoming = 15:00–18:00.
            "time": [
                "2026-07-01T13:00",
                "2026-07-01T14:00",
                "2026-07-01T15:00",
                "2026-07-01T16:00",
                "2026-07-01T17:00",
                "2026-07-01T18:00",
            ],
            "temperature_2m": [30, 31, 32, 33, 32, 30],
            "apparent_temperature": [31, 33, 34, 35, 33, 31],
            "precipitation_probability": [10, 20, 30, 40, 50, 60],
            "weather_code": [3, 61, 2, 2, 63, 95],
        },
        "daily": {
            "time": ["2026-07-01", "2026-07-02"],
            "temperature_2m_max": [34, 38],
            "temperature_2m_min": [24, 26],
            "apparent_temperature_max": [35, 39],
            "apparent_temperature_min": [25, 27],
        },
    }


AQI = {"current": {"us_aqi": 110, "pm2_5": 43.8, "pm10": 45.1, "aerosol_optical_depth": 0.6}}


def _client() -> OpenMeteoClient:
    return OpenMeteoClient()


# niquests-mock matches the full URL incl. query unless `params` are given, in which case it
# subset-matches them and compares the base URL. Both endpoints send `timezone=auto`, so keying on
# that one param matches any request to each base URL without coupling to the exact query string.
_ANY_QUERY = {"timezone": "auto"}


def _route(router, forecast: dict | None = None, aqi: dict | None = None) -> None:
    router.get(FORECAST_API, params=_ANY_QUERY).respond(
        json=forecast if forecast is not None else _forecast()
    )
    router.get(AQI_API, params=_ANY_QUERY).respond(json=aqi if aqi is not None else AQI)


def test_fetch_parses_core_fields() -> None:
    with nm.mock(assert_all_called=False) as router:
        _route(router)
        r = asyncio.run(_client().fetch(LAT, LON))

    assert r.temperature.real == 31
    assert r.temperature.feels_like == 33
    assert r.weather_code == 61  # raw WMO code, not a lossy description
    assert r.humidity == 65
    assert r.dewpoint == 23.4  # raw float
    assert r.wind_speed_kmh == 7.0
    assert r.wind_direction == "SW"  # 225° → SW
    assert r.precip_probability == 20  # this hour (14:00)
    assert r.raining is True  # precipitation 0.5 > 0
    assert r.as_of.hour == 14


def test_upcoming_hours_excludes_current_hour() -> None:
    with nm.mock(assert_all_called=False) as router:
        _route(router)
        r = asyncio.run(_client().fetch(LAT, LON))
    # hourly_hours defaults to 4; the current hour (14:00) is excluded.
    assert [h.time.hour for h in r.hourly] == [15, 16, 17, 18]
    assert [h.temperature.real for h in r.hourly] == [32, 33, 32, 30]
    assert [h.temperature.feels_like for h in r.hourly] == [34, 35, 33, 31]
    assert [h.precip_probability for h in r.hourly] == [30, 40, 50, 60]
    assert r.hourly[0].weather_code == 2  # raw WMO code carried through


def test_high_low_today_before_rollover() -> None:
    with nm.mock(assert_all_called=False) as router:
        _route(router)
        r = asyncio.run(_client().fetch(LAT, LON))
    assert r.high.real == 34
    assert r.high.feels_like == 35  # apparent max
    assert r.low.real == 24
    assert r.low.feels_like == 25
    assert r.high_low_date.isoformat() == "2026-07-01"


def test_high_low_rolls_to_next_day_after_rollover() -> None:
    with nm.mock(assert_all_called=False) as router:
        _route(router, forecast=_forecast(time="2026-07-01T21:00"))  # past default rollover 20:00
        r = asyncio.run(_client().fetch(LAT, LON))
    assert r.high.real == 38
    assert r.low.real == 26
    assert r.high_low_date.isoformat() == "2026-07-02"


def test_air_quality_fields() -> None:
    with nm.mock(assert_all_called=False) as router:
        _route(router)
        r = asyncio.run(_client().fetch(LAT, LON))
    assert r.us_aqi == 110
    assert r.pm2_5 == 43.8
    assert r.pm10 == 45.1
    assert r.aerosol_optical_depth == 0.6


def test_air_quality_failure_degrades_gracefully() -> None:
    with nm.mock(assert_all_called=False) as router:
        router.get(FORECAST_API, params=_ANY_QUERY).respond(json=_forecast())
        router.get(AQI_API, params=_ANY_QUERY).respond(status_code=500)  # air quality unavailable
        r = asyncio.run(_client().fetch(LAT, LON))
    assert r.us_aqi is None
    assert r.pm2_5 is None
    assert r.aerosol_optical_depth is None
    assert r.temperature.real == 31  # core report still produced


def test_air_quality_null_current_degrades() -> None:
    # A 200 whose `current` is null must degrade to no-AQI, not raise an AttributeError.
    with nm.mock(assert_all_called=False) as router:
        router.get(FORECAST_API, params=_ANY_QUERY).respond(json=_forecast())
        router.get(AQI_API, params=_ANY_QUERY).respond(json={"current": None})
        r = asyncio.run(_client().fetch(LAT, LON))
    assert r.us_aqi is None
    assert r.temperature.real == 31  # core report still produced


def test_malformed_forecast_raises_open_meteo_error() -> None:
    # A null where an object/array is expected (a common malformed-JSON shape) must surface as
    # OpenMeteoError so the pipeline isolates it, not a raw TypeError that would sink the render.
    bad = _forecast()
    bad["hourly"] = None
    with nm.mock(assert_all_called=False) as router:
        router.get(FORECAST_API, params=_ANY_QUERY).respond(json=bad)
        router.get(AQI_API, params=_ANY_QUERY).respond(json=AQI)
        with pytest.raises(OpenMeteoError):
            asyncio.run(_client().fetch(LAT, LON))


def test_forecast_http_error_raises() -> None:
    with nm.mock(assert_all_called=False) as router:
        router.get(FORECAST_API, params=_ANY_QUERY).respond(status_code=500)
        router.get(AQI_API, params=_ANY_QUERY).respond(json=AQI)
        with pytest.raises(OpenMeteoError):
            asyncio.run(_client().fetch(LAT, LON))


@pytest.mark.parametrize(
    "degrees,expected",
    [(0, "N"), (90, "E"), (180, "S"), (225, "SW"), (270, "W"), (350, "N"), (None, "")],
)
def test_cardinal(degrees: float | None, expected: str) -> None:
    assert _cardinal(degrees) == expected


@pytest.mark.parametrize(
    "code,expected",
    [
        (0, "Clear sky"),
        (3, "Overcast"),
        (61, "Slight rain"),
        (75, "Heavy snowfall"),
        (999, "Unknown"),
    ],
)
def test_wmo_description(code: int, expected: str) -> None:
    assert wmo_description(code) == expected
