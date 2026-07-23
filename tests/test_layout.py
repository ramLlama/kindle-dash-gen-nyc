"""Integration tests for the Pillow rendering backend.

These render real ``DashboardData`` through the layout and assert output invariants (exact panel
size, grayscale mode, determinism, graceful degradation). Glyph-level appearance is not asserted;
it depends on the system font and is an iterating visual concern.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from unittest import mock

import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError

from kindle_dash_gen.models import DashboardData
from kindle_dash_gen.render.builtins.glanceable import (
    _BADGE_GAP,
    _BADGE_INSET,
    _BADGE_MARGIN,
    _BADGE_MIN_WIDTH,
    _CLOCK_INDENT,
    _GUTTER,
    _MARGIN,
    _MAX_TRANSIT_BOARDS,
    _cap_height,
    _cap_midline,
    _direction_label,
    _direction_rank,
    _from_mta,
    _from_sf_bay_511,
    _transit,
)
from kindle_dash_gen.render.layout import LayoutError, render
from kindle_dash_gen.render.toolkit import INK, PAPER, Fonts, _resolve_face, fit_font
from kindle_dash_gen.sources.builtins.mta.model import (
    Direction,
    MtaData,
    StationBoard,
    TrainArrival,
)
from kindle_dash_gen.sources.builtins.nws.model import (
    DailyHighLow,
    HourlyForecast,
    LocationWeather,
    NwsData,
    Temperature,
    WeatherAlert,
)
from kindle_dash_gen.sources.builtins.open_meteo import model as om
from kindle_dash_gen.sources.builtins.sf_bay_511 import model as sf

NOW = datetime(2026, 7, 3, 0, 30, 0, tzinfo=UTC)  # 2026-07-02 20:30 EDT
LOCATION = "home"
W, H = 1072, 1448

_MISSING = object()  # distinguishes "use default" from an explicit None/[]


def _weather() -> LocationWeather:
    return LocationWeather(
        temperature=Temperature(41.0, 44.0),
        conditions="Clear",
        humidity=40,
        dewpoint=18.0,
        wind_speed_kmh=13.0,
        wind_direction="SW",
        precip_probability=0,
        raining=False,
        observed_conditions="Clear",
        today=DailyHighLow(
            day=date(2026, 7, 2), high=Temperature(42.0, 45.0), low=Temperature(30.0, None)
        ),
        tomorrow=DailyHighLow(
            day=date(2026, 7, 3), high=Temperature(40.0, None), low=Temperature(28.0, None)
        ),
        forecast="Clear",
        forecast_name="Tonight",
        hourly=[
            HourlyForecast(
                time=NOW + timedelta(hours=h),
                temperature=Temperature(40.0 - h, None),
                conditions="Clear",
                precip_probability=None if h == 3 else h,
            )
            for h in range(1, 5)
        ],
        as_of=NOW,
    )


def _open_meteo_weather() -> om.LocationWeather:
    """An OpenMeteoData carrying the same drawn values as :func:`_weather` (NwsData).

    Mirrors every field the glanceable weather section renders (current apparent temp, wind, the
    icon-resolving conditions text, and the hourly strip) so the two providers must render
    identically; the extra AQI fields are unset.
    """
    return om.LocationWeather(
        temperature=om.Temperature(41.0, 44.0),
        weather_code=0,  # Clear → resolves to the same "sunny" icon as the NWS fixture
        humidity=40,
        dewpoint=18.0,
        wind_speed_kmh=13.0,
        wind_direction="SW",
        precip_probability=0,
        raining=False,
        today=om.DailyHighLow(
            day=date(2026, 7, 2), high=om.Temperature(42.0, 45.0), low=om.Temperature(30.0, None)
        ),
        tomorrow=om.DailyHighLow(
            day=date(2026, 7, 3), high=om.Temperature(40.0, None), low=om.Temperature(28.0, None)
        ),
        hourly=[
            om.HourlyForecast(
                time=NOW + timedelta(hours=h),
                temperature=om.Temperature(40.0 - h, None),
                weather_code=0,
                precip_probability=None if h == 3 else h,
            )
            for h in range(1, 5)
        ],
        as_of=NOW,
        us_aqi=None,
        pm2_5=None,
        pm10=None,
        aerosol_optical_depth=None,
    )


def _boards() -> list[StationBoard]:
    def arr(route: str, direction: Direction, mins: int) -> TrainArrival:
        return TrainArrival(
            route=route, direction=direction, destination="", arrival=NOW + timedelta(minutes=mins)
        )

    return [
        StationBoard(
            name="57 St-6 Av",
            arrivals_by_direction={
                Direction.NORTH: [arr("M", Direction.NORTH, m) for m in (5, 12, 20)],
                Direction.SOUTH: [arr("M", Direction.SOUTH, m) for m in (3, 11)],
            },
        ),
        StationBoard(
            name="57 St-7 Av",
            arrivals_by_direction={
                Direction.NORTH: [arr(r, Direction.NORTH, m) for r, m in (("R", 2), ("Q", 4))],
                Direction.SOUTH: [],
            },
        ),
    ]


def _dashboard(weather=_MISSING, boards=_MISSING) -> DashboardData:
    """Build DashboardData from source_data; a None weather or absent boards omits that key."""
    w = _nws_data() if weather is _MISSING else weather
    b = _boards() if boards is _MISSING else boards
    source_data: dict[type, object] = {}
    if w is not None:
        source_data[NwsData] = w
    if b is not None:
        source_data[MtaData] = MtaData(boards=b)
    return DashboardData(generated_at=NOW, source_data=source_data)


def _nws_data(inner: LocationWeather | None = None) -> NwsData:
    return NwsData(locations={LOCATION: inner if inner is not None else _weather()})


def _om_data(inner: om.LocationWeather | None = None) -> om.OpenMeteoData:
    return om.OpenMeteoData(
        locations={LOCATION: inner if inner is not None else _open_meteo_weather()}
    )


_CONFIG = {
    "title": "NYC",
    "timezone": "America/New_York",
    "weather_location": LOCATION,
    "font": "Adwaita Sans",
    "weather_temp_units": "both",
}


def _render(data: DashboardData) -> Image.Image:
    return render(data, width=W, height=H, layout="glanceable", layout_config=_CONFIG)


def test_renders_kindle_sized_grayscale() -> None:
    img = _render(_dashboard())
    assert img.size == (W, H)
    assert img.mode == "L"


def test_render_is_deterministic() -> None:
    data = _dashboard()
    first = _render(data)
    second = _render(data)
    assert first.tobytes() == second.tobytes()


def test_open_meteo_renders_identically_to_nws() -> None:
    # The glanceable adapter fully normalizes each provider, so equivalent NWS and Open-Meteo data
    # produce a byte-identical dashboard — provider choice is invisible at the pixel level.
    boards = MtaData(boards=_boards())
    nws = DashboardData(generated_at=NOW, source_data={NwsData: _nws_data(), MtaData: boards})
    open_meteo = DashboardData(
        generated_at=NOW, source_data={om.OpenMeteoData: _om_data(), MtaData: boards}
    )
    assert _render(nws).tobytes() == _render(open_meteo).tobytes()


def test_adapter_prefers_open_meteo_when_both_present() -> None:
    # With both weather sources configured, the adapter renders Open-Meteo (the global provider).
    from kindle_dash_gen.render.builtins.glanceable import _weather as adapt

    nws = _weather()  # wind "SW"
    open_meteo = om.LocationWeather(
        temperature=om.Temperature(10.0, 10.0),
        weather_code=3,  # Overcast → "cloudy" icon
        humidity=None,
        dewpoint=None,
        wind_speed_kmh=99.0,
        wind_direction="NE",  # distinct from the NWS fixture's "SW"
        precip_probability=None,
        raining=None,
        today=DailyHighLow(day=date(2026, 7, 2), high=None, low=None),
        tomorrow=DailyHighLow(day=date(2026, 7, 3), high=None, low=None),
        hourly=[],
        as_of=NOW,
        us_aqi=None,
        pm2_5=None,
        pm10=None,
        aerosol_optical_depth=None,
    )
    data = DashboardData(
        generated_at=NOW,
        source_data={NwsData: _nws_data(nws), om.OpenMeteoData: _om_data(open_meteo)},
    )
    resolved = adapt(data, LOCATION)
    assert resolved is not None
    assert resolved.wind_direction == "NE"  # Open-Meteo won
    assert resolved.icon == "cloudy"  # from Open-Meteo's "Overcast"


@pytest.mark.parametrize(
    "code,icon",
    [
        (0, "sunny"),  # clear
        (1, "sunny"),  # mainly clear
        (3, "cloudy"),  # overcast
        (45, "cloudy"),  # fog
        (61, "rain"),
        (82, "rain"),  # violent rain showers
        (95, "rain"),  # thunderstorm
        (75, "snow"),
        (86, "snow"),  # snow showers
        (999, "sunny"),  # unknown code falls through
    ],
)
def test_wmo_icon_classification(code: int, icon: str) -> None:
    from kindle_dash_gen.render.builtins.glanceable import _wmo_icon

    assert _wmo_icon(code) == icon


def _alert(event: str, severity: str) -> WeatherAlert:
    return WeatherAlert(
        event=event,
        category="Met",
        severity=severity,
        certainty="Likely",
        urgency="Immediate",
        status="Actual",
        message_type="Alert",
        area_desc="NYC",
        sender_name="NWS",
        headline=None,
        description=None,
        instruction=None,
        response=None,
        effective=None,
        onset=None,
        expires=None,
        ends=None,
    )


def test_adapter_combines_aqi_and_alerts_across_providers() -> None:
    # AQI comes from Open-Meteo, alerts from NWS — both surface even though Open-Meteo drives the
    # hero. Alerts are ordered most-severe-first regardless of source order.
    from kindle_dash_gen.render.builtins.glanceable import _weather as adapt

    om_weather = replace(_open_meteo_weather(), us_aqi=110)
    nws = replace(
        _weather(), alerts=[_alert("Heat Advisory", "Moderate"), _alert("Tornado", "Extreme")]
    )
    data = DashboardData(
        generated_at=NOW,
        source_data={NwsData: _nws_data(nws), om.OpenMeteoData: _om_data(om_weather)},
    )
    resolved = adapt(data, LOCATION)
    assert resolved is not None
    assert resolved.aqi == 110  # from Open-Meteo
    assert resolved.alerts == ("Tornado", "Heat Advisory")  # Extreme before Moderate


def test_adapter_omits_missing_provider_fields() -> None:
    # NWS-only: no AQI (Open-Meteo absent). Open-Meteo-only: no alerts (NWS absent).
    from kindle_dash_gen.render.builtins.glanceable import _weather as adapt

    nws_only = DashboardData(generated_at=NOW, source_data={NwsData: _nws_data()})
    resolved = adapt(nws_only, LOCATION)
    assert resolved is not None
    assert resolved.aqi is None
    assert resolved.alerts == ()

    om_only = DashboardData(
        generated_at=NOW,
        source_data={om.OpenMeteoData: _om_data(replace(_open_meteo_weather(), us_aqi=42))},
    )
    resolved = adapt(om_only, LOCATION)
    assert resolved is not None
    assert resolved.aqi == 42
    assert resolved.alerts == ()


def test_renders_with_aqi_and_alerts() -> None:
    # The extra hero rows must not break the full-size render.
    om_weather = replace(_open_meteo_weather(), us_aqi=165)
    nws = replace(_weather(), alerts=[_alert("Flash Flood Warning", "Severe")])
    data = DashboardData(
        generated_at=NOW,
        source_data={
            NwsData: _nws_data(nws),
            om.OpenMeteoData: _om_data(om_weather),
            MtaData: MtaData(boards=_boards()),
        },
    )
    img = _render(data)
    assert img.size == (W, H)


def _font_or_skip(family: str) -> Fonts:
    """``Fonts(family)``, skipping the test when that family isn't installed on this machine."""
    fonts = Fonts(family)
    try:
        fonts.get(40, "Bold")
    except LayoutError:
        pytest.skip(f"font {family!r} is not installed")
    return fonts


# "Adwaita Sans" is DEFAULT_FONT, so it's assumed installed; "Charter" (a small-descent face, the
# one that surfaced the bug) is opportunistic — skipped where it isn't available.
@pytest.mark.parametrize("font", ["Adwaita Sans", "Charter"])
def test_cap_metrics_match_rendered_ink(font: str) -> None:
    # The regression that motivated these helpers: an icon centered on the `lm` anchor's `cy` rides
    # high above the text, because `m` centers the em box (ascent+descent), not the caps. Assert the
    # computed cap band against what Pillow actually inks, rather than re-deriving the formula.
    ft = _font_or_skip(font).get(40, "Bold")
    cy = 100.0
    img = Image.new("L", (400, 200), PAPER)
    ImageDraw.Draw(img).text((10, cy), "HH", font=ft, fill=INK, anchor="lm")
    rows = [y for y in range(200) if any(img.getpixel((x, y)) < 128 for x in range(400))]
    # "H" is all caps: its ink spans exactly the cap band, so both must agree within a pixel.
    assert abs(_cap_midline(ft, cy) - (rows[0] + rows[-1]) / 2) <= 1.0
    assert abs(_cap_height(ft) - (rows[-1] - rows[0] + 1)) <= 1.0


def _icons_pasted(monkeypatch, data: DashboardData) -> list[str]:
    """Render ``data``, recording the name of every icon the layout pastes."""
    from kindle_dash_gen.render.builtins import glanceable

    names: list[str] = []
    original = glanceable._Glanceable._paste_icon

    def spy(self, name, cx, cy, box):
        names.append(name)
        return original(self, name, cx, cy, box)

    monkeypatch.setattr(glanceable._Glanceable, "_paste_icon", spy)
    _render(data)
    return names


@pytest.mark.parametrize("aqi,flagged", [(110, False), (165, True)])
def test_unhealthy_aqi_is_flagged(monkeypatch, aqi: int, flagged: bool) -> None:
    # An "Unhealthy" AQI earns the same warning icon an alert gets; "Unhealthy (Sensitive)" (110)
    # is scoped to at-risk groups and stays a plain row.
    data = DashboardData(
        generated_at=NOW,
        source_data={om.OpenMeteoData: _om_data(replace(_open_meteo_weather(), us_aqi=aqi))},
    )
    assert ("warning" in _icons_pasted(monkeypatch, data)) is flagged


def test_alert_row_draws_warning_icon(monkeypatch) -> None:
    nws = replace(_weather(), alerts=[_alert("Flash Flood Warning", "Severe")])
    data = DashboardData(generated_at=NOW, source_data={NwsData: _nws_data(nws)})
    assert "warning" in _icons_pasted(monkeypatch, data)


def test_renders_without_weather() -> None:
    # A dropped weather source (subway only) still yields a full-size image.
    img = _render(_dashboard(weather=None))
    assert img.size == (W, H)


def test_renders_without_boards() -> None:
    # A dropped subway source (weather only) still yields a full-size image.
    img = _render(_dashboard(boards=[]))
    assert img.size == (W, H)


def test_font_none_falls_back_to_default() -> None:
    # An unspecified font (None) resolves to the layout's default (glanceable's DEFAULT_FONT), so
    # the render still succeeds at full size rather than failing to resolve a font.
    img = render(
        _dashboard(),
        width=W,
        height=H,
        layout="glanceable",
        layout_config={
            "title": "NYC",
            "timezone": "America/New_York",
            "weather_location": LOCATION,
        },
    )
    assert img.size == (W, H)


def test_title_is_required() -> None:
    # The header title has no default; a layout_config without it fails validation.
    with pytest.raises(ValidationError):
        render(_dashboard(), width=W, height=H, layout="glanceable", layout_config={})


def test_custom_title_renders() -> None:
    # A configured title changes the header without otherwise altering the render.
    base = _render(_dashboard()).tobytes()
    cfg = dict(_CONFIG)
    cfg["title"] = "Brooklyn"
    other = render(
        _dashboard(), width=W, height=H, layout="glanceable", layout_config=cfg
    ).tobytes()
    assert other != base  # the header pixels differ


def test_unknown_layout_raises() -> None:
    with pytest.raises(LayoutError):
        render(_dashboard(), width=W, height=H, layout="nope", layout_config={})


def test_unknown_layout_config_key_is_rejected() -> None:
    # A layout owns its config (extra="forbid"), so an unknown layout_config key fails fast.
    with pytest.raises(ValidationError):
        render(
            _dashboard(),
            width=W,
            height=H,
            layout="glanceable",
            layout_config={
                "title": "NYC",
                "timezone": "America/New_York",
                "weather_location": LOCATION,
                "bogus": 1,
            },
        )


def test_unresolvable_font_raises() -> None:
    # fc-match always substitutes a best match; a bogus family must fail fast, not render in a
    # silently-substituted fallback font.
    with pytest.raises(LayoutError):
        render(
            _dashboard(),
            width=W,
            height=H,
            layout="glanceable",
            layout_config={
                "title": "NYC",
                "timezone": "America/New_York",
                "weather_location": LOCATION,
                "font": "No Such Font Family 9000",
            },
        )


def test_resolve_face_differentiates_weights() -> None:
    # Distinct weights must resolve to distinct faces (font file or variable-font instance index),
    # so headings actually render heavier than body text.
    faces = {_resolve_face("Adwaita Sans", w) for w in ("Regular", "Bold", "Black")}
    assert len(faces) == 3


# ── Display timezone ───────────────────────────────────────────────────────────────────────────
# Sources hand over aware UTC; the layout converts every drawn time to its configured zone. These
# pin that the conversion actually happens, and that it is the config — not the host's TZ — that
# decides what the panel says.


def _rendered_text(data: DashboardData, config: dict) -> list[str]:
    """Every string the layout draws, captured by spying on ImageDraw.text."""
    drawn: list[str] = []
    original = ImageDraw.ImageDraw.text

    def spy(self, xy, text, *args, **kwargs):
        drawn.append(str(text))
        return original(self, xy, text, *args, **kwargs)

    with mock.patch.object(ImageDraw.ImageDraw, "text", spy):
        render(data, width=W, height=H, layout="glanceable", layout_config=config)
    return drawn


def test_header_clock_is_drawn_in_the_configured_timezone() -> None:
    # NOW is 2026-07-03 00:30 UTC, i.e. 20:30 on Jul 2 in New York and 17:30 in Los Angeles.
    eastern = _rendered_text(_dashboard(), {**_CONFIG, "timezone": "America/New_York"})
    pacific = _rendered_text(_dashboard(), {**_CONFIG, "timezone": "America/Los_Angeles"})
    assert "Thu Jul 2, 8:30 PM" in eastern
    assert "Thu Jul 2, 5:30 PM" in pacific


def test_arrival_clock_is_drawn_in_the_configured_timezone() -> None:
    # A single aware-UTC arrival renders as a different wall clock per dashboard timezone.
    arrival = datetime(2026, 7, 3, 0, 35, 0, tzinfo=UTC)
    board = StationBoard(
        name="Union Sq",
        arrivals_by_direction={
            Direction.NORTH: [
                TrainArrival(
                    route="N", direction=Direction.NORTH, destination="Astoria", arrival=arrival
                )
            ]
        },
    )
    data = _dashboard(boards=[board])
    assert "8:35 pm" in _rendered_text(data, {**_CONFIG, "timezone": "America/New_York"})
    assert "5:35 pm" in _rendered_text(data, {**_CONFIG, "timezone": "America/Los_Angeles"})


def test_render_is_independent_of_the_host_timezone(host_timezone) -> None:
    # The whole point of aware UTC + a configured display zone: the panel must not change because
    # the machine generating it sits in a different zone.
    def render_under(tz: str) -> bytes:
        host_timezone(tz)
        return _render(_dashboard()).tobytes()

    assert render_under("America/Los_Angeles") == render_under("Asia/Kolkata")


def test_timezone_is_required_and_validated() -> None:
    # No default: without a zone the layout would print UTC clock times.
    with pytest.raises(ValidationError):
        render(_dashboard(), width=W, height=H, layout="glanceable", layout_config={"title": "NYC"})
    # A bad zone fails at config validation, not at the first render.
    with pytest.raises(ValidationError):
        render(
            _dashboard(),
            width=W,
            height=H,
            layout="glanceable",
            layout_config={"title": "NYC", "timezone": "Mars/Olympus_Mons"},
        )


# ── Transit adapter ────────────────────────────────────────────────────────────────────────────
# The layout draws transit through one normalized surface, so the draw code never sees a
# provider type — the same shape the weather adapter already uses. These cover the mapping;
# the pixel-level guarantees are further down.


def _sf_arrival(agency, direction, line="Green-N", minutes=5):
    return sf.TransitArrival(
        agency=agency,
        line=line,
        direction=direction,
        destination="Somewhere",
        arrival=NOW + timedelta(minutes=minutes),
    )


@pytest.mark.parametrize(
    ("direction", "expected"),
    [
        (sf.BartDirection.NORTH, "Northbound"),
        (sf.BartDirection.SOUTH, "Southbound"),
        (sf.AcTransitDirection.EAST, "Eastbound"),
        (sf.AcTransitDirection.WEST, "Westbound"),
        (sf.MuniDirection.INBOUND, "Inbound"),
        (sf.MuniDirection.OUTBOUND, "Outbound"),
        # An unrecognized code degrades to itself rather than failing the render. The vocabulary
        # lives here and in the source's per-agency enums, so a new direction shows as a bare code.
        ("ZZ", "ZZ"),
    ],
)
def test_direction_labels_cover_every_agency_vocabulary(direction, expected) -> None:
    assert _direction_label(direction) == expected


def test_an_unknown_direction_sorts_last() -> None:
    assert _direction_rank("ZZ") > _direction_rank(sf.MuniDirection.OUTBOUND)


def test_from_mta_always_emits_both_directions() -> None:
    # Both blocks are always drawn (an empty one shows "No trains"), which is what keeps the MTA
    # rendering unchanged by this adapter.
    board = StationBoard(
        name="Union Sq",
        arrivals_by_direction={
            Direction.NORTH: [
                TrainArrival(
                    route="N", direction=Direction.NORTH, destination="Astoria", arrival=NOW
                )
            ]
        },
    )
    (glance,) = _from_mta([board])
    assert glance.label == "Union Sq"
    assert [g.label for g in glance.groups] == ["Uptown", "Downtown"]
    assert [a.label for a in glance.groups[0].arrivals] == ["N"]
    assert glance.groups[1].arrivals == []  # southbound empty, still its own block


def test_from_sf_bay_511_labels_directions_and_uses_the_line_as_the_badge() -> None:
    board = sf.StopBoard(
        name="Embarcadero",
        arrivals={
            sf.Agency.BART: {
                sf.BartDirection.NORTH: [_sf_arrival(sf.Agency.BART, sf.BartDirection.NORTH)]
            }
        },
    )
    (glance,) = _from_sf_bay_511([board])
    assert glance.label == "Embarcadero"
    assert [g.label for g in glance.groups] == ["Northbound"]
    assert [a.label for a in glance.groups[0].arrivals] == ["Green-N"]


def test_from_sf_bay_511_prefixes_the_agency_only_when_a_board_spans_several() -> None:
    # One place served by two operators would otherwise show two unqualified "Northbound" blocks.
    # Muni is inserted first on purpose: dicts keep insertion order, so without the canonical
    # agency sort this test would pass whatever order the adapter emitted.
    shared = sf.StopBoard(
        name="Embarcadero",
        arrivals={
            sf.Agency.MUNI: {
                sf.MuniDirection.INBOUND: [
                    _sf_arrival(sf.Agency.MUNI, sf.MuniDirection.INBOUND, line="N")
                ]
            },
            sf.Agency.BART: {
                sf.BartDirection.NORTH: [_sf_arrival(sf.Agency.BART, sf.BartDirection.NORTH)]
            },
        },
    )
    (glance,) = _from_sf_bay_511([shared])
    assert [g.label for g in glance.groups] == ["BART Northbound", "Muni Inbound"]


def test_from_sf_bay_511_orders_groups_canonically() -> None:
    # The adapter orders but does not truncate: the two-block limit is geometric, so it lives in
    # the draw code. Ordering is canonical so the panel doesn't reshuffle between runs just
    # because the feed listed directions differently.
    board = sf.StopBoard(
        name="Busy",
        arrivals={
            sf.Agency.AC_TRANSIT: {
                sf.AcTransitDirection.WEST: [
                    _sf_arrival(sf.Agency.AC_TRANSIT, sf.AcTransitDirection.WEST)
                ],
                sf.AcTransitDirection.SOUTH: [
                    _sf_arrival(sf.Agency.AC_TRANSIT, sf.AcTransitDirection.SOUTH)
                ],
                sf.AcTransitDirection.NORTH: [
                    _sf_arrival(sf.Agency.AC_TRANSIT, sf.AcTransitDirection.NORTH)
                ],
            }
        },
    )
    (glance,) = _from_sf_bay_511([board])
    assert [g.label for g in glance.groups] == ["Northbound", "Southbound", "Westbound"]


def test_only_the_first_two_direction_blocks_are_drawn() -> None:
    # The band's pitch divides the remaining height by six (two blocks x three arrivals), so a
    # third direction has nowhere to go.
    board = sf.StopBoard(
        name="Busy",
        arrivals={
            sf.Agency.AC_TRANSIT: {
                d: [_sf_arrival(sf.Agency.AC_TRANSIT, d, line=d.name)]
                for d in (
                    sf.AcTransitDirection.NORTH,
                    sf.AcTransitDirection.SOUTH,
                    sf.AcTransitDirection.WEST,
                )
            }
        },
    )
    data = DashboardData(
        generated_at=NOW, source_data={sf.SfBay511Data: sf.SfBay511Data(boards=[board])}
    )
    drawn = _rendered_text(data, _CONFIG)
    assert "Northbound" in drawn
    assert "Southbound" in drawn
    assert "Westbound" not in drawn


def test_transit_combines_providers_with_mta_first() -> None:
    # MTA first so an MTA-only dashboard's column order (and pixels) are untouched by 511 existing.
    mta = MtaData(boards=[StationBoard(name="Union Sq", arrivals_by_direction={})])
    bay = sf.SfBay511Data(boards=[sf.StopBoard(name="Embarcadero", arrivals={})])
    both = DashboardData(generated_at=NOW, source_data={MtaData: mta, sf.SfBay511Data: bay})
    assert [b.label for b in _transit(both, None)] == ["Union Sq", "Embarcadero"]
    only_mta = DashboardData(generated_at=NOW, source_data={MtaData: mta})
    assert [b.label for b in _transit(only_mta, None)] == ["Union Sq"]
    only_bay = DashboardData(generated_at=NOW, source_data={sf.SfBay511Data: bay})
    assert [b.label for b in _transit(only_bay, None)] == ["Embarcadero"]
    assert _transit(DashboardData(generated_at=NOW, source_data={}), None) == []


def _sf_dashboard() -> DashboardData:
    board = sf.StopBoard(
        name="Embarcadero",
        arrivals={
            sf.Agency.BART: {
                sf.BartDirection.NORTH: [
                    _sf_arrival(sf.Agency.BART, sf.BartDirection.NORTH, minutes=m)
                    for m in (4, 12, 20)
                ],
                sf.BartDirection.SOUTH: [
                    _sf_arrival(sf.Agency.BART, sf.BartDirection.SOUTH, line="Blue-S", minutes=m)
                    for m in (6, 18)
                ],
            }
        },
    )
    return DashboardData(
        generated_at=NOW, source_data={sf.SfBay511Data: sf.SfBay511Data(boards=[board])}
    )


def test_renders_a_511_only_dashboard() -> None:
    img = _render(_sf_dashboard())
    assert img.size == (W, H)
    assert img.mode == "L"


def test_511_board_draws_its_direction_labels_and_local_clocks() -> None:
    drawn = _rendered_text(_sf_dashboard(), {**_CONFIG, "timezone": "America/Los_Angeles"})
    assert "Embarcadero" in drawn
    assert "Northbound" in drawn
    assert "Southbound" in drawn
    assert "Green-N" in drawn  # the LineRef as the route badge
    # NOW is 00:30 UTC on Jul 3 = 17:30 Pacific on Jul 2; the first arrival is +4 minutes.
    assert "5:34 pm" in drawn


def test_an_empty_511_direction_shows_no_trains() -> None:
    board = sf.StopBoard(
        name="Embarcadero",
        arrivals={sf.Agency.BART: {sf.BartDirection.NORTH: []}},
    )
    data = DashboardData(
        generated_at=NOW, source_data={sf.SfBay511Data: sf.SfBay511Data(boards=[board])}
    )
    assert "No trains" in _rendered_text(data, _CONFIG)


def test_renders_both_providers_together() -> None:
    mta = MtaData(boards=_boards())
    bay = _sf_dashboard().source_data[sf.SfBay511Data]
    data = DashboardData(generated_at=NOW, source_data={MtaData: mta, sf.SfBay511Data: bay})
    drawn = _rendered_text(data, _CONFIG)
    assert "57 St-6 Av" in drawn  # MTA board
    assert "Embarcadero" in drawn  # 511 board
    assert _render(data).size == (W, H)


def test_mta_rendering_is_unchanged_by_the_transit_adapter() -> None:
    """Pixel hashes captured from the pre-adapter layout, when MTA was drawn directly.

    The adapter was a re-shaping, not a redesign: an existing NYC dashboard must look exactly as it
    did. These are checked-in digests rather than a self-comparison, so a change to the geometry,
    the "Uptown"/"Downtown" labels, the always-two-blocks rule, or the MTA-first ordering fails
    here instead of quietly redrawing someone's panel.

    Re-derive them against the pre-adapter layout with::

        git worktree add /tmp/pre 6c14902 && (cd /tmp/pre && uv run python -c "...")

    Note the digests also depend on the resolved font file and the Pillow/FreeType version, so a
    font or Pillow bump fails this with an unhelpful message. It is worth keeping only while the
    refactor is recent; delete it once the guarantee has stopped being interesting.
    """
    expected = {
        "weather+boards": "6a39b5a7f769f6fc",
        "boards only": "2d48e5a65fa6b652",
        "weather only": "6711aa7eec22fb7a",
    }
    actual = {
        "weather+boards": _digest(_dashboard()),
        "boards only": _digest(_dashboard(weather=None)),
        "weather only": _digest(_dashboard(boards=[])),
    }
    assert actual == expected


def _digest(data: DashboardData) -> str:
    return hashlib.sha256(_render(data).tobytes()).hexdigest()[:16]


def _gutter_ink(line: str) -> int:
    """Ink pixels in the gutter between two transit columns, for a board using route ``line``."""
    boards = [
        sf.StopBoard(
            name=name,
            arrivals={
                sf.Agency.BART: {
                    sf.BartDirection.NORTH: [
                        _sf_arrival(sf.Agency.BART, sf.BartDirection.NORTH, line=line)
                    ]
                }
            },
        )
        for name in ("Embarcadero", "Montgomery")
    ]
    data = DashboardData(
        generated_at=NOW, source_data={sf.SfBay511Data: sf.SfBay511Data(boards=boards)}
    )
    img = _render(data)
    pixels = img.load()
    col_w = (W - 2 * _MARGIN - _GUTTER) / 2
    left = int(_MARGIN + col_w) + 1
    return sum(
        1 for x in range(left, left + _GUTTER - 1) for y in range(img.height) if pixels[x, y] < 128
    )


def test_a_long_route_badge_stays_inside_its_column() -> None:
    """A 511 LineRef is a word, not a letter, and must not spill into the next column.

    Badges are centered on a shared x, which suits a one-character subway route; centering
    "Yellow-N" on the same x pushed half of it across the gutter and through the neighbouring
    column's clock. Only rendering surfaced it — the text was drawn, just in the wrong place — so
    this asserts on pixels. Differencing against a short label cancels the full-width rules that
    legitimately cross the gutter.
    """
    assert _gutter_ink("Yellow-Nx") == _gutter_ink("K")


@pytest.mark.parametrize("boards", [1, 2, 3, 4, 5])
def test_a_short_badge_is_untouched_at_any_column_count(boards: int) -> None:
    """The badge clamps must not fire for a subway route, however narrow the columns get.

    A four-column board leaves the clock almost the whole width, so without the width floor the
    fit clamp shrank a one-character MTA badge from 50 to 18 — a silent regression the two-column
    digest test could not see.
    """
    fonts = Fonts("Adwaita Sans")
    clock_w = fonts.get(46, "Medium").getlength("12:34 pm")
    col_w = (W - 2 * _MARGIN - _GUTTER * (boards - 1)) / boards
    available = col_w - _CLOCK_INDENT - clock_w - _BADGE_GAP

    font = fit_font(fonts, "M", "Black", 50, max(available, _BADGE_MIN_WIDTH))
    assert font.size == 50, "badge shrank"
    # The center clamp is a no-op precisely while the badge is narrower than the floor.
    assert font.getlength("M") < _BADGE_MIN_WIDTH
    assert _BADGE_MARGIN + font.getlength("M") / 2 <= _BADGE_INSET, "badge slid off its shared x"


def test_more_than_three_transit_boards_is_an_error() -> None:
    """The band is three columns wide; a fourth would overlap, so it fails loudly at render.

    The count is only known once sources are fetched (a station maps to a board), so this is a
    render-time LayoutError the pipeline isolates per dashboard, not a config-load check.
    """
    boards = [
        StationBoard(name=f"S{i}", arrivals_by_direction={}) for i in range(_MAX_TRANSIT_BOARDS + 1)
    ]
    data = DashboardData(generated_at=NOW, source_data={MtaData: MtaData(boards=boards)})
    with pytest.raises(LayoutError):
        _render(data)


def test_exactly_three_transit_boards_render() -> None:
    boards = [
        StationBoard(name=f"S{i}", arrivals_by_direction={}) for i in range(_MAX_TRANSIT_BOARDS)
    ]
    data = DashboardData(generated_at=NOW, source_data={MtaData: MtaData(boards=boards)})
    assert _render(data).size == (W, H)


def test_the_board_limit_counts_across_providers() -> None:
    # Two MTA + two 511 is four columns just as surely as four MTA boards.
    mta = MtaData(boards=[StationBoard(name=f"M{i}", arrivals_by_direction={}) for i in range(2)])
    bay = sf.SfBay511Data(boards=[sf.StopBoard(name=f"B{i}", arrivals={}) for i in range(2)])
    data = DashboardData(generated_at=NOW, source_data={MtaData: mta, sf.SfBay511Data: bay})
    with pytest.raises(LayoutError):
        _render(data)


# ── Per-dashboard selection (weather_location + transit_boards) ─────────────────────────────────
# One shared fetch can carry several cities and stations; each dashboard's layout_config picks the
# slice it draws. This is what lets sibling dashboards show different places from one gather().


def test_weather_location_picks_the_named_city() -> None:
    from kindle_dash_gen.render.builtins.glanceable import _weather as adapt

    warm = replace(_weather(), temperature=Temperature(30.0, 30.0))
    cold = replace(_weather(), temperature=Temperature(-5.0, -5.0))
    data = DashboardData(
        generated_at=NOW,
        source_data={NwsData: NwsData(locations={"NYC": warm, "SF": cold})},
    )
    assert adapt(data, "NYC").temperature.real == 30.0
    assert adapt(data, "SF").temperature.real == -5.0


def test_weather_location_absent_from_data_renders_no_weather() -> None:
    from kindle_dash_gen.render.builtins.glanceable import _weather as adapt

    data = DashboardData(generated_at=NOW, source_data={NwsData: _nws_data()})
    assert adapt(data, "not-configured") is None


def test_weather_reconciles_the_same_name_across_providers() -> None:
    # The city name is the join key: Open-Meteo's "NYC" forecast pairs with NWS's "NYC" alerts.
    from kindle_dash_gen.render.builtins.glanceable import _weather as adapt

    om_nyc = replace(_open_meteo_weather(), us_aqi=88)
    nws_nyc = replace(_weather(), alerts=[_alert("Heat Advisory", "Moderate")])
    data = DashboardData(
        generated_at=NOW,
        source_data={
            om.OpenMeteoData: om.OpenMeteoData(locations={"NYC": om_nyc}),
            NwsData: NwsData(locations={"NYC": nws_nyc}),
        },
    )
    resolved = adapt(data, "NYC")
    assert resolved.aqi == 88  # Open-Meteo's
    assert resolved.alerts == ("Heat Advisory",)  # NWS's


def test_weather_location_is_required() -> None:
    with pytest.raises(ValidationError):
        render(
            _dashboard(),
            width=W,
            height=H,
            layout="glanceable",
            layout_config={"title": "NYC", "timezone": "America/New_York"},
        )


def test_transit_boards_none_draws_every_board() -> None:
    boards = [StationBoard(name=f"S{i}", arrivals_by_direction={}) for i in range(2)]
    data = DashboardData(generated_at=NOW, source_data={MtaData: MtaData(boards=boards)})
    assert [b.label for b in _transit(data, None)] == ["S0", "S1"]


def test_transit_boards_allowlist_keeps_only_named_boards() -> None:
    boards = [
        StationBoard(name=n, arrivals_by_direction={}) for n in ("Union Sq", "14 St", "Bergen")
    ]
    data = DashboardData(generated_at=NOW, source_data={MtaData: MtaData(boards=boards)})
    kept = _transit(data, ["Bergen", "Union Sq"])
    # Filtered to the allowlisted names, in source order (not the list's order).
    assert [b.label for b in kept] == ["Union Sq", "Bergen"]


def test_transit_boards_selects_by_canonical_name_not_display_label() -> None:
    # display_name overrides the label a layout shows, but selection matches the canonical name so
    # renaming the display never breaks the allowlist.
    board = StationBoard(name="union-sq", arrivals_by_direction={}, display_name="Union Square")
    data = DashboardData(generated_at=NOW, source_data={MtaData: MtaData(boards=[board])})
    (kept,) = _transit(data, ["union-sq"])
    assert kept.label == "Union Square"


def test_transit_boards_filter_spans_providers() -> None:
    mta = MtaData(boards=[StationBoard(name="Union Sq", arrivals_by_direction={})])
    bay = sf.SfBay511Data(boards=[sf.StopBoard(name="Embarcadero", arrivals={})])
    data = DashboardData(generated_at=NOW, source_data={MtaData: mta, sf.SfBay511Data: bay})
    assert [b.label for b in _transit(data, ["Embarcadero"])] == ["Embarcadero"]


def test_transit_boards_narrows_an_over_cap_config_below_the_limit() -> None:
    # The 3-board cap lives after the allowlist, on purpose: a config with more stations than fit
    # is fine as long as each dashboard selects a drawable few. Four configured, two selected.
    boards = [StationBoard(name=f"S{i}", arrivals_by_direction={}) for i in range(4)]
    data = DashboardData(generated_at=NOW, source_data={MtaData: MtaData(boards=boards)})
    cfg = {**_CONFIG, "transit_boards": ["S0", "S2"]}
    img = render(data, width=W, height=H, layout="glanceable", layout_config=cfg)
    assert img.size == (W, H)


def test_selecting_more_than_three_boards_still_overflows() -> None:
    boards = [StationBoard(name=f"S{i}", arrivals_by_direction={}) for i in range(5)]
    data = DashboardData(generated_at=NOW, source_data={MtaData: MtaData(boards=boards)})
    cfg = {**_CONFIG, "transit_boards": ["S0", "S1", "S2", "S3"]}
    with pytest.raises(LayoutError):
        render(data, width=W, height=H, layout="glanceable", layout_config=cfg)
