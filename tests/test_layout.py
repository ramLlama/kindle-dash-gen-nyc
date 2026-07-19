"""Integration tests for the Pillow rendering backend.

These render real ``DashboardData`` through the layout and assert output invariants (exact panel
size, grayscale mode, determinism, graceful degradation). Glyph-level appearance is not asserted;
it depends on the system font and is an iterating visual concern.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError

from kindle_dash_gen.models import DashboardData
from kindle_dash_gen.render.builtins.glanceable import _cap_height, _cap_midline
from kindle_dash_gen.render.layout import LayoutError, render
from kindle_dash_gen.render.toolkit import INK, PAPER, Fonts, _resolve_face
from kindle_dash_gen.sources.builtins.mta.model import (
    Direction,
    MtaData,
    StationBoard,
    TrainArrival,
)
from kindle_dash_gen.sources.builtins.nws.model import (
    DailyHighLow,
    HourlyForecast,
    NwsData,
    Temperature,
    WeatherAlert,
)
from kindle_dash_gen.sources.builtins.open_meteo import model as om

NOW = datetime(2026, 7, 2, 20, 30, 0)
W, H = 1072, 1448

_MISSING = object()  # distinguishes "use default" from an explicit None/[]


def _weather() -> NwsData:
    return NwsData(
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


def _open_meteo_weather() -> om.OpenMeteoData:
    """An OpenMeteoData carrying the same drawn values as :func:`_weather` (NwsData).

    Mirrors every field the glanceable weather section renders (current apparent temp, wind, the
    icon-resolving conditions text, and the hourly strip) so the two providers must render
    identically; the extra AQI fields are unset.
    """
    return om.OpenMeteoData(
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
    w = _weather() if weather is _MISSING else weather
    b = _boards() if boards is _MISSING else boards
    source_data: dict[type, object] = {}
    if w is not None:
        source_data[NwsData] = w
    if b is not None:
        source_data[MtaData] = MtaData(boards=b)
    return DashboardData(generated_at=NOW, source_data=source_data)


_CONFIG = {"title": "NYC", "font": "Adwaita Sans", "weather_temp_units": "both"}


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
    nws = DashboardData(generated_at=NOW, source_data={NwsData: _weather(), MtaData: boards})
    open_meteo = DashboardData(
        generated_at=NOW, source_data={om.OpenMeteoData: _open_meteo_weather(), MtaData: boards}
    )
    assert _render(nws).tobytes() == _render(open_meteo).tobytes()


def test_adapter_prefers_open_meteo_when_both_present() -> None:
    # With both weather sources configured, the adapter renders Open-Meteo (the global provider).
    from kindle_dash_gen.render.builtins.glanceable import _weather as adapt

    nws = _weather()  # wind "SW"
    open_meteo = om.OpenMeteoData(
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
    data = DashboardData(generated_at=NOW, source_data={NwsData: nws, om.OpenMeteoData: open_meteo})
    resolved = adapt(data)
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
    data = DashboardData(generated_at=NOW, source_data={NwsData: nws, om.OpenMeteoData: om_weather})
    resolved = adapt(data)
    assert resolved is not None
    assert resolved.aqi == 110  # from Open-Meteo
    assert resolved.alerts == ("Tornado", "Heat Advisory")  # Extreme before Moderate


def test_adapter_omits_missing_provider_fields() -> None:
    # NWS-only: no AQI (Open-Meteo absent). Open-Meteo-only: no alerts (NWS absent).
    from kindle_dash_gen.render.builtins.glanceable import _weather as adapt

    nws_only = DashboardData(generated_at=NOW, source_data={NwsData: _weather()})
    resolved = adapt(nws_only)
    assert resolved is not None
    assert resolved.aqi is None
    assert resolved.alerts == ()

    om_only = DashboardData(
        generated_at=NOW, source_data={om.OpenMeteoData: replace(_open_meteo_weather(), us_aqi=42)}
    )
    resolved = adapt(om_only)
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
            NwsData: nws,
            om.OpenMeteoData: om_weather,
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
        source_data={om.OpenMeteoData: replace(_open_meteo_weather(), us_aqi=aqi)},
    )
    assert ("warning" in _icons_pasted(monkeypatch, data)) is flagged


def test_alert_row_draws_warning_icon(monkeypatch) -> None:
    nws = replace(_weather(), alerts=[_alert("Flash Flood Warning", "Severe")])
    data = DashboardData(generated_at=NOW, source_data={NwsData: nws})
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
        _dashboard(), width=W, height=H, layout="glanceable", layout_config={"title": "NYC"}
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
            layout_config={"title": "NYC", "bogus": 1},
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
            layout_config={"title": "NYC", "font": "No Such Font Family 9000"},
        )


def test_resolve_face_differentiates_weights() -> None:
    # Distinct weights must resolve to distinct faces (font file or variable-font instance index),
    # so headings actually render heavier than body text.
    faces = {_resolve_face("Adwaita Sans", w) for w in ("Regular", "Bold", "Black")}
    assert len(faces) == 3
