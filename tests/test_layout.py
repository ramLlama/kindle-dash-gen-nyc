"""Integration tests for the Pillow rendering backend.

These render real ``DashboardData`` through the layout and assert output invariants (exact panel
size, grayscale mode, determinism, graceful degradation). Glyph-level appearance is not asserted;
it depends on the system font and is an iterating visual concern.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from io import BytesIO

import pytest
from PIL import Image

from kindle_dash_gen_nyc.models import (
    DashboardData,
    Direction,
    HourlyForecast,
    StationBoard,
    Temperature,
    TrainArrival,
    WeatherReport,
)
from kindle_dash_gen_nyc.render.layout import LayoutError, render

NOW = datetime(2026, 7, 2, 20, 30, 0)
W, H = 1072, 1448

_MISSING = object()  # distinguishes "use default" from an explicit None/[]


def _weather() -> WeatherReport:
    return WeatherReport(
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
    return DashboardData(
        weather=_weather() if weather is _MISSING else weather,
        boards=_boards() if boards is _MISSING else boards,
        generated_at=NOW,
    )


def _render(data: DashboardData) -> Image.Image:
    png = render(data, units="both", width=W, height=H, layout="glanceable", font="Adwaita Sans")
    return Image.open(BytesIO(png))


def test_renders_kindle_sized_grayscale() -> None:
    img = _render(_dashboard())
    assert img.size == (W, H)
    assert img.mode == "L"


def test_render_is_deterministic() -> None:
    data = _dashboard()
    first = render(data, units="both", width=W, height=H, layout="glanceable", font="Adwaita Sans")
    second = render(data, units="both", width=W, height=H, layout="glanceable", font="Adwaita Sans")
    assert first == second


def test_renders_without_weather() -> None:
    # A dropped weather source (subway only) still yields a full-size image.
    img = _render(_dashboard(weather=None))
    assert img.size == (W, H)


def test_renders_without_boards() -> None:
    # A dropped subway source (weather only) still yields a full-size image.
    img = _render(_dashboard(boards=[]))
    assert img.size == (W, H)


def test_unknown_layout_raises() -> None:
    with pytest.raises(LayoutError):
        render(_dashboard(), units="us", width=W, height=H, layout="nope", font="Adwaita Sans")


def test_unresolvable_font_raises() -> None:
    # fc-match always substitutes a best match; a bogus family must fail fast, not render in a
    # silently-substituted fallback font.
    with pytest.raises(LayoutError):
        render(_dashboard(), units="us", width=W, height=H, layout="glanceable",
               font="No Such Font Family 9000")
