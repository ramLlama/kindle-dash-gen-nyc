"""Render the OpenRouter image-generation prompt from gathered dashboard data.

Template context contract (public — custom templates depend on this):

Variables:
    weather -- ``WeatherReport | None``
    boards  -- ``list[StationBoard]``
    units   -- display units, ``"us"``, ``"si"``, or ``"both"``
    width   -- target image width, px
    height  -- target image height, px
    aspect  -- resolved aspect ratio string, e.g. ``"4:3"``
    now     -- ``datetime``, equal to ``data.generated_at`` (for ETA formatting)

Helper globals (the same :mod:`kindle_dash_gen.format` functions the debug CLIs use, so
display formatting has one source of truth):
    ``format_reading(temp, units)`` (real, with feels-like in brackets),
    ``format_apparent(temp, units)`` (feels-like only, falling back to real),
    ``format_temp(celsius, units)``, ``format_wind(kmh, direction, units)``,
    ``format_eta(arrival, now)``,
    ``weather_icon(report)`` (one of ``"sunny"``/``"cloudy"``/``"rain"``/``"snow"``).
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from jinja2 import Environment

from ..format import (
    format_apparent,
    format_eta,
    format_reading,
    format_temp,
    format_wind,
    weather_icon,
)
from ..models import DashboardData

_TEMPLATE_DIR = "assets/dashboard_prompts"
_TEMPLATE_SUFFIX = ".j2"

_ENV = Environment(autoescape=False, trim_blocks=True, lstrip_blocks=True)
_ENV.globals.update(
    format_reading=format_reading,
    format_apparent=format_apparent,
    format_temp=format_temp,
    format_wind=format_wind,
    format_eta=format_eta,
    weather_icon=weather_icon,
)


def render_prompt(
    data: DashboardData,
    *,
    units: str,
    width: int,
    height: int,
    aspect: str,
    template: str = "dense",
) -> str:
    """Render the image-generation prompt for ``data`` using ``template``.

    ``template`` is a bundled name (see :func:`_bundled_template_names`) or a filesystem path.
    """
    source = _load_template_source(template)
    jinja_template = _ENV.from_string(source)
    context = _build_context(data, units=units, width=width, height=height, aspect=aspect)
    return jinja_template.render(**context)


def _build_context(
    data: DashboardData, *, units: str, width: int, height: int, aspect: str
) -> dict:
    """Build the public template context (see the module docstring for the contract)."""
    return {
        "weather": data.weather,
        "boards": data.boards,
        "units": units,
        "width": width,
        "height": height,
        "aspect": aspect,
        "now": data.generated_at,
    }


def _bundled_template_names() -> list[str]:
    """Stems of every bundled prompt template under ``assets/dashboard_prompts/*.j2``."""
    directory = files("kindle_dash_gen").joinpath(_TEMPLATE_DIR)
    return sorted(
        entry.name.removesuffix(_TEMPLATE_SUFFIX)
        for entry in directory.iterdir()
        if entry.name.endswith(_TEMPLATE_SUFFIX)
    )


def _load_template_source(spec: str) -> str:
    """Resolve ``spec`` to template source: a bundled name first, else a filesystem path."""
    bundled = files("kindle_dash_gen").joinpath(_TEMPLATE_DIR, f"{spec}{_TEMPLATE_SUFFIX}")
    if bundled.is_file():
        return bundled.read_text()
    path = Path(spec)
    if path.is_file():
        return path.read_text()
    raise ValueError(
        f"unknown prompt template {spec!r}; bundled templates: {_bundled_template_names()}"
    )
