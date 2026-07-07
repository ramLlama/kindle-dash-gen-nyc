"""The bundled "glanceable" layout — a plugin like any other.

Hero weather, an hourly strip, and per-station arrival boards. This ships with the app but is
structurally identical to a private plugin: it depends only on the public
:mod:`kindle_dash_gen_nyc.render.toolkit` surface, owns its own assets, and registers itself via
:func:`register_layout` at import. It could be dropped verbatim into a private plugins directory.
"""

from __future__ import annotations

from datetime import datetime

from PIL import Image, ImageDraw

from kindle_dash_gen_nyc.models import DashboardData, Direction, TrainArrival, WeatherReport
from kindle_dash_gen_nyc.render.layout import register_layout
from kindle_dash_gen_nyc.render.toolkit import (
    INK,
    PAPER,
    Fonts,
    fit_font,
    format_apparent,
    format_wind,
    load_asset_image,
    weather_icon,
)

_PACKAGE = "kindle_dash_gen_nyc.render.layouts.glanceable"  # this plugin's own package, for assets
_MARGIN = 44


def _load_icon(name: str, size: int) -> tuple[Image.Image, Image.Image]:
    """Load this plugin's icon ``name`` at ``size`` px as (grayscale, alpha mask)."""
    icon = load_asset_image(_PACKAGE, f"assets/icons/{name}.png")
    icon = icon.convert("LA").resize((size, size), Image.LANCZOS)
    return icon.getchannel("L"), icon.getchannel("A")


class _Glanceable:
    """Renders the glanceable layout: hero weather, an hourly strip, and per-station arrival boards.

    Everything is measured against ``width``/``height`` so the layout adapts to the panel size.
    """

    def __init__(self, width: int, height: int, fonts: Fonts, units: str) -> None:
        self.w = width
        self.h = height
        self.fonts = fonts
        self.units = units
        self.img = Image.new("L", (width, height), PAPER)
        self.d = ImageDraw.Draw(self.img)

    def render(self, data: DashboardData) -> Image.Image:
        y = self._title(_MARGIN, data.generated_at)
        if data.weather is not None:
            y = self._hero(y, data.weather)
            y = self._hourly(y, data.weather)
        self._subway(y, data)
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
        self.d.text((self.w - _MARGIN, baseline), label, font=self.fonts.get(30, "Medium"),
                    fill=INK, anchor="rs")
        rule = y + 96
        self.d.line((_MARGIN, rule, self.w - _MARGIN, rule), fill=INK, width=5)
        return rule + 28

    def _hero(self, y: int, weather: WeatherReport) -> int:
        icon_slot = 260  # horizontal space reserved for the icon at the right
        temp = format_apparent(weather.temperature, self.units)
        temp_font = fit_font(self.fonts, temp, "Black", 190, self.w - 2 * _MARGIN - icon_slot - 30)
        self.d.text((_MARGIN, y), temp, font=temp_font, fill=INK, anchor="la")
        temp_h = temp_font.getbbox(temp)[3]

        # wind row beneath the temperature (metric text only, no icon)
        wind_cy = y + temp_h + 40
        wind = format_wind(weather.wind_speed_kmh, weather.wind_direction, self.units)
        self.d.text((_MARGIN, wind_cy), wind, font=self.fonts.get(40, "Medium"),
                    fill=INK, anchor="lm")

        bottom = wind_cy + 46
        # weather icon: as large as the section allows, vertically centered on the right
        band_h = bottom - y
        icon_box = int(min(icon_slot, band_h))
        icon_cx = self.w - _MARGIN - icon_slot / 2
        self._paste_icon(weather_icon(weather), icon_cx, y + band_h / 2, icon_box)

        # no rule here: separate the hourly strip from the hero with a little whitespace
        return bottom + 28

    def _hourly(self, y: int, weather: WeatherReport) -> int:
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
            self.d.text((cx, y), h.time.strftime("%-I%p").lower(),
                        font=self.fonts.get(34, "Medium"), fill=INK, anchor="ma")
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

    def _direction_block(self, x: float, y: float, w: float, label: str,
                         arrivals: list[TrainArrival], pitch: float) -> float:
        self.d.text((x, y), label, font=self.fonts.get(32, "Bold"), fill=INK, anchor="la")
        y += 52
        if len(arrivals) == 0:
            self.d.text((x + 12, y), "No trains", font=self.fonts.get(34, "Regular"),
                        fill=INK, anchor="la")
            return y + pitch
        # This deterministic layout is sized for 3 rows per direction; it shows the soonest 3 of
        # the (uncapped) board and drops the rest. Truncation is the layout's call, not the fetch's.
        for a in arrivals[:3]:
            # departure clock time on the left, route letter (bold, no badge) on the right; both on
            # a shared baseline so the all-caps route lines up with the time's descenders
            baseline = y + pitch * 0.4 + 16
            clock = f"{a.arrival.strftime('%-I:%M')} {a.arrival.strftime('%p').lower()}"
            self.d.text((x + 6, baseline), clock, font=self.fonts.get(46, "Medium"),
                        fill=INK, anchor="ls")
            # center each route letter on a common x so they line up regardless of glyph width
            self.d.text((x + w - 28, baseline), a.route, font=self.fonts.get(50, "Black"),
                        fill=INK, anchor="ms")
            y += pitch
        return y + 8

    def _subway(self, y: int, data: DashboardData) -> None:
        boards = data.boards
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
            self.d.text((x, y), board.name, font=self.fonts.get(44, "Bold"), fill=INK, anchor="la")
            name_b = y + 58
            self.d.line((x, name_b, x + col_w, name_b), fill=INK, width=2)
            cy = name_b + 18
            north = board.arrivals_by_direction.get(Direction.NORTH, [])
            south = board.arrivals_by_direction.get(Direction.SOUTH, [])
            cy = self._direction_block(x, cy, col_w, "Uptown", north, pitch)
            self._direction_block(x, cy + block_gap, col_w, "Downtown", south, pitch)


register_layout("glanceable", _Glanceable)
