"""Pillow rendering backend: the layout registry and dispatch.

The alternative to the LLM backend (:mod:`kindle_dash_gen.render.openrouter`). A named *layout*
draws :class:`DashboardData` directly with Pillow at the device's native resolution, so the output
is exact, free, offline, and never garbles the underlying data.

Layouts are **plugins** — nothing is special-cased here. Each layout (the bundled ``glanceable``
included) is a package under a discovered plugin root that calls :func:`register_layout` at import
time; :func:`render` loads them on first use. Build one on the public
:mod:`kindle_dash_gen.render.toolkit` surface. See ``docs/plugins.md``.
"""

from __future__ import annotations

from io import BytesIO
from typing import Protocol

from PIL import Image

from ..models import DashboardData
from .toolkit import LayoutError

__all__ = ["Layout", "LayoutError", "register_layout", "render"]


class Layout(Protocol):
    """The interface a layout class implements: construct with panel size + font, then render.

    ``font`` is the dashboard's configured font family, or ``None`` when unspecified. A layout
    resolves it into :class:`~kindle_dash_gen.render.toolkit.Fonts` itself, so it can supply its
    own default(s) for the ``None`` case (e.g. ``Fonts(font or DEFAULT_FONT)``).
    """

    def __init__(self, width: int, height: int, font: str | None, units: str) -> None: ...

    def render(self, data: DashboardData) -> Image.Image:
        """Draw ``data`` and return an ``"L"``-mode image of the constructed panel size."""
        ...


# Populated only by plugin discovery (see :mod:`kindle_dash_gen.plugins`) — no builtins here.
_LAYOUTS: dict[str, type[Layout]] = {}


def register_layout(name: str, factory: type[Layout]) -> None:
    """Register layout class ``factory`` under ``name``; raise on a duplicate name.

    Plugins call this at import time. Duplicate names are a configuration error (two plugins
    claiming the same layout), so they fail fast rather than silently shadowing.
    """
    if name in _LAYOUTS:
        raise LayoutError(f"layout {name!r} is already registered")
    _LAYOUTS[name] = factory


def render(
    data: DashboardData, *, units: str, width: int, height: int, layout: str, font: str | None
) -> bytes:
    """Render ``data`` to a grayscale PNG (bytes) at ``width``×``height`` using ``layout``.

    Loads the bundled layout plugins on first use, then dispatches by name. The image is drawn at
    the exact panel size; the caller still post-processes it to quantize to the device's gray
    levels (the fit step is then a no-op). Raises :class:`LayoutError` on an unknown layout,
    unresolvable font, or missing asset.
    """
    from .. import plugins  # lazy import: plugins imports layout modules that import this module

    plugins.load_plugins()  # safety net for direct/test callers; pipeline also loads local plugins
    if layout not in _LAYOUTS:
        raise LayoutError(f"unknown layout {layout!r}; available: {sorted(_LAYOUTS)}")
    image = _LAYOUTS[layout](width, height, font, units).render(data)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
