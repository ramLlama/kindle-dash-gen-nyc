"""The bundled "glanceable" layout — a plugin like any other.

Hero weather, an hourly strip, and per-station arrival boards. This ships with the app but is
structurally identical to a private plugin: it depends only on the public
:mod:`kindle_dash_gen.render.toolkit` surface, owns its own assets, and registers itself via
:func:`register_layout` at import. It could be dropped verbatim into a private plugins directory.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, ConfigDict

from kindle_dash_gen.models import DashboardData
from kindle_dash_gen.render.layout import Layout, register_layout
from kindle_dash_gen.render.toolkit import (
    DEFAULT_FONT,
    INK,
    PAPER,
    Fonts,
    LayoutError,
    aqi_is_unhealthy,
    fit_font,
    format_apparent,
    format_aqi,
    format_wind,
    load_asset_image,
    weather_icon,
)
from kindle_dash_gen.sources.builtins.mta.model import Direction, MtaData, StationBoard
from kindle_dash_gen.sources.builtins.nws.model import LocationWeather as NwsLocationWeather
from kindle_dash_gen.sources.builtins.nws.model import NwsData
from kindle_dash_gen.sources.builtins.open_meteo.model import (
    LocationWeather as OpenMeteoLocationWeather,
)
from kindle_dash_gen.sources.builtins.open_meteo.model import OpenMeteoData
from kindle_dash_gen.sources.builtins.sf_bay_511.model import Agency as SfAgency
from kindle_dash_gen.sources.builtins.sf_bay_511.model import SfBay511Data
from kindle_dash_gen.sources.builtins.sf_bay_511.model import StopBoard as SfBoard

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
    """The weather draw surface, combining whatever providers are present.

    Hero/hourly come from the preferred weather provider; ``aqi`` and ``alerts`` are pulled from
    whichever provider supplies them (Open-Meteo for AQI, NWS for alerts) regardless of which one
    drives the hero. A field is simply absent (``aqi=None`` / ``alerts=()``) when its provider is
    not configured, so the draw code never inspects a provider type.
    """

    temperature: _Temp
    wind_speed_kmh: float | None
    wind_direction: str
    icon: str  # resolved icon name (snow/rain/cloudy/sunny) — provider differences handled here
    hourly: list[_GlanceHour]
    aqi: int | None = None  # US AQI (Open-Meteo only); None when unavailable
    alerts: tuple[str, ...] = ()  # active-alert event names (NWS only), worst severity first


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


def _from_nws(w: NwsLocationWeather) -> _GlanceWeather:
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


def _from_open_meteo(w: OpenMeteoLocationWeather) -> _GlanceWeather:
    # Open-Meteo has no station observation; the icon comes straight from its WMO weather code.
    return _GlanceWeather(
        temperature=_Temp(w.temperature.real, w.temperature.feels_like),
        wind_speed_kmh=w.wind_speed_kmh,
        wind_direction=w.wind_direction,
        icon=_wmo_icon(w.weather_code),
        hourly=_norm_hours(w.hourly),
    )


# CAP severity → rank, so the layout surfaces the worst active alert first (all alerts are carried
# unfiltered by the source; picking which to show is the layout's call).
_SEVERITY_RANK = {"Extreme": 4, "Severe": 3, "Moderate": 2, "Minor": 1, "Unknown": 0}


def _alert_events(nws: NwsLocationWeather) -> tuple[str, ...]:
    """Active-alert event names, most-severe first (ties keep source order)."""
    ordered = sorted(nws.alerts, key=lambda a: _SEVERITY_RANK.get(a.severity, 0), reverse=True)
    return tuple(a.event for a in ordered)


def _weather(data: DashboardData, location: str) -> _GlanceWeather | None:
    """The draw surface for one named location, reconciled across whichever providers cover it.

    ``location`` is the name both weather sources key their results by, so the same "NYC" pairs
    Open-Meteo's forecast with NWS's alerts. Hero/hourly come from the preferred provider
    (Open-Meteo, global; NWS fallback); AQI is Open-Meteo's and alerts are NWS's, pulled
    independently. ``None`` when no configured provider has that location this run (its source was
    absent or failed), so the dashboard simply shows no weather.
    """
    om = data.source_data.get(OpenMeteoData)
    nws = data.source_data.get(NwsData)
    om_loc = om.locations.get(location) if om is not None else None
    nws_loc = nws.locations.get(location) if nws is not None else None
    if om_loc is not None:
        base = _from_open_meteo(om_loc)
    elif nws_loc is not None:
        base = _from_nws(nws_loc)
    else:
        return None
    aqi = om_loc.us_aqi if om_loc is not None else None
    alerts = _alert_events(nws_loc) if nws_loc is not None else ()
    return replace(base, aqi=aqi, alerts=alerts)


# ── Transit adapter ────────────────────────────────────────────────────────────────────────────
# The transit side mirrors the weather adapter above: each provider's own board type is normalized
# into one draw surface, so the drawing code below never inspects a provider type. The providers
# genuinely differ — MTA runs uptown/downtown, while a Bay Area board can carry several
# operators with different direction vocabularies — and that is all resolved into a plain label
# here, before anything is drawn.


@dataclass(frozen=True, kw_only=True)
class _GlanceArrival:
    """One upcoming vehicle: when it arrives and the badge identifying its route."""

    clock: datetime  # aware UTC; converted to the layout's timezone at draw time
    label: str  # MTA route letter ("N"), 511 LineRef ("Green-N")


@dataclass(frozen=True, kw_only=True)
class _GlanceGroup:
    """One direction block: its heading and the arrivals under it."""

    label: str  # "Uptown", "Northbound", "BART Northbound" — already resolved for display
    arrivals: list[_GlanceArrival]


@dataclass(frozen=True, kw_only=True)
class _GlanceBoard:
    """One station column: a heading plus its ordered direction blocks.

    ``groups`` is ordered but *not* truncated: which directions matter is the adapter's call, while
    how many fit is geometry, so the draw code takes the first ``_MAX_BLOCKS``.
    """

    label: str
    groups: list[_GlanceGroup]


# Direction code → what a rider reads. Keyed by the raw string value, so it serves every agency's
# own direction enum at once (they are StrEnums, and "N" means northbound whoever emitted it).
_DIRECTION_LABELS = {
    "N": "Northbound",
    "S": "Southbound",
    "E": "Eastbound",
    "W": "Westbound",
    "IB": "Inbound",
    "OB": "Outbound",
}

# Canonical order for direction blocks, so a board's columns don't reshuffle between runs just
# because the feed happened to list its directions in a different order.
_DIRECTION_ORDER = ("N", "S", "E", "W", "IB", "OB")


def _direction_label(direction: str) -> str:
    """A readable label for a direction code, falling back to the code itself."""
    return _DIRECTION_LABELS.get(str(direction), str(direction))


def _from_mta(boards: list[StationBoard]) -> list[_GlanceBoard]:
    """MTA boards as draw surfaces: always uptown then downtown.

    Both blocks are emitted even when a direction has no trains (it draws "No trains"), which is
    what this layout has always done — the adapter is a re-shaping, not a change in output.
    """
    return [
        _GlanceBoard(
            label=board.label,
            groups=[
                _GlanceGroup(label=label, arrivals=_mta_arrivals(board, direction))
                for label, direction in (("Uptown", Direction.NORTH), ("Downtown", Direction.SOUTH))
            ],
        )
        for board in boards
    ]


def _mta_arrivals(board: StationBoard, direction: Direction) -> list[_GlanceArrival]:
    return [
        _GlanceArrival(clock=a.arrival, label=a.route)
        for a in board.arrivals_by_direction.get(direction, [])
    ]


def _from_sf_bay_511(boards: list[SfBoard]) -> list[_GlanceBoard]:
    """511 boards as draw surfaces, flattening agency → direction into ordered blocks.

    A board can be served by several operators, so each (agency, direction) pair becomes its own
    block. The agency is named in the label only when the board actually spans more than one —
    otherwise every label would carry a redundant prefix.
    """
    surfaces = []
    for board in boards:
        multi_agency = len(board.arrivals) > 1
        groups = []
        for agency in sorted(board.arrivals, key=list(SfAgency).index):
            by_direction = board.arrivals[agency]
            for direction in sorted(by_direction, key=_direction_rank):
                label = _direction_label(direction)
                groups.append(
                    _GlanceGroup(
                        label=f"{agency.label} {label}" if multi_agency else label,
                        arrivals=[
                            _GlanceArrival(clock=a.arrival, label=a.line)
                            for a in by_direction[direction]
                        ],
                    )
                )
        surfaces.append(_GlanceBoard(label=board.label, groups=groups))
    return surfaces


def _direction_rank(direction: str) -> int:
    value = str(direction)
    return _DIRECTION_ORDER.index(value) if value in _DIRECTION_ORDER else len(_DIRECTION_ORDER)


def _transit(data: DashboardData, selected: list[str] | None) -> list[_GlanceBoard]:
    """The transit boards to draw, as one ordered list of draw surfaces.

    ``selected`` is an allowlist of board *names* (the canonical config key, not the display
    label, so renaming the display never breaks the match). ``None`` draws every board a source
    produced; a list keeps only those, in source order — MTA first, so adding a Bay Area source to
    an existing dashboard leaves its columns where they were. This is how one shared fetch feeds
    several dashboards that each show a different slice of the configured stations.
    """
    boards: list[_GlanceBoard] = []
    mta = data.source_data.get(MtaData)
    if mta is not None:
        boards.extend(_from_mta(_select(mta.boards, selected)))
    bay = data.source_data.get(SfBay511Data)
    if bay is not None:
        boards.extend(_from_sf_bay_511(_select(bay.boards, selected)))
    return boards


def _select(boards: list, selected: list[str] | None) -> list:
    """Filter provider boards to the allowlisted names, or all of them when unset."""
    if selected is None:
        return boards
    return [board for board in boards if board.name in selected]


_GUTTER = 56  # whitespace between station columns
_MAX_TRANSIT_BOARDS = 3  # columns the band has room for; a fourth would overlap
_MAX_BLOCKS = 2  # direction blocks a column has room for
_MAX_ARRIVALS = 3  # arrivals shown per direction block
_CLOCK_INDENT = 6  # left inset of an arrival's clock within its column
_BADGE_GAP = 12  # clear space between an arrival's clock and its route badge
_BADGE_INSET = 28  # badge center's offset from the column's right edge
_BADGE_MARGIN = 4  # keep a shrunken badge this far inside the column's right edge
# Floor for the badge's fitting width, chosen so that *neither* badge clamp can touch a label
# narrower than it: at this width the centered badge's right edge lands exactly on the margin.
# That makes "short badges render exactly as they always have" one checkable property rather
# than a coincidence of column count — a 4-column board leaves almost no room beside the clock,
# and without the floor a one-character subway route would shrink to a third of its size.
_BADGE_MIN_WIDTH = 2 * (_BADGE_INSET - _BADGE_MARGIN)
_ROW_SIZE = 40  # nominal type size of the hero's metric rows (wind / AQI / alert)
_ICON_SLOT = 260  # horizontal space reserved for the hero's weather icon, at the right
_WARN_ICON_SCALE = 1.15  # warning icon height as a multiple of cap height, so it reads as equal


def _warn_gutter(font: ImageFont.FreeTypeFont) -> int:
    """Width the warning icon and its trailing space occupy on a metric row set in ``font``."""
    return round(_cap_height(font) * _WARN_ICON_SCALE) + 18


def _alert_label(events: tuple[str, ...]) -> str:
    """The most-severe alert's event name, with a "+N more" tail when others are active."""
    if len(events) > 1:
        return f"{events[0]}  +{len(events) - 1} more"
    return events[0]


def _cap_height(font: ImageFont.FreeTypeFont) -> float:
    """The height of ``font``'s capitals, cap top to baseline."""
    ascent, _ = font.getmetrics()
    return ascent - font.getbbox("H")[1]


def _cap_midline(font: ImageFont.FreeTypeFont, cy: float) -> float:
    """The midline of ``font``'s capitals for text drawn at ``cy`` with an ``lm`` anchor.

    Pillow's ``m`` anchor centers the *em box* (ascent + descent) on ``cy``, so for a face with a
    small descent the visible ink lands well below ``cy``. An icon centered on ``cy`` would ride
    high above the text beside it; centering on the caps instead is what the eye actually reads.
    """
    ascent, descent = font.getmetrics()
    baseline = cy + (ascent + descent) / 2 - descent
    return baseline - _cap_height(font) / 2


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
    # Header text shown at the top-left of the dashboard (e.g. a city or place name). Required.
    title: str
    # IANA zone every displayed time is converted to (e.g. "America/New_York"). Required, and has
    # no default on purpose: sources hand over aware UTC, so without a zone this layout would print
    # UTC clock times. It is what lets one process render a New York and a Bay Area dashboard from
    # a single fetch. pydantic parses the TOML string into a ZoneInfo and rejects an unknown name
    # at config load, so no hand-rolled validator is needed.
    timezone: ZoneInfo
    # Which weather location to draw, by the name a weather source keyed it under. Required and has
    # no default: this layout draws one hero, and a weather source may now offer several locations,
    # so the dashboard must say which. A name no source produced this run renders no weather.
    weather_location: str
    # Which transit boards to draw, by their canonical station names. Omit to draw every board the
    # configured transit sources produced; list names to keep only those (in source order). This is
    # how sibling dashboards fed by one fetch each show their own stations.
    transit_boards: list[str] | None = None
    # Display units for weather temperatures: "us" (°F), "si" (°C), or "both".
    weather_temp_units: Literal["us", "si", "both"] = "us"


class _Glanceable(Layout[GlanceableConfig]):
    """Renders the glanceable layout: hero weather, an hourly strip, and per-station arrival boards.

    Everything is measured against ``width``/``height`` so the layout adapts to the panel size. The
    transit band is three columns wide (across all providers combined); a dashboard whose sources
    produce more boards than that raises :class:`LayoutError` rather than drawing a garbled panel.
    """

    Config = GlanceableConfig

    def __init__(self, config: GlanceableConfig, *, width: int, height: int) -> None:
        self.w = width
        self.h = height
        # No layout-specific font opinion: fall back to the app-wide default when unspecified.
        self.fonts = Fonts(config.font if config.font is not None else DEFAULT_FONT)
        self.units = config.weather_temp_units
        self.title = config.title
        # Every datetime drawn is aware UTC; this is the zone they are shown in.
        self.tz = config.timezone
        self.weather_location = config.weather_location
        self.transit_boards = config.transit_boards
        self.img = Image.new("L", (width, height), PAPER)
        self.d = ImageDraw.Draw(self.img)

    def render(self, data: DashboardData) -> Image.Image:
        y = self._title(_MARGIN, data.generated_at)
        weather = _weather(data, self.weather_location)
        if weather is not None:
            y = self._hero(y, weather)
            y = self._hourly(y, weather)
        self._transit_boards(y, _transit(data, self.transit_boards))
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
        title_font = self.fonts.get(78, "Black")
        self.d.text((_MARGIN, y), self.title, font=title_font, fill=INK, anchor="la")
        # sit the time on the same baseline as the title (ascent below the cap line)
        baseline = y + title_font.getmetrics()[0]
        label = when.astimezone(self.tz).strftime("%a %b %-d, %-I:%M %p")
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
        temp = format_apparent(weather.temperature, self.units)
        temp_font = fit_font(self.fonts, temp, "Black", 190, self.w - 2 * _MARGIN - _ICON_SLOT - 30)
        self.d.text((_MARGIN, y), temp, font=temp_font, fill=INK, anchor="la")
        temp_h = int(temp_font.getbbox(temp)[3])

        # Metric rows stacked beneath the temperature: wind, then AQI and an alert line — each drawn
        # only when its provider supplied it (the adapter leaves them absent otherwise). An
        # unhealthy AQI is flagged like an alert: bold, behind the same warning icon.
        row_cy = y + temp_h + 40
        row_pitch = 52
        wind = format_wind(weather.wind_speed_kmh, weather.wind_direction, self.units)
        self._metric_row(row_cy, wind, "Medium", warn=False)
        if weather.aqi is not None:
            row_cy += row_pitch
            unhealthy = aqi_is_unhealthy(weather.aqi)
            self._metric_row(
                row_cy,
                format_aqi(weather.aqi),
                "Bold" if unhealthy else "Medium",
                warn=unhealthy,
            )
        if len(weather.alerts) > 0:
            row_cy += row_pitch
            self._metric_row(row_cy, _alert_label(weather.alerts), "Bold", warn=True)

        bottom = row_cy + 46
        # weather icon: as large as the section allows, vertically centered on the right (the
        # adapter already resolved the icon name, handling each provider's conditions vocabulary)
        band_h = bottom - y
        icon_box = int(min(_ICON_SLOT, band_h))
        icon_cx = self.w - _MARGIN - _ICON_SLOT / 2
        self._paste_icon(weather.icon, icon_cx, y + band_h / 2, icon_box)

        # no rule here: separate the hourly strip from the hero with a little whitespace
        return bottom + 28

    def _metric_row(self, cy: float, text: str, style: str, *, warn: bool) -> None:
        """A hero metric line at ``cy``: an optional warning icon, then ``text``.

        Text is shrunk to stay clear of the right-side weather icon. A ``warn`` row's icon is sized
        and aligned to the text's caps, so the icon and the text read as one line.
        """
        x = _MARGIN
        if warn:
            # Fit against the gutter the *nominal* size would need, breaking the circularity of
            # sizing the icon off a font that isn't chosen yet. The real icon is sized off the
            # fitted font below, so its gutter is only ever narrower — the text still fits.
            x += _warn_gutter(self.fonts.get(_ROW_SIZE, style))
        font = fit_font(self.fonts, text, style, _ROW_SIZE, self.w - _MARGIN - _ICON_SLOT - x)
        if warn:
            box = round(_cap_height(font) * _WARN_ICON_SCALE)
            self._paste_icon("warning", _MARGIN + box / 2, _cap_midline(font, cy), box)
            x = _MARGIN + _warn_gutter(font)
        self.d.text((x, cy), text, font=font, fill=INK, anchor="lm")

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
                h.time.astimezone(self.tz).strftime("%-I%p").lower(),
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
        self, x: float, y: float, w: float, label: str, arrivals: list[_GlanceArrival], pitch: float
    ) -> float:
        self.d.text((x, y), label, font=self.fonts.get(32, "Bold"), fill=INK, anchor="la")
        y += 52
        if len(arrivals) == 0:
            self.d.text(
                (x + 12, y), "No trains", font=self.fonts.get(34, "Regular"), fill=INK, anchor="la"
            )
            return y + pitch
        # This deterministic layout is sized for _MAX_ARRIVALS rows per direction; it shows the
        # soonest few of the (uncapped) board and drops the rest. Truncation is the layout's
        # call, not the fetch's.
        for a in arrivals[:_MAX_ARRIVALS]:
            # departure clock time on the left, route badge (bold, no chip) on the right; both on
            # a shared baseline so the all-caps route lines up with the time's descenders
            baseline = y + pitch * 0.4 + 16
            local = a.clock.astimezone(self.tz)
            clock = f"{local.strftime('%-I:%M')} {local.strftime('%p').lower()}"
            clock_font = self.fonts.get(46, "Medium")
            self.d.text(
                (x + _CLOCK_INDENT, baseline), clock, font=clock_font, fill=INK, anchor="ls"
            )
            self._route_badge(a.label, x, w, baseline, clock_font.getlength(clock))
            y += pitch
        return y + 8

    def _route_badge(self, label: str, x: float, w: float, baseline: float, clock_w: float) -> None:
        """Draw a route badge at the right of a column, clamped inside it.

        Badges are centered on a common x so they line up down the column regardless of glyph
        width — which is all a one- or two-character subway route needs. A 511 LineRef is a whole
        word ("Yellow-N"), and centering that on the same x pushes half of it past the column into
        the neighbouring one. So the badge shrinks to the space the clock leaves, and its center
        slides left as needed to keep its right edge inside the column.

        Neither clamp touches a label narrower than ``_BADGE_MIN_WIDTH``, which is what keeps
        existing subway boards rendering pixel-for-pixel as before whatever their column count.

        The right edge is always inside the column, even when ``fit_font`` bottoms out at its
        minimum size. The *left* edge is not clamped: a label too wide to fit beside the clock even
        at that minimum would overprint it. Nothing in the bundled sources gets close, but a route
        name of a dozen-plus characters in a four-column board would.
        """
        available = w - _CLOCK_INDENT - clock_w - _BADGE_GAP
        font = fit_font(self.fonts, label, "Black", 50, max(available, _BADGE_MIN_WIDTH))
        center = min(x + w - _BADGE_INSET, x + w - _BADGE_MARGIN - font.getlength(label) / 2)
        self.d.text((center, baseline), label, font=font, fill=INK, anchor="ms")

    def _transit_boards(self, y: int, boards: list[_GlanceBoard]) -> None:
        """Draw one column per board, each with up to two direction blocks.

        ``_MAX_BLOCKS`` is a geometric limit, not a data one: the pitch below divides the remaining
        height by blocks x arrivals, so a board offering more directions shows the ones its adapter
        ranked first.
        """
        if len(boards) == 0:
            return
        if len(boards) > _MAX_TRANSIT_BOARDS:
            # Only known here: a station maps to a board once its source is fetched. Past three
            # columns the clocks and route badges collide, so fail loudly rather than draw a
            # garbled panel — the pipeline isolates this to the offending dashboard.
            raise LayoutError(
                f"glanceable draws at most {_MAX_TRANSIT_BOARDS} transit boards, "
                f"got {len(boards)}; reduce the stations this dashboard's sources configure"
            )
        n = len(boards)
        col_w = (self.w - 2 * _MARGIN - _GUTTER * (n - 1)) / n
        # Distribute the leftover vertical space across every arrival row so the band fills the
        # height instead of clustering at the top. Deterministic layout makes this exact.
        block_gap = 28
        # name area, then each block's label and trailer, then the gap between the two blocks
        fixed = 68 + _MAX_BLOCKS * (52 + 8) + block_gap
        pitch = max(58, min(96, (self.h - _MARGIN - y - fixed) / (_MAX_BLOCKS * _MAX_ARRIVALS)))
        for i, board in enumerate(boards):
            x = _MARGIN + i * (col_w + _GUTTER)
            self.d.text((x, y), board.label, font=self.fonts.get(44, "Bold"), fill=INK, anchor="la")
            name_b = y + 58
            self.d.line((x, name_b, x + col_w, name_b), fill=INK, width=2)
            cy: float = name_b + 18
            for block, group in enumerate(board.groups[:_MAX_BLOCKS]):
                if block > 0:
                    cy += block_gap
                cy = self._direction_block(x, cy, col_w, group.label, group.arrivals, pitch)


register_layout("glanceable", _Glanceable)
