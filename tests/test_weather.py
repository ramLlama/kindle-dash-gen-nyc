"""Tests for the NWS weather source."""

from datetime import datetime

import niquests_mock as nm
import pytest

from kindle_dash_gen.models import Temperature
from kindle_dash_gen.sources.weather import NwsClient, WeatherError, _is_raining

LAT, LON = 40.7484, -73.9857
POINTS_URL = f"https://api.weather.gov/points/{LAT:.4f},{LON:.4f}"
FORECAST_URL = "https://api.weather.gov/gridpoints/OKX/34,44/forecast"
HOURLY_URL = "https://api.weather.gov/gridpoints/OKX/34,44/forecast/hourly"
GRID_URL = "https://api.weather.gov/gridpoints/OKX/34,44"
STATIONS_URL = "https://api.weather.gov/gridpoints/OKX/34,44/stations"
STATION_URL = "https://api.weather.gov/stations/KNYC"
OBS_URL = f"{STATION_URL}/observations/latest"

POINTS = {
    "properties": {
        "forecast": FORECAST_URL,
        "forecastHourly": HOURLY_URL,
        "forecastGridData": GRID_URL,
        "observationStations": STATIONS_URL,
        "relativeLocation": {"properties": {"city": "New York", "state": "NY"}},
    }
}


def _hour(start: str, temp: int, precip: int | None, short: str) -> dict:
    return {
        "startTime": start,
        "temperature": temp,
        "temperatureUnit": "C",
        "shortForecast": short,
        "probabilityOfPrecipitation": {"value": precip},
        "relativeHumidity": {"value": 65},
        "dewpoint": {"value": 23.4},
        "windSpeed": "7 km/h",
        "windDirection": "SW",
        "isDaytime": False,
    }


HOURLY = {
    "properties": {
        "periods": [
            _hour("2026-07-01T14:00:00-04:00", 31, 20, "Isolated Showers"),
            _hour("2026-07-01T15:00:00-04:00", 32, 30, "Partly Sunny"),
            _hour("2026-07-01T16:00:00-04:00", 33, 40, "Partly Sunny"),
            _hour("2026-07-01T17:00:00-04:00", 32, 50, "Showers"),
            _hour("2026-07-01T18:00:00-04:00", 30, 60, "Thunderstorms"),
        ]
    }
}


def _daily(name: str, start: str, end: str, daytime: bool, temp: int) -> dict:
    return {
        "name": name,
        "startTime": start,
        "endTime": end,
        "isDaytime": daytime,
        "temperature": temp,
        "temperatureUnit": "C",
        "shortForecast": "Partly Sunny" if daytime else "Isolated Showers",
    }


FORECAST = {
    "properties": {
        "periods": [
            _daily(
                "This Afternoon", "2026-07-01T14:00:00-04:00", "2026-07-01T18:00:00-04:00", True, 34
            ),  # noqa: E501
            _daily("Tonight", "2026-07-01T18:00:00-04:00", "2026-07-02T06:00:00-04:00", False, 24),
            _daily("Thursday", "2026-07-02T06:00:00-04:00", "2026-07-02T18:00:00-04:00", True, 38),
            _daily(
                "Thursday Night",
                "2026-07-02T18:00:00-04:00",
                "2026-07-03T06:00:00-04:00",
                False,
                26,
            ),  # noqa: E501
        ]
    }
}

# Apparent-temperature windows in UTC (14:00 EDT == 18:00 UTC), one per upcoming hour.
GRID = {
    "properties": {
        "apparentTemperature": {
            "uom": "wmoUnit:degC",
            "values": [
                {"validTime": "2026-07-01T18:00:00+00:00/PT1H", "value": 40.6},  # 14:00 EDT (now)
                {"validTime": "2026-07-01T19:00:00+00:00/PT1H", "value": 42.0},  # 15:00 EDT
                {"validTime": "2026-07-01T20:00:00+00:00/PT1H", "value": 41.0},  # 16:00 EDT
                {"validTime": "2026-07-01T21:00:00+00:00/PT1H", "value": 39.0},  # 17:00 EDT
                {"validTime": "2026-07-01T22:00:00+00:00/PT1H", "value": 37.0},  # 18:00 EDT
            ],
        }
    }
}

STATIONS = {"features": [{"id": STATION_URL, "properties": {"stationIdentifier": "KNYC"}}]}


def _obs(present_weather: list[dict], text: str) -> dict:
    return {"properties": {"presentWeather": present_weather, "textDescription": text}}


def _route_all(router, obs=None) -> None:
    router.get(POINTS_URL).respond(json=POINTS)
    router.get(HOURLY_URL, params={"units": "si"}).respond(json=HOURLY)
    router.get(FORECAST_URL, params={"units": "si"}).respond(json=FORECAST)
    router.get(GRID_URL).respond(json=GRID)
    router.get(STATIONS_URL).respond(json=STATIONS)
    router.get(OBS_URL).respond(json=obs if obs is not None else _obs([], "Clear"))


def _client() -> NwsClient:
    return NwsClient("test-agent (t@example.com)")


def test_fetch_parses_core_fields() -> None:
    with nm.mock(assert_all_called=False) as router:
        _route_all(router)
        r = _client().fetch(LAT, LON)

    assert r.temperature.real == 31
    assert r.temperature.feels_like == 40.6  # raw float, not rounded
    assert r.conditions == "Isolated Showers"
    assert r.humidity == 65
    assert r.dewpoint == 23.4  # raw float
    assert r.wind_speed_kmh == 7.0
    assert r.wind_direction == "SW"
    assert r.precip_probability == 20
    assert r.location_name == "New York, NY"
    assert r.as_of.hour == 14


def test_upcoming_hours_excludes_current_hour() -> None:
    with nm.mock(assert_all_called=False) as router:
        _route_all(router)
        r = _client().fetch(LAT, LON)
    # hourly_hours defaults to 4; the current hour (14:00) is excluded.
    assert [h.time.hour for h in r.hourly] == [15, 16, 17, 18]
    assert [h.temperature.real for h in r.hourly] == [32, 33, 32, 30]
    assert [h.temperature.feels_like for h in r.hourly] == [42.0, 41.0, 39.0, 37.0]
    assert r.hourly[0].precip_probability == 30


def test_raining_from_present_weather() -> None:
    with nm.mock(assert_all_called=False) as router:
        _route_all(router, obs=_obs([{"weather": "rain"}], "Light Rain"))
        r = _client().fetch(LAT, LON)
    assert r.raining is True
    assert r.observed_conditions == "Light Rain"


def test_not_raining_when_clear() -> None:
    with nm.mock(assert_all_called=False) as router:
        _route_all(router, obs=_obs([], "Clear"))
        r = _client().fetch(LAT, LON)
    assert r.raining is False


def test_observation_failure_degrades_gracefully() -> None:
    with nm.mock(assert_all_called=False) as router:
        router.get(POINTS_URL).respond(json=POINTS)
        router.get(HOURLY_URL, params={"units": "si"}).respond(json=HOURLY)
        router.get(FORECAST_URL, params={"units": "si"}).respond(json=FORECAST)
        router.get(GRID_URL).respond(json=GRID)
        router.get(STATIONS_URL).respond(status_code=500)  # observation unavailable
        r = _client().fetch(LAT, LON)
    assert r.raining is None
    assert r.observed_conditions is None
    assert r.temperature.real == 31  # core report still produced


# Apparent windows within This Afternoon (max -> high) and Tonight (min -> low).
_HL_APPARENT = [
    (datetime.fromisoformat("2026-07-01T19:00:00+00:00"), 40.6),  # afternoon
    (datetime.fromisoformat("2026-07-01T20:00:00+00:00"), 42.0),  # afternoon (max)
    (datetime.fromisoformat("2026-07-01T23:00:00+00:00"), 30.0),  # tonight
    (datetime.fromisoformat("2026-07-02T05:00:00+00:00"), 26.5),  # tonight (min)
]


def test_high_low_uses_today_before_rollover() -> None:
    periods = FORECAST["properties"]["periods"]
    before = datetime.fromisoformat("2026-07-01T15:00:00-04:00")
    high, low, day = _client()._high_low(periods, before, _HL_APPARENT)
    assert high == Temperature(34, 42.0)  # actual high, apparent = window max
    assert low == Temperature(24, 26.5)  # actual low, apparent = window min
    assert day.isoformat() == "2026-07-01"


def test_high_low_rolls_to_next_day_after_rollover() -> None:
    periods = FORECAST["properties"]["periods"]
    after = datetime.fromisoformat("2026-07-01T21:00:00-04:00")  # past default rollover 20:00
    high, low, day = _client()._high_low(periods, after, [])  # no apparent data
    assert high == Temperature(38, None)
    assert low == Temperature(26, None)
    assert day.isoformat() == "2026-07-02"


def test_http_error_raises_weather_error() -> None:
    with nm.mock(assert_all_called=False) as router:
        router.get(POINTS_URL).respond(status_code=500)
        with pytest.raises(WeatherError):
            _client().fetch(LAT, LON)


def test_sends_user_agent_header() -> None:
    with nm.mock(assert_all_called=False) as router:
        points_route = router.get(POINTS_URL).respond(json=POINTS)
        router.get(HOURLY_URL, params={"units": "si"}).respond(json=HOURLY)
        router.get(FORECAST_URL, params={"units": "si"}).respond(json=FORECAST)
        router.get(GRID_URL).respond(json=GRID)
        router.get(STATIONS_URL).respond(json=STATIONS)
        router.get(OBS_URL).respond(json=_obs([], "Clear"))
        NwsClient("my-agent (t@example.com)").fetch(LAT, LON)
        assert points_route.calls[-1].request.headers["User-Agent"] == "my-agent (t@example.com)"


@pytest.mark.parametrize(
    "present,text,expected",
    [
        ([{"weather": "thunderstorm"}], "Thunderstorm", True),
        ([{"weather": "fog"}], "Fog", False),  # present weather, but not precip
        ([], "Light Drizzle", True),  # fall back to text
        ([], "Mostly Cloudy", False),
    ],
)
def test_is_raining(present: list, text: str, expected: bool) -> None:
    assert _is_raining(present, text) is expected
