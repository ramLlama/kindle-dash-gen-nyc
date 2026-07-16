"""Public toolkit for building pillow render layouts (bundled or private plugins).

A layout plugin draws :class:`~kindle_dash_gen.models.DashboardData` with Pillow at the
device's native size. This module is the stable surface a plugin builds on: font resolution
(:class:`Fonts`), the ink/paper constants, a shrink-to-fit helper (:func:`fit_font`), and an asset
loader (:func:`load_asset_image`). Everything the bundled ``glanceable`` layout uses comes from
here, so any private plugin can be built (or ``glanceable`` recreated) with only this public API.
"""

from __future__ import annotations

import subprocess
from importlib.resources import files

from PIL import Image, ImageFont

# Display formatters re-exported as part of the public plugin surface. Layouts must render through
# these (never convert units themselves) to honor the "SI internally, round at display" invariant;
# `weather_icon(observed, conditions, raining)` maps condition text to a bundled-icon name.
from ..format import (
    format_apparent,
    format_eta,
    format_reading,
    format_temp,
    format_wind,
    weather_icon,
)

__all__ = [
    "DEFAULT_FONT",
    "INK",
    "PAPER",
    "Fonts",
    "LayoutError",
    "fit_font",
    "load_asset_image",
    "format_apparent",
    "format_eta",
    "format_reading",
    "format_temp",
    "format_wind",
    "weather_icon",
]

INK, PAPER = 0, 255  # black ink, white paper (8-bit grayscale)

# The app-wide fallback font family, used when a dashboard leaves `font` unspecified and the layout
# has no font opinion of its own. A layout distinguishes "unspecified" (its constructor's `font` is
# None) from an explicit family, so it can pick a different default; `glanceable` falls back here.
DEFAULT_FONT = "Adwaita Sans"

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


class Fonts:
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


def fit_font(
    fonts: Fonts, text: str, weight: str, max_size: int, max_width: float
) -> ImageFont.FreeTypeFont:
    """Largest ``fonts`` face (from ``max_size`` down) at which ``text`` fits ``max_width``."""
    size = max_size
    while size > 12:
        f = fonts.get(size, weight)
        if f.getlength(text) <= max_width:
            return f
        size -= 4
    return fonts.get(size, weight)


def load_asset_image(package: str, rel_path: str) -> Image.Image:
    """Load a bundled image resource ``rel_path`` from ``package`` as a fully-read PIL image.

    ``package`` is an importable package name (e.g. a plugin's own package) and ``rel_path`` is a
    path within it (e.g. ``"assets/icons/sunny.png"``). Raises :class:`LayoutError` if absent.
    """
    resource = files(package).joinpath(rel_path)
    if not resource.is_file():
        raise LayoutError(f"missing asset {rel_path!r} in package {package!r}")
    with resource.open("rb") as f:
        image = Image.open(f)
        image.load()
    return image


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
        raise LayoutError(
            f"font {family!r} is not installed (fc-match substituted "
            f"{got_family!r}); install it or set a different font in the layout's config"
        )
    return path, index
