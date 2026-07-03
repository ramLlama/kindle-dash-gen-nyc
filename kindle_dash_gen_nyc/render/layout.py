"""Deterministic Pillow rendering backend: draw the dashboard as a Kindle-ready grayscale image.

The alternative to the LLM backend (:mod:`kindle_dash_gen_nyc.render.openrouter`). A named layout
draws :class:`DashboardData` directly with Pillow at the device's native resolution, so the output
is exact, free, offline, and never garbles the underlying data. Fonts are resolved from the system
by family name via fontconfig; weather icons are bundled PNGs under ``assets/icons``.
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from importlib.resources import files
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from ..format import format_apparent, format_wind, weather_icon
from ..models import DashboardData, Direction, TrainArrival, WeatherReport

_MARGIN = 44
_INK, _PAPER = 0, 255
_ICON_DIR = "assets/icons"
# Our weight names -> candidate fontconfig style names (tried first, in order), then a fontconfig
# weight token (fallback). Style matching is robust to fonts whose OS/2 weight metadata is unusable
# (e.g. every face reporting the same weight); the weight fallback covers families that only expose
# weights, and picks the nearest available weight when a named style is absent.
_WEIGHT_STYLES = {
    "Regular": ("Regular", "Book"),
    "Medium": ("Medium",),
    "SemiBold": ("SemiBold", "Semibold", "DemiBold"),
    "Bold": ("Bold",),
    "Black": ("Black", "Heavy"),
}
_WEIGHT_TOKENS = {
    "Regular": "regular",
    "Medium": "medium",
    "SemiBold": "semibold",
    "Bold": "bold",
    "Black": "black",
}


class LayoutError(RuntimeError):
    """A layout could not be rendered (unknown layout, unresolvable font, or missing asset)."""


# --- font + icon resources -------------------------------------------------------------------


class _Fonts:
    """Loads and caches faces of one system font family, one entry per (size, weight)."""

    def __init__(self, family: str) -> None:
        self._family = family
        self._cache: dict[tuple[int, str], ImageFont.FreeTypeFont] = {}

    def get(self, size: int, weight: str = "Regular") -> ImageFont.FreeTypeFont:
        key = (size, weight)
        if key not in self._cache:
            path, index = _resolve_face(self._family, weight)
            self._cache[key] = ImageFont.truetype(path, size, index=index)
        return self._cache[key]


def _norm(text: str) -> str:
    """Normalize a family/style token for comparison (case- and space-insensitive)."""
    return text.lower().replace(" ", "")


def _fc_match(pattern: str) -> tuple[str, str, str, int] | None:
    """Run ``fc-match`` on a fontconfig ``pattern``; return (family, style, file, index) or None.

    None means fc-match produced no usable line. A missing ``fc-match`` binary is fatal.
    """
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{family}\t%{style}\t%{file}\t%{index}", pattern],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise LayoutError("fontconfig 'fc-match' is required to resolve fonts") from exc
    parts = result.stdout.rstrip("\n").split("\t")
    if result.returncode != 0 or len(parts) != 4:
        return None
    family, style, path, index = parts
    return family, style, path, int(index)


def _resolve_face(family: str, weight: str) -> tuple[str, int]:
    """Resolve ``(family, weight)`` to a ``(font file, face index)`` via fontconfig's ``fc-match``.

    Prefers an exact **style-name** match (e.g. "Semibold", "Heavy") because some fonts carry
    unusable weight metadata; falls back to fontconfig **weight** matching for families that only
    expose weights (or to select the nearest weight when the style is absent). The index selects a
    variable font's named instance, so both paths work for variable and per-weight-file families.
    Because fc-match always returns a best match, a missing/misspelled ``family`` would silently
    substitute another font; we verify the resolved family and fail fast instead.
    """
    # 1. Exact style-name match, robust to fonts whose weight metadata collapses every face.
    for style in _WEIGHT_STYLES[weight]:
        match = _fc_match(f"{family}:style={style}")
        if match is None:
            continue
        got_family, got_style, path, index = match
        got_styles = {_norm(s) for s in got_style.split(",")}
        if _norm(family) in _norm(got_family) and _norm(style) in got_styles:
            return path, index
    # 2. Fall back to weight matching; this is also where a substituted (missing) family fails fast.
    match = _fc_match(f"{family}:weight={_WEIGHT_TOKENS[weight]}")
    if match is None:
        raise LayoutError(f"could not resolve font {family!r} via fontconfig")
    got_family, _got_style, path, index = match
    if _norm(family) not in _norm(got_family):
        raise LayoutError(f"font {family!r} is not installed (fc-match substituted "
                          f"{got_family!r}); install it or set dashboard.font")
    return path, index


def _load_icon(name: str, size: int) -> tuple[Image.Image, Image.Image]:
    """Load bundled icon ``name`` at ``size`` px as (grayscale, alpha mask)."""
    resource = files("kindle_dash_gen_nyc").joinpath(_ICON_DIR, f"{name}.png")
    if not resource.is_file():
        raise LayoutError(f"missing bundled icon asset {name!r}")
    with resource.open("rb") as f:
        icon = Image.open(f)
        icon.load()
    icon = icon.convert("LA").resize((size, size), Image.LANCZOS)
    return icon.getchannel("L"), icon.getchannel("A")


# --- the "glanceable" layout -----------------------------------------------------------------


class _Glanceable:
    """Renders the glanceable layout: hero weather, an hourly strip, and per-station arrival boards.

    Everything is measured against ``width``/``height`` so the layout adapts to the panel size.
    """

    def __init__(self, width: int, height: int, fonts: _Fonts, units: str) -> None:
        self.w = width
        self.h = height
        self.fonts = fonts
        self.units = units
        self.img = Image.new("L", (width, height), _PAPER)
        self.d = ImageDraw.Draw(self.img)

    def render(self, data: DashboardData) -> Image.Image:
        y = self._title(_MARGIN, data.generated_at)
        if data.weather is not None:
            y = self._hero(y, data.weather)
            y = self._hourly(y, data.weather)
        self._subway(y, data)
        return self.img

    def _fit_width(self, text: str, weight: str, max_size: int, max_width: float
                   ) -> ImageFont.FreeTypeFont:
        """Largest face (down from ``max_size``) at which ``text`` fits within ``max_width``."""
        size = max_size
        while size > 12:
            f = self.fonts.get(size, weight)
            if f.getlength(text) <= max_width:
                return f
            size -= 4
        return self.fonts.get(size, weight)

    def _paste_icon(self, name: str, cx: float, cy: float, box: int) -> None:
        """Paste bundled icon ``name`` centered on (cx, cy) in a ``box``-px square."""
        gray, mask = _load_icon(name, box)
        self.img.paste(gray, (int(cx - box / 2), int(cy - box / 2)), mask)

    def _raindrop(self, cx: float, cy: float, s: float) -> None:
        """A small teardrop: a pointed top over a round bowl."""
        d = self.d
        d.polygon((cx, cy - s, cx - s * 0.62, cy + s * 0.2, cx + s * 0.62, cy + s * 0.2), fill=_INK)
        d.ellipse((cx - s * 0.62, cy - s * 0.25, cx + s * 0.62, cy + s * 0.75), fill=_INK)

    def _title(self, y: int, when: datetime) -> int:
        nyc = self.fonts.get(78, "Black")
        self.d.text((_MARGIN, y), "NYC", font=nyc, fill=_INK, anchor="la")
        # sit the time on the same baseline as "NYC" (ascent below the cap line)
        baseline = y + nyc.getmetrics()[0]
        label = when.strftime("%a %b %-d, %-I:%M %p")
        self.d.text((self.w - _MARGIN, baseline), label, font=self.fonts.get(30, "Medium"),
                    fill=_INK, anchor="rs")
        rule = y + 96
        self.d.line((_MARGIN, rule, self.w - _MARGIN, rule), fill=_INK, width=5)
        return rule + 28

    def _hero(self, y: int, weather: WeatherReport) -> int:
        icon_slot = 260  # horizontal space reserved for the icon at the right
        temp = format_apparent(weather.temperature, self.units)
        temp_font = self._fit_width(temp, "Black", 190, self.w - 2 * _MARGIN - icon_slot - 30)
        self.d.text((_MARGIN, y), temp, font=temp_font, fill=_INK, anchor="la")
        temp_h = temp_font.getbbox(temp)[3]

        # wind row beneath the temperature (metric text only, no icon)
        wind_cy = y + temp_h + 40
        wind = format_wind(weather.wind_speed_kmh, weather.wind_direction, self.units)
        self.d.text((_MARGIN, wind_cy), wind, font=self.fonts.get(40, "Medium"),
                    fill=_INK, anchor="lm")

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
                        font=self.fonts.get(34, "Medium"), fill=_INK, anchor="ma")
            self.d.line((x0, y + 48, x0 + col_w, y + 48), fill=_INK, width=2)
            # sized a bit under the column width so the temperature keeps a margin inside the column
            temp = format_apparent(h.temperature, self.units)
            self.d.text((cx, y + 60), temp, font=self._fit_width(temp, "Medium", 40, col_w - 20),
                        fill=_INK, anchor="ma")
            # raindrop + precipitation percentage as one group, centered under the column
            pct = f"{h.precip_probability}%" if h.precip_probability is not None else "—"
            pf = self.fonts.get(34, "Regular")
            gx = cx - (24 + pf.getlength(pct)) / 2
            pct_y = y + 134
            self.d.text((gx + 24, pct_y), pct, font=pf, fill=_INK, anchor="lm")
            # align the raindrop's mass with the percentage's ink center (its own centroid sits
            # ~0.125*size above its cy, so nudge down to compensate)
            _, ink_top, _, ink_bottom = pf.getbbox(pct, anchor="lm")
            self._raindrop(gx + 8, pct_y + (ink_top + ink_bottom) / 2 + 1.5, 12)
        # extra whitespace between the precip row and the rule dividing hourly from subway
        bottom = y + 200
        self.d.line((_MARGIN, bottom, self.w - _MARGIN, bottom), fill=_INK, width=3)
        return bottom + 30

    def _direction_block(self, x: float, y: float, w: float, label: str,
                         arrivals: list[TrainArrival], pitch: float) -> float:
        self.d.text((x, y), label, font=self.fonts.get(32, "Bold"), fill=_INK, anchor="la")
        y += 52
        if len(arrivals) == 0:
            self.d.text((x + 12, y), "No trains", font=self.fonts.get(34, "Regular"),
                        fill=_INK, anchor="la")
            return y + pitch
        # Fixed cap of 3 rows per direction: this deterministic layout is sized for 3 (independent
        # of Station.max_arrivals, which the gathered board already applies); extra arrivals drop.
        for a in arrivals[:3]:
            # departure clock time on the left, route letter (bold, no badge) on the right; both on
            # a shared baseline so the all-caps route lines up with the time's descenders
            baseline = y + pitch * 0.4 + 16
            clock = f"{a.arrival.strftime('%-I:%M')} {a.arrival.strftime('%p').lower()}"
            self.d.text((x + 6, baseline), clock, font=self.fonts.get(46, "Medium"),
                        fill=_INK, anchor="ls")
            # center each route letter on a common x so they line up regardless of glyph width
            self.d.text((x + w - 28, baseline), a.route, font=self.fonts.get(50, "Black"),
                        fill=_INK, anchor="ms")
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
            self.d.text((x, y), board.name, font=self.fonts.get(44, "Bold"), fill=_INK, anchor="la")
            name_b = y + 58
            self.d.line((x, name_b, x + col_w, name_b), fill=_INK, width=2)
            cy = name_b + 18
            north = board.arrivals_by_direction.get(Direction.NORTH, [])
            south = board.arrivals_by_direction.get(Direction.SOUTH, [])
            cy = self._direction_block(x, cy, col_w, "Uptown", north, pitch)
            self._direction_block(x, cy + block_gap, col_w, "Downtown", south, pitch)


_LAYOUTS = {"glanceable": _Glanceable}


def render(data: DashboardData, *, units: str, width: int, height: int, layout: str,
           font: str) -> bytes:
    """Render ``data`` to a grayscale PNG (bytes) at ``width``×``height`` using ``layout``.

    The image is drawn at the exact panel size; the caller still post-processes it to quantize to
    the device's gray levels (the fit step is then a no-op). Raises :class:`LayoutError` on an
    unknown layout, unresolvable font, or missing icon asset.
    """
    if layout not in _LAYOUTS:
        raise LayoutError(f"unknown layout {layout!r}; available: {sorted(_LAYOUTS)}")
    image = _LAYOUTS[layout](width, height, _Fonts(font), units).render(data)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
