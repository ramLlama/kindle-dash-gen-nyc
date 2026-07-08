"""Pillow rendering: the layout registry and dispatch.

A named *layout* draws :class:`DashboardData` directly with Pillow at the device's native
resolution, so the output is exact, free, offline, and never garbles the underlying data.

Layouts are **plugins** — nothing is special-cased here. Each layout (the bundled ``glanceable``
included) is a package under a discovered plugin root that calls :func:`register_layout` at import
time, and **owns its config**: a ``Config`` pydantic model validated against the dashboard's
``[dashboards.<name>.layout_config]`` table (like a source's ``[sources.<name>]`` table). Build one
on the public :mod:`kindle_dash_gen.render.toolkit` surface. See ``docs/plugins.md``.
"""

from __future__ import annotations

from typing import Any, ClassVar, Protocol, TypeVar

from PIL import Image
from pydantic import BaseModel

from ..models import DashboardData
from .toolkit import LayoutError

__all__ = ["Layout", "LayoutError", "build_layout", "register_layout", "render", "validate_layout"]

# The config type a layout is built from. Variance is nominal (it appears only in __init__, which
# Protocols exclude from structural checks), so mypy expects covariant. Mirrors sources/registry.py.
ConfigT_co = TypeVar("ConfigT_co", bound=BaseModel, covariant=True)


class Layout(Protocol[ConfigT_co]):
    """The interface a layout class implements: declare a config model, construct, then render.

    ``Config`` is the pydantic model for this layout's ``layout_config`` table (keep
    ``extra="forbid"``); the dispatch validates the raw table against it. The layout is constructed
    from that validated config plus the panel size, then :meth:`render` draws the dashboard and
    returns a raw ``"L"``-mode :class:`PIL.Image.Image` — the pipeline post-processes and writes it.
    """

    Config: ClassVar[type[BaseModel]]

    def __init__(self, config: ConfigT_co, *, width: int, height: int) -> None: ...

    def render(self, data: DashboardData) -> Image.Image: ...


# Populated only by plugin discovery (see :mod:`kindle_dash_gen.plugins`) — no builtins here.
_LAYOUTS: dict[str, type[Layout[Any]]] = {}


def register_layout(name: str, factory: type[Layout[Any]]) -> None:
    """Register layout class ``factory`` under ``name``; raise on a duplicate name.

    Plugins call this at import time. Duplicate names are a configuration error (two plugins
    claiming the same layout), so they fail fast rather than silently shadowing.
    """
    if name in _LAYOUTS:
        raise LayoutError(f"layout {name!r} is already registered")
    _LAYOUTS[name] = factory


def validate_layout(name: str, raw: dict[str, Any]) -> BaseModel:
    """Validate a ``layout_config`` table against layout ``name``'s ``Config``; return the config.

    Loads the bundled layout plugins first as a safety net (a caller wanting local
    ``plugins_path`` layouts must have loaded those already). Raises :class:`LayoutError` on an
    unknown layout, and the layout's own validation error (``extra="forbid"``) on a bad table.
    """
    from .. import plugins  # lazy import: plugins imports layout modules that import this module

    plugins.load_plugins()
    if name not in _LAYOUTS:
        raise LayoutError(f"unknown layout {name!r}; available: {sorted(_LAYOUTS)}")
    return _LAYOUTS[name].Config.model_validate(raw)


def build_layout(name: str, raw: dict[str, Any], *, width: int, height: int) -> Layout[Any]:
    """Validate ``raw`` against layout ``name``'s ``Config`` and construct it at the panel size."""
    config = validate_layout(name, raw)
    return _LAYOUTS[name](config, width=width, height=height)


def render(
    data: DashboardData, *, width: int, height: int, layout: str, layout_config: dict[str, Any]
) -> Image.Image:
    """Build ``layout`` from ``layout_config`` and draw ``data`` to a raw Pillow Image.

    The image is drawn at the exact panel size; the caller post-processes it (grayscale, fit,
    quantize) and writes it. Raises :class:`LayoutError` on an unknown layout, unresolvable font,
    or missing asset.
    """
    return build_layout(layout, layout_config, width=width, height=height).render(data)
