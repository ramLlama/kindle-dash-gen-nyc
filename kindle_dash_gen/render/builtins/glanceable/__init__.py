"""The bundled "glanceable" layout — a plugin like any other.

Hero weather, an hourly strip, and per-station arrival boards. This ships with the app but is
structurally identical to a private plugin: it depends only on the public
:mod:`kindle_dash_gen.render.toolkit` surface, owns its own assets, and registers itself via
:func:`register_layout` at import. It could be dropped verbatim into a private plugins directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict

from kindle_dash_gen.models import DashboardData
from kindle_dash_gen.render.layout import Layout, register_layout
from kindle_dash_gen.render.toolkit import (
    DEFAULT_FONT,
    INK,
    PAPER,
    Fonts,
    fit_font,
    format_apparent,
    format_wind,
    load_asset_image,
    weather_icon,
)
from kindle_dash_gen.sources.builtins.mta.model import (
    Direction,
    MtaData,
    StationBoard,
    TrainArrival,
)
from kindle_dash_gen.sources.builtins.nws.model import NwsData
from kindle_dash_gen.sources.builtins.open_meteo.model import OpenMeteoData

_PACKAGE = "kindle_dash_gen.render.builtins.glanceable"  # this plugin's own package, for assets
_MARGIN = 44


# ── Weather adapter ────────────────────────────────────────────────────────────────────────────
# This layout reconciles multiple weather providers in its own local adapter (there is no shared
# cross-provider weather model). Each provider's own data type is normalized into the small draw
# surface the layout actually uses; nothing else here touches a provider type.


@dataclass(frozen=True)
class _Temp:
    """A normalized temperature reading (satisfies format's ``_TemperatureLike`` structurally)."""

    real: float
    feels_like: float | None


@dataclass(frozen=True, kw_only=True)
class _GlanceHour:
    """One upcoming hour, normalized to what the hourly strip draws."""

    time: datetime
    temperature: _Temp
    precip_probability: int | None


@dataclass(frozen=True, kw_only=True)
class _GlanceWeather:
    """The weather draw surface: current temp, wind, a resolved icon, and the hourly strip."""

    temperature: _Temp
    wind_speed_kmh: float | None
    wind_direction: str
    icon: str  # resolved icon name (snow/rain/cloudy/sunny) — provider differences handled here
    hourly: list[_GlanceHour]


def _norm_hours(hours: list) -> list[_GlanceHour]:
    """Normalize a provider's hourly forecasts (all share time/temperature/precip_probability)."""
    return [
        _GlanceHour(
            time=h.time,
            temperature=_Temp(h.temperature.real, h.temperature.feels_like),
            precip_probability=h.precip_probability,
        )
        for h in hours
    ]


def _from_nws(w: NwsData) -> _GlanceWeather:
    return _GlanceWeather(
        temperature=_Temp(w.temperature.real, w.temperature.feels_like),
        wind_speed_kmh=w.wind_speed_kmh,
        wind_direction=w.wind_direction,
        icon=weather_icon(w.observed_conditions, w.conditions, w.raining),
        hourly=_norm_hours(w.hourly),
    )


# WMO weather-interpretation code → dashboard icon. Icon selection is the layout's job (the source
# keeps the raw code, not a description engineered to match keywords), so the classification lives
# here. Unlisted codes — including 0/1 (clear) — fall through to "sunny".
_WMO_SNOW = frozenset({71, 73, 75, 77, 85, 86})
_WMO_RAIN = frozenset({51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99})
_WMO_CLOUDY = frozenset({2, 3, 45, 48})


def _wmo_icon(code: int) -> str:
    if code in _WMO_SNOW:
        return "snow"
    if code in _WMO_RAIN:
        return "rain"
    if code in _WMO_CLOUDY:
        return "cloudy"
    return "sunny"


def _from_open_meteo(w: OpenMeteoData) -> _GlanceWeather:
    # Open-Meteo has no station observation; the icon comes straight from its WMO weather code.
    return _GlanceWeather(
        temperature=_Temp(w.temperature.real, w.temperature.feels_like),
        wind_speed_kmh=w.wind_speed_kmh,
        wind_direction=w.wind_direction,
        icon=_wmo_icon(w.weather_code),
        hourly=_norm_hours(w.hourly),
    )


def _weather(data: DashboardData) -> _GlanceWeather | None:
    """Reconcile whichever weather provider is present into the layout's draw surface.

    Open-Meteo (global) is preferred; NWS is the fallback. A dashboard with neither renders without
    the weather section.
    """
    om = data.source_data.get(OpenMeteoData)
    if om is not None:
        return _from_open_meteo(om)
    nws = data.source_data.get(NwsData)
    if nws is not None:
        return _from_nws(nws)
    return None


def _load_icon(name: str, size: int) -> tuple[Image.Image, Image.Image]:
    """Load this plugin's icon ``name`` at ``size`` px as (grayscale, alpha mask)."""
    icon = load_asset_image(_PACKAGE, f"assets/icons/{name}.png")
    icon = icon.convert("LA").resize((size, size), Image.Resampling.LANCZOS)
    return icon.getchannel("L"), icon.getchannel("A")


class GlanceableConfig(BaseModel):
    """Config for a glanceable dashboard's ``[dashboards.<name>.layout_config]`` table."""

    model_config = ConfigDict(extra="forbid")

    # System font family (resolved via fontconfig). None = unspecified → the app-wide default.
    font: str | None = None
    # Display units for weather temperatures: "us" (°F), "si" (°C), or "both".
    weather_temp_units: Literal["us", "si", "both"] = "us"


class _Glanceable(Layout[GlanceableConfig]):
    """Renders the glanceable layout: hero weather, an hourly strip, and per-station arrival boards.

    Everything is measured against ``width``/``height`` so the layout adapts to the panel size.
    """

    Config = GlanceableConfig

    def __init__(self, config: GlanceableConfig, *, width: int, height: int) -> None:
        self.w = width
        self.h = height
        # No layout-specific font opinion: fall back to the app-wide default when unspecified.
        self.fonts = Fonts(config.font if config.font is not None else DEFAULT_FONT)
        self.units = config.weather_temp_units
        self.img = Image.new("L", (width, height), PAPER)
        self.d = ImageDraw.Draw(self.img)

    def render(self, data: DashboardData) -> Image.Image:
        y = self._title(_MARGIN, data.generated_at)
        weather = _weather(data)
        if weather is not None:
            y = self._hero(y, weather)
            y = self._hourly(y, weather)
        mta = data.source_data.get(MtaData)
        self._subway(y, mta.boards if mta is not None else [])
        return self.img

    def _paste_icon(self, name: str, cx: float, cy: float, box: int) -> None:
        """Paste icon ``name`` centered on (cx, cy) in a ``box``-px square."""
        gray, mask = _load_icon(name, box)
        self.img.paste(gray, (int(cx - box / 2), int(cy - box / 2)), mask)

    def _raindrop(self, cx: float, cy: float, s: float) -> None:
        """A small teardrop: a pointed top over a round bowl."""
        d = self.d
        d.polygon((cx, cy - s, cx - s * 0.62, cy + s * 0.2, cx + s * 0.62, cy + s * 0.2), fill=INK)
        d.ellipse((cx - s * 0.62, cy - s * 0.25, cx + s * 0.62, cy + s * 0.75), fill=INK)

    def _title(self, y: int, when: datetime) -> int:
        nyc = self.fonts.get(78, "Black")
        self.d.text((_MARGIN, y), "NYC", font=nyc, fill=INK, anchor="la")
        # sit the time on the same baseline as "NYC" (ascent below the cap line)
        baseline = y + nyc.getmetrics()[0]
        label = when.strftime("%a %b %-d, %-I:%M %p")
        self.d.text(
            (self.w - _MARGIN, baseline),
            label,
            font=self.fonts.get(30, "Medium"),
            fill=INK,
            anchor="rs",
        )
        rule = y + 96
        self.d.line((_MARGIN, rule, self.w - _MARGIN, rule), fill=INK, width=5)
        return rule + 28

    def _hero(self, y: int, weather: _GlanceWeather) -> int:
        icon_slot = 260  # horizontal space reserved for the icon at the right
        temp = format_apparent(weather.temperature, self.units)
        temp_font = fit_font(self.fonts, temp, "Black", 190, self.w - 2 * _MARGIN - icon_slot - 30)
        self.d.text((_MARGIN, y), temp, font=temp_font, fill=INK, anchor="la")
        temp_h = int(temp_font.getbbox(temp)[3])

        # wind row beneath the temperature (metric text only, no icon)
        wind_cy = y + temp_h + 40
        wind = format_wind(weather.wind_speed_kmh, weather.wind_direction, self.units)
        self.d.text(
            (_MARGIN, wind_cy), wind, font=self.fonts.get(40, "Medium"), fill=INK, anchor="lm"
        )

        bottom = wind_cy + 46
        # weather icon: as large as the section allows, vertically centered on the right (the
        # adapter already resolved the icon name, handling each provider's conditions vocabulary)
        band_h = bottom - y
        icon_box = int(min(icon_slot, band_h))
        icon_cx = self.w - _MARGIN - icon_slot / 2
        self._paste_icon(weather.icon, icon_cx, y + band_h / 2, icon_box)

        # no rule here: separate the hourly strip from the hero with a little whitespace
        return bottom + 28

    def _hourly(self, y: int, weather: _GlanceWeather) -> int:
        hours = weather.hourly[:4]
        if len(hours) == 0:
            return y
        n = len(hours)
        gap = 44  # gap between adjacent column dividers
        # Columns partition the full content width (first starts at the left margin, last ends at
        # the right margin); content is centered within each column, which stays equal width.
        col_w = (self.w - 2 * _MARGIN - gap * (n - 1)) / n
        for i, h in enumerate(hours):
            x0 = _MARGIN + i * (col_w + gap)
            cx = x0 + col_w / 2
            self.d.text(
                (cx, y),
                h.time.strftime("%-I%p").lower(),
                font=self.fonts.get(34, "Medium"),
                fill=INK,
                anchor="ma",
            )
            self.d.line((x0, y + 48, x0 + col_w, y + 48), fill=INK, width=2)
            # sized a bit under the column width so the temperature keeps a margin inside the column
            temp = format_apparent(h.temperature, self.units)
            temp_font = fit_font(self.fonts, temp, "Medium", 40, col_w - 20)
            self.d.text((cx, y + 60), temp, font=temp_font, fill=INK, anchor="ma")
            # raindrop + precipitation percentage as one group, centered under the column
            pct = f"{h.precip_probability}%" if h.precip_probability is not None else "—"
            pf = self.fonts.get(34, "Regular")
            gx = cx - (24 + pf.getlength(pct)) / 2
            pct_y = y + 134
            self.d.text((gx + 24, pct_y), pct, font=pf, fill=INK, anchor="lm")
            # align the raindrop's mass with the percentage's ink center (its own centroid sits
            # ~0.125*size above its cy, so nudge down to compensate)
            _, ink_top, _, ink_bottom = pf.getbbox(pct, anchor="lm")
            self._raindrop(gx + 8, pct_y + (ink_top + ink_bottom) / 2 + 1.5, 12)
        # extra whitespace between the precip row and the rule dividing hourly from subway
        bottom = y + 200
        self.d.line((_MARGIN, bottom, self.w - _MARGIN, bottom), fill=INK, width=3)
        return bottom + 30

    def _direction_block(
        self, x: float, y: float, w: float, label: str, arrivals: list[TrainArrival], pitch: float
    ) -> float:
        self.d.text((x, y), label, font=self.fonts.get(32, "Bold"), fill=INK, anchor="la")
        y += 52
        if len(arrivals) == 0:
            self.d.text(
                (x + 12, y), "No trains", font=self.fonts.get(34, "Regular"), fill=INK, anchor="la"
            )
            return y + pitch
        # This deterministic layout is sized for 3 rows per direction; it shows the soonest 3 of
        # the (uncapped) board and drops the rest. Truncation is the layout's call, not the fetch's.
        for a in arrivals[:3]:
            # departure clock time on the left, route letter (bold, no badge) on the right; both on
            # a shared baseline so the all-caps route lines up with the time's descenders
            baseline = y + pitch * 0.4 + 16
            clock = f"{a.arrival.strftime('%-I:%M')} {a.arrival.strftime('%p').lower()}"
            self.d.text(
                (x + 6, baseline), clock, font=self.fonts.get(46, "Medium"), fill=INK, anchor="ls"
            )
            # center each route letter on a common x so they line up regardless of glyph width
            self.d.text(
                (x + w - 28, baseline),
                a.route,
                font=self.fonts.get(50, "Black"),
                fill=INK,
                anchor="ms",
            )
            y += pitch
        return y + 8

    def _subway(self, y: int, boards: list[StationBoard]) -> None:
        if len(boards) == 0:
            return
        n = len(boards)
        gutter = 56  # whitespace between station columns
        col_w = (self.w - 2 * _MARGIN - gutter * (n - 1)) / n
        # Distribute the leftover vertical space across the (up to) six arrival rows so the band
        # fills the height instead of clustering at the top. Deterministic layout makes this exact.
        block_gap = 28
        fixed = 68 + 2 * 52 + 2 * 8 + block_gap  # name area, two labels, two trailers, inter-gap
        pitch = max(58, min(96, (self.h - _MARGIN - y - fixed) / 6))
        for i, board in enumerate(boards):
            x = _MARGIN + i * (col_w + gutter)
            self.d.text((x, y), board.label, font=self.fonts.get(44, "Bold"), fill=INK, anchor="la")
            name_b = y + 58
            self.d.line((x, name_b, x + col_w, name_b), fill=INK, width=2)
            cy: float = name_b + 18
            north = board.arrivals_by_direction.get(Direction.NORTH, [])
            south = board.arrivals_by_direction.get(Direction.SOUTH, [])
            cy = self._direction_block(x, cy, col_w, "Uptown", north, pitch)
            self._direction_block(x, cy + block_gap, col_w, "Downtown", south, pitch)


register_layout("glanceable", _Glanceable)
