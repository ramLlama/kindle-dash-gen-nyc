"""Tests for the NWS weather source."""

import asyncio
from datetime import date, datetime

import niquests_mock as nm
import pytest
from pydantic import ValidationError

from kindle_dash_gen.sources.builtins.nws.model import Temperature
from kindle_dash_gen.sources.builtins.nws.source import (
    NwsClient,
    NwsConfig,
    WeatherError,
    _day_high_low,
    _is_raining,
)

LAT, LON = 40.7484, -73.9857
POINTS_URL = f"https://api.weather.gov/points/{LAT:.4f},{LON:.4f}"
FORECAST_URL = "https://api.weather.gov/gridpoints/OKX/34,44/forecast"
HOURLY_URL = "https://api.weather.gov/gridpoints/OKX/34,44/forecast/hourly"
GRID_URL = "https://api.weather.gov/gridpoints/OKX/34,44"
STATIONS_URL = "https://api.weather.gov/gridpoints/OKX/34,44/stations"
STATION_URL = "https://api.weather.gov/stations/KNYC"
OBS_URL = f"{STATION_URL}/observations/latest"
ALERTS_URL = "https://api.weather.gov/alerts/active"

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

# One fully-populated alert plus a sparse one (null free-text/timestamps, missing CAP fields).
ALERTS = {
    "features": [
        {
            "properties": {
                "event": "Flash Flood Warning",
                "category": "Met",
                "severity": "Severe",
                "certainty": "Likely",
                "urgency": "Immediate",
                "status": "Actual",
                "messageType": "Alert",
                "areaDesc": "Elko, NV",
                "senderName": "NWS Elko NV",
                "headline": "Flash Flood Warning until 9:30PM EDT",
                "description": "* WHAT...Flash flooding.\n* WHERE...Elko.",
                "instruction": "Turn around, don't drown.",
                "response": "Avoid",
                "effective": "2026-07-01T14:00:00-04:00",
                "onset": "2026-07-01T14:02:00-04:00",
                "expires": "2026-07-01T21:30:00-04:00",
                "ends": "2026-07-01T21:30:00-04:00",
            }
        },
        {
            "properties": {
                "event": "Special Weather Statement",
                "severity": "Minor",
                "headline": None,
                "onset": None,
                "expires": "2026-07-01T18:00:00-04:00",
            }
        },
    ]
}
NO_ALERTS = {"features": []}


def _obs(present_weather: list[dict], text: str) -> dict:
    return {"properties": {"presentWeather": present_weather, "textDescription": text}}


def _route_all(router, obs=None, alerts=None) -> None:
    router.get(POINTS_URL).respond(json=POINTS)
    router.get(HOURLY_URL, params={"units": "si"}).respond(json=HOURLY)
    router.get(FORECAST_URL, params={"units": "si"}).respond(json=FORECAST)
    router.get(GRID_URL).respond(json=GRID)
    router.get(STATIONS_URL).respond(json=STATIONS)
    router.get(OBS_URL).respond(json=obs if obs is not None else _obs([], "Clear"))
    # niquests-mock matches full URL incl. query; pass `params` to match by base URL only.
    router.get(ALERTS_URL, params={"point": f"{LAT:.4f},{LON:.4f}"}).respond(
        json=alerts if alerts is not None else NO_ALERTS
    )


def _client() -> NwsClient:
    return NwsClient("test-agent (t@example.com)")


def test_fetch_parses_core_fields() -> None:
    with nm.mock(assert_all_called=False) as router:
        _route_all(router)
        r = asyncio.run(_client().fetch(LAT, LON))

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
        r = asyncio.run(_client().fetch(LAT, LON))
    # hourly_hours defaults to 4; the current hour (14:00) is excluded.
    assert [h.time.hour for h in r.hourly] == [15, 16, 17, 18]
    assert [h.temperature.real for h in r.hourly] == [32, 33, 32, 30]
    assert [h.temperature.feels_like for h in r.hourly] == [42.0, 41.0, 39.0, 37.0]
    assert r.hourly[0].precip_probability == 30


def test_raining_from_present_weather() -> None:
    with nm.mock(assert_all_called=False) as router:
        _route_all(router, obs=_obs([{"weather": "rain"}], "Light Rain"))
        r = asyncio.run(_client().fetch(LAT, LON))
    assert r.raining is True
    assert r.observed_conditions == "Light Rain"


def test_not_raining_when_clear() -> None:
    with nm.mock(assert_all_called=False) as router:
        _route_all(router, obs=_obs([], "Clear"))
        r = asyncio.run(_client().fetch(LAT, LON))
    assert r.raining is False


def test_observation_failure_degrades_gracefully() -> None:
    with nm.mock(assert_all_called=False) as router:
        router.get(POINTS_URL).respond(json=POINTS)
        router.get(HOURLY_URL, params={"units": "si"}).respond(json=HOURLY)
        router.get(FORECAST_URL, params={"units": "si"}).respond(json=FORECAST)
        router.get(GRID_URL).respond(json=GRID)
        router.get(STATIONS_URL).respond(status_code=500)  # observation unavailable
        router.get(ALERTS_URL, params={"point": f"{LAT:.4f},{LON:.4f}"}).respond(json=NO_ALERTS)
        r = asyncio.run(_client().fetch(LAT, LON))
    assert r.raining is None
    assert r.observed_conditions is None
    assert r.temperature.real == 31  # core report still produced


def test_alerts_parsed() -> None:
    with nm.mock(assert_all_called=False) as router:
        _route_all(router, alerts=ALERTS)
        r = asyncio.run(_client().fetch(LAT, LON))
    assert [a.event for a in r.alerts] == ["Flash Flood Warning", "Special Weather Statement"]
    first = r.alerts[0]
    assert first.category == "Met"
    assert first.severity == "Severe"
    assert first.certainty == "Likely"
    assert first.urgency == "Immediate"
    assert first.status == "Actual"
    assert first.message_type == "Alert"
    assert first.area_desc == "Elko, NV"
    assert first.sender_name == "NWS Elko NV"
    assert first.headline == "Flash Flood Warning until 9:30PM EDT"
    assert first.description == "* WHAT...Flash flooding.\n* WHERE...Elko."
    assert first.instruction == "Turn around, don't drown."
    assert first.response == "Avoid"
    assert first.effective == datetime.fromisoformat("2026-07-01T14:00:00-04:00")
    assert first.onset == datetime.fromisoformat("2026-07-01T14:02:00-04:00")
    assert first.expires == datetime.fromisoformat("2026-07-01T21:30:00-04:00")
    assert first.ends == datetime.fromisoformat("2026-07-01T21:30:00-04:00")
    # A sparse alert: null free-text degrades to None, missing CAP fields to their defaults.
    second = r.alerts[1]
    assert second.headline is None
    assert second.description is None
    assert second.onset is None
    assert second.effective is None  # key absent entirely
    assert second.category == "Unknown"  # missing classification -> CAP "Unknown"
    assert second.area_desc == ""  # missing text -> empty


def test_malformed_alert_feature_is_skipped() -> None:
    # A feature missing the required `event` key is dropped; valid siblings survive.
    malformed = {
        "features": [
            {"properties": {"severity": "Severe"}},  # no "event" -> skipped
            ALERTS["features"][0],  # valid Flash Flood Warning -> kept
        ]
    }
    with nm.mock(assert_all_called=False) as router:
        _route_all(router, alerts=malformed)
        r = asyncio.run(_client().fetch(LAT, LON))
    assert [a.event for a in r.alerts] == ["Flash Flood Warning"]


def test_no_active_alerts() -> None:
    with nm.mock(assert_all_called=False) as router:
        _route_all(router)  # NO_ALERTS by default
        r = asyncio.run(_client().fetch(LAT, LON))
    assert r.alerts == []


def test_alerts_failure_degrades_gracefully() -> None:
    with nm.mock(assert_all_called=False) as router:
        router.get(POINTS_URL).respond(json=POINTS)
        router.get(HOURLY_URL, params={"units": "si"}).respond(json=HOURLY)
        router.get(FORECAST_URL, params={"units": "si"}).respond(json=FORECAST)
        router.get(GRID_URL).respond(json=GRID)
        router.get(STATIONS_URL).respond(json=STATIONS)
        router.get(OBS_URL).respond(json=_obs([], "Clear"))
        router.get(ALERTS_URL, params={"point": f"{LAT:.4f},{LON:.4f}"}).respond(status_code=500)
        r = asyncio.run(_client().fetch(LAT, LON))
    assert r.alerts == []  # alert-endpoint failure isolated
    assert r.temperature.real == 31  # core report still produced


# Apparent windows within This Afternoon (max -> high) and Tonight (min -> low).
_HL_APPARENT = [
    (datetime.fromisoformat("2026-07-01T19:00:00+00:00"), 40.6),  # afternoon
    (datetime.fromisoformat("2026-07-01T20:00:00+00:00"), 42.0),  # afternoon (max)
    (datetime.fromisoformat("2026-07-01T23:00:00+00:00"), 30.0),  # tonight
    (datetime.fromisoformat("2026-07-02T05:00:00+00:00"), 26.5),  # tonight (min)
]


def test_day_high_low_pairs_the_days_daytime_and_nighttime_periods() -> None:
    periods = FORECAST["properties"]["periods"]
    day = date.fromisoformat("2026-07-01")
    result = _day_high_low(periods, day, _HL_APPARENT)
    assert result.day == day
    assert result.high == Temperature(34, 42.0)  # actual high, apparent = window max
    assert result.low == Temperature(24, 26.5)  # actual low, apparent = window min


def test_day_high_low_without_apparent_data() -> None:
    periods = FORECAST["properties"]["periods"]
    result = _day_high_low(periods, date.fromisoformat("2026-07-02"), [])
    assert result.high == Temperature(38, None)
    assert result.low == Temperature(26, None)


def test_day_high_low_is_none_for_a_day_not_in_the_feed() -> None:
    periods = FORECAST["properties"]["periods"]
    result = _day_high_low(periods, date.fromisoformat("2026-07-09"), [])
    assert result.day == date.fromisoformat("2026-07-09")
    assert result.high is None
    assert result.low is None


def test_expired_daytime_period_does_not_borrow_tomorrows_high() -> None:
    """The evening case: today's daytime period has dropped out of the feed.

    Today's high is then genuinely unknown. Reporting ``None`` is honest; the previous
    fall-forward behavior returned *tomorrow's* 38 labelled with today's date.
    """
    # Feed starting at "Tonight" — today's daytime period is gone, its night period remains.
    evening = FORECAST["properties"]["periods"][1:]
    today = _day_high_low(evening, date.fromisoformat("2026-07-01"), [])
    assert today.high is None  # not 38 borrowed from tomorrow
    assert today.low == Temperature(24, None)  # tonight's low still known
    tomorrow = _day_high_low(evening, date.fromisoformat("2026-07-02"), [])
    assert tomorrow.high == Temperature(38, None)
    assert tomorrow.low == Temperature(26, None)


def test_fetch_anchors_today_on_the_current_hour_not_the_first_daily_period() -> None:
    # Drives the evening feed end-to-end. "Today" comes from the hourly period's local date, so a
    # nighttime-only first daily period cannot shift the anchor (it would under `_pdate(first)` if
    # NWS ever stopped truncating an in-progress period's startTime).
    evening_forecast = {"properties": {"periods": FORECAST["properties"]["periods"][1:]}}
    with nm.mock(assert_all_called=False) as router:
        _route_all(router)
        router.get(FORECAST_URL, params={"units": "si"}).respond(json=evening_forecast)
        r = asyncio.run(_client().fetch(LAT, LON))
    assert r.today.day.isoformat() == "2026-07-01"
    assert r.today.high is None
    assert r.today.low.real == 24
    assert r.tomorrow.day.isoformat() == "2026-07-02"
    assert r.tomorrow.high.real == 38


def test_fetch_returns_both_days_high_low() -> None:
    # The source makes no display decision about which day to show: it reports both, keyed off the
    # first daily period the feed returns, and a layout picks (see docs/sources.md).
    with nm.mock(assert_all_called=False) as router:
        _route_all(router)
        r = asyncio.run(_client().fetch(LAT, LON))
    assert r.today.day.isoformat() == "2026-07-01"
    assert r.today.high.real == 34
    assert r.today.low.real == 24
    assert r.tomorrow.day.isoformat() == "2026-07-02"
    assert r.tomorrow.high.real == 38
    assert r.tomorrow.low.real == 26


def test_rollover_hour_is_rejected_as_unknown_config() -> None:
    # The knob is gone, and NwsConfig stays extra="forbid", so a stale config fails fast.
    with pytest.raises(ValidationError):
        NwsConfig(latitude=LAT, longitude=LON, user_agent="x", rollover_hour=20)


def test_http_error_raises_weather_error() -> None:
    with nm.mock(assert_all_called=False) as router:
        router.get(POINTS_URL).respond(status_code=500)
        with pytest.raises(WeatherError):
            asyncio.run(_client().fetch(LAT, LON))


def test_sends_user_agent_header() -> None:
    with nm.mock(assert_all_called=False) as router:
        points_route = router.get(POINTS_URL).respond(json=POINTS)
        router.get(HOURLY_URL, params={"units": "si"}).respond(json=HOURLY)
        router.get(FORECAST_URL, params={"units": "si"}).respond(json=FORECAST)
        router.get(GRID_URL).respond(json=GRID)
        router.get(STATIONS_URL).respond(json=STATIONS)
        router.get(OBS_URL).respond(json=_obs([], "Clear"))
        router.get(ALERTS_URL, params={"point": f"{LAT:.4f},{LON:.4f}"}).respond(json=NO_ALERTS)
        asyncio.run(NwsClient("my-agent (t@example.com)").fetch(LAT, LON))
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
