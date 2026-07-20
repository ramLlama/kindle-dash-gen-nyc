"""End-to-end dashboard pipeline: gather → render → post-process → write.

Runs as a single one-shot or on an interval loop. Each data source is isolated: a source
that fails degrades the dashboard (drops its panel) rather than aborting the whole render.
In the loop, a wholly failed iteration is logged and retried at the next interval so a
transient outage never kills the runner.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image

from . import plugins
from .config import Config, Dashboard
from .models import DashboardData
from .render import layout
from .render.postprocess import post_process
from .sources.registry import SourceError, build_sources

log = logging.getLogger(__name__)


async def gather(cfg: Config) -> DashboardData:
    """Fetch every configured source for one render, concurrently, isolating each.

    Sources are discovered plugins: each ``[sources.<name>]`` is validated, constructed, and
    fetched. All fetches run concurrently (``asyncio.gather``); results are then reduced in
    ``build_sources`` order so the outcome is deterministic regardless of completion order. A source
    that raises a :class:`SourceError` drops its data (logged) and the render proceeds with whatever
    else was gathered; any other exception is not isolated and propagates. The result keys each
    produced data class to its instance (see :class:`DashboardData`); a failed or empty source is
    simply absent.
    """
    # Discover local (plugins_path) sources too, not just the bundled ones build_sources loads.
    plugins.load_plugins(cfg.plugins_path)
    # Aware UTC: every datetime in the app is aware UTC (see the Source protocol), so a dashboard
    # can mix sources in different regions and a layout converts to its own display timezone.
    now = datetime.now(UTC)
    resolved = build_sources(cfg.sources)
    names = list(resolved)
    results = await asyncio.gather(
        *(source_cls(source_cfg).fetch(now) for source_cls, source_cfg in resolved.values()),
        return_exceptions=True,
    )

    source_data: dict[type, Any] = {}
    # Deterministic reduce: iterate in build_sources order, not completion order.
    for name, result in zip(names, results, strict=True):
        if isinstance(result, SourceError):
            log.warning("source %r unavailable (%s); omitting its data", name, result)
            continue
        if isinstance(result, BaseException):
            raise result  # only SourceError is isolated; anything else is a real bug — fail loud
        if result is not None:
            # source_data is keyed by produced type, so two sources producing the same class would
            # silently clobber. That's a misconfiguration, not a degraded source, so fail loud.
            key = type(result)
            if key in source_data:
                raise SourceError(
                    f"source {name!r} produced {key.__name__}, already provided by another source"
                )
            source_data[key] = result
            log.info("source %r ok (%s)", name, key.__name__)
    return DashboardData(generated_at=now, source_data=source_data)


def render(cfg: Config, data: DashboardData, dash: Dashboard) -> bytes:
    """Draw the dashboard with its layout and return a Kindle-ready PNG.

    ``render_raw`` draws ``data`` at the panel size; ``post_process`` then grayscales, fits, and
    quantizes to the device's gray levels. The image is already exact-sized, so the fit step is a
    no-op and only the quantization matters.
    """
    image = render_raw(cfg, data, dash)
    log.info(
        "post-processing to %dx%d, %d gray levels (%s)",
        dash.width,
        dash.height,
        dash.gray_levels,
        dash.post_process_method,
    )
    return post_process(
        image,
        width=dash.width,
        height=dash.height,
        gray_levels=dash.gray_levels,
        method=dash.post_process_method,
        rotate=dash.rotate,
    )


def render_raw(cfg: Config, data: DashboardData, dash: Dashboard) -> Image.Image:
    """Draw the dashboard with its layout — the raw Pillow image, before Kindle post-processing."""
    # Register bundled + any configured local layout plugins before the layout is looked up.
    plugins.load_plugins(cfg.plugins_path)
    log.info("rendering image via layout %r", dash.layout)
    return layout.render(
        data,
        width=dash.width,
        height=dash.height,
        layout=dash.layout,
        layout_config=dash.layout_config,
    )


@dataclass(frozen=True)
class RunResult:
    """Outcome of one :func:`run_once`: which dashboards were written and which failed.

    ``written`` and ``failed`` are both empty when the render was skipped because every source was
    unavailable (a legitimate no-op, distinct from ``failed`` being non-empty).
    """

    written: list[Path]
    failed: list[str]  # names of dashboards whose render/write raised


async def run_once(cfg: Config) -> RunResult:
    """Gather once, then render and write every configured dashboard; report the outcome.

    Data is fetched a single time and shared across all dashboards. If every source failed (empty
    ``source_data``), the render is skipped entirely: writing a blank dashboard would clobber the
    last good images, so the previous outputs are left in place and an empty :class:`RunResult` is
    returned. Dashboards are isolated from one another — a render or
    write failure for one is logged (with its name) and the remaining dashboards still render; the
    failed names are returned so a one-shot caller can exit non-zero.
    """
    log.info("dashboard render starting for %d dashboard(s)", len(cfg.dashboards))
    data = await gather(cfg)
    if len(data.source_data) == 0:
        log.warning("all sources unavailable; keeping the last dashboard image(s)")
        return RunResult(written=[], failed=[])
    written: list[Path] = []
    failed: list[str] = []
    for name, dash in cfg.dashboards.items():
        try:
            png = render(cfg, data, dash)
            dash.output_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(dash.output_path, png)
            log.info("wrote dashboard %r to %s", name, dash.output_path)
            written.append(dash.output_path)
        except Exception:  # isolate one dashboard's failure from the others
            log.exception("dashboard %r failed to render; skipping it this iteration", name)
            failed.append(name)
    return RunResult(written=written, failed=failed)


def _atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically (temp file in the same dir, then a rename).

    A crash or kill mid-write leaves the previous image intact rather than a truncated PNG.
    ``Path.replace`` is an atomic rename within the same filesystem.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


async def run(cfg: Config) -> None:
    """Regenerate the dashboard every ``interval_minutes`` until interrupted.

    A failed iteration is logged and retried at the next interval; Ctrl-C exits cleanly.
    """
    interval = cfg.schedule.interval_minutes * 60
    log.info(
        "starting dashboard loop (every %d min); Ctrl-C to stop", cfg.schedule.interval_minutes
    )
    try:
        while True:
            try:
                await run_once(cfg)
            except Exception:  # any source/render failure — keep the loop alive
                log.exception("dashboard render failed; retrying next interval")
            await asyncio.sleep(interval)
    except KeyboardInterrupt:
        log.info("stopping dashboard loop")
