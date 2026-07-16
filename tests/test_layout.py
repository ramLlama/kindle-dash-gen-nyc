"""Integration tests for the Pillow rendering backend.

These render real ``DashboardData`` through the layout and assert output invariants (exact panel
size, grayscale mode, determinism, graceful degradation). Glyph-level appearance is not asserted;
it depends on the system font and is an iterating visual concern.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from PIL import Image
from pydantic import ValidationError

from kindle_dash_gen.models import DashboardData
from kindle_dash_gen.render.layout import LayoutError, render
from kindle_dash_gen.render.toolkit import _resolve_face
from kindle_dash_gen.sources.builtins.mta.model import (
    Direction,
    MtaData,
    StationBoard,
    TrainArrival,
)
from kindle_dash_gen.sources.builtins.nws.model import HourlyForecast, NwsData, Temperature

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
        high=Temperature(42.0, 45.0),
        low=Temperature(30.0, None),
        high_low_date=date(2026, 7, 2),
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


_CONFIG = {"font": "Adwaita Sans", "weather_temp_units": "both"}


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
    img = render(_dashboard(), width=W, height=H, layout="glanceable", layout_config={})
    assert img.size == (W, H)


def test_unknown_layout_raises() -> None:
    with pytest.raises(LayoutError):
        render(_dashboard(), width=W, height=H, layout="nope", layout_config={})


def test_unknown_layout_config_key_is_rejected() -> None:
    # A layout owns its config (extra="forbid"), so an unknown layout_config key fails fast.
    with pytest.raises(ValidationError):
        render(_dashboard(), width=W, height=H, layout="glanceable", layout_config={"bogus": 1})


def test_unresolvable_font_raises() -> None:
    # fc-match always substitutes a best match; a bogus family must fail fast, not render in a
    # silently-substituted fallback font.
    with pytest.raises(LayoutError):
        render(
            _dashboard(),
            width=W,
            height=H,
            layout="glanceable",
            layout_config={"font": "No Such Font Family 9000"},
        )


def test_resolve_face_differentiates_weights() -> None:
    # Distinct weights must resolve to distinct faces (font file or variable-font instance index),
    # so headings actually render heavier than body text.
    faces = {_resolve_face("Adwaita Sans", w) for w in ("Regular", "Bold", "Black")}
    assert len(faces) == 3
