"""End-to-end dashboard pipeline: gather → prompt → generate → post-process → write.

Runs as a single one-shot or on an interval loop. Each data source is isolated: a source
that fails degrades the dashboard (drops its panel) rather than aborting the whole render.
In the loop, a wholly failed iteration is logged and retried at the next interval so a
transient outage never kills the runner.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import plugins
from .config import Config, Dashboard
from .models import DashboardData, StationBoard
from .render import layout
from .render.openrouter import OpenRouterClient
from .render.postprocess import post_process
from .render.prompt import render_prompt
from .sources.mta import MtaClient, MtaError
from .sources.weather import NwsClient, WeatherError

log = logging.getLogger(__name__)


def gather(cfg: Config) -> DashboardData:
    """Fetch weather and subway data for one render, isolating each source.

    A weather failure drops the weather panel; a subway failure drops the arrival boards.
    Either degradation is logged and the render proceeds with whatever was gathered.
    """
    weather_client = NwsClient(
        cfg.weather.user_agent, cfg.weather.rollover_hour, cfg.weather.hourly_hours
    )
    log.info("fetching weather for %s,%s", cfg.location.latitude, cfg.location.longitude)
    try:
        weather = weather_client.fetch(cfg.location.latitude, cfg.location.longitude)
        if weather is not None:
            log.info("weather ok: %s", weather.conditions)
        else:
            log.info("weather fetch returned no data")
    except WeatherError as exc:
        log.warning("weather unavailable (%s); omitting weather panel", exc)
        weather = None

    log.info("fetching subway arrivals for %d station(s)", len(cfg.stations))
    try:
        boards = MtaClient(cfg.stations).fetch()
        log.info(
            "subway ok: %d board(s), %d upcoming arrival(s)", len(boards), _count_arrivals(boards)
        )
    except MtaError as exc:
        log.warning("subway unavailable (%s); omitting arrival boards", exc)
        boards = []
    return DashboardData(weather=weather, boards=boards, generated_at=datetime.now())


def _count_arrivals(boards: list[StationBoard]) -> int:
    """Total upcoming arrivals across every board and direction (for a log summary)."""
    return sum(
        len(arrivals) for board in boards for arrivals in board.arrivals_by_direction.values()
    )


def build_prompt(
    cfg: Config, data: DashboardData, client: OpenRouterClient, dash: Dashboard
) -> tuple[str, str]:
    """Resolve the model's aspect ratio and render the OpenRouter prompt for ``data``.

    Returns ``(prompt, aspect_ratio)`` so the caller can pass the same aspect on to generate().
    """
    assert cfg.openrouter is not None  # only called for the llm backend, which requires it
    aspect = client.resolve_aspect_ratio(dash.width, dash.height, dash.aspect_ratio)
    prompt = render_prompt(
        data,
        units=cfg.weather.units,
        width=dash.width,
        height=dash.height,
        aspect=aspect,
        template=cfg.openrouter.prompt_template,
    )
    return prompt, aspect


def render(cfg: Config, data: DashboardData, dash: Dashboard) -> bytes:
    """Render ``data`` into a Kindle-ready PNG via ``dash``'s backend, then post-process.

    Both backends produce raw PNG bytes; ``post_process`` then grayscales, fits, and quantizes to
    the device's gray levels. For the pillow backend the image is already the exact panel size, so
    the fit step is a no-op and only the quantization matters.
    """
    raw = render_raw(cfg, data, dash)
    log.info(
        "post-processing %d bytes to %dx%d, %d gray levels (%s)",
        len(raw),
        dash.width,
        dash.height,
        dash.gray_levels,
        dash.post_process_method,
    )
    return post_process(
        raw,
        width=dash.width,
        height=dash.height,
        gray_levels=dash.gray_levels,
        method=dash.post_process_method,
        rotate=dash.rotate,
    )


def render_raw(cfg: Config, data: DashboardData, dash: Dashboard) -> bytes:
    """Render raw PNG bytes via ``dash``'s backend, before Kindle post-processing."""
    # Register bundled + any configured local layout plugins before a pillow layout is looked up.
    plugins.load_plugins(cfg.plugins_path)
    if dash.backend == "pillow":
        return _render_pillow(cfg, data, dash)
    return _render_llm(cfg, data, dash)


def _render_pillow(cfg: Config, data: DashboardData, dash: Dashboard) -> bytes:
    """Draw the dashboard locally with the Pillow layout backend (raw PNG bytes)."""
    log.info("rendering image via pillow layout %r (font %r)", dash.layout, dash.font)
    return layout.render(
        data,
        units=cfg.weather.units,
        width=dash.width,
        height=dash.height,
        layout=dash.layout,
        font=dash.font,
    )


def _render_llm(cfg: Config, data: DashboardData, dash: Dashboard) -> bytes:
    """Generate the dashboard via the OpenRouter image model backend (raw PNG bytes)."""
    assert cfg.openrouter is not None  # guaranteed by Config validation when a backend == "llm"
    client = OpenRouterClient(cfg.openrouter.model, cfg.openrouter.api_key.resolve())
    prompt, aspect = build_prompt(cfg, data, client, dash)
    log.info("generating image via %s (aspect %s)", cfg.openrouter.model, aspect)
    return client.generate(prompt, aspect_ratio=aspect, resolution=dash.resolution)


@dataclass(frozen=True)
class RunResult:
    """Outcome of one :func:`run_once`: which dashboards were written and which failed.

    ``written`` and ``failed`` are both empty when the render was skipped because every source was
    unavailable (a legitimate no-op, distinct from ``failed`` being non-empty).
    """

    written: list[Path]
    failed: list[str]  # names of dashboards whose render/write raised


def run_once(cfg: Config) -> RunResult:
    """Gather once, then render and write every configured dashboard; report the outcome.

    Data is fetched a single time and shared across all dashboards. If every source failed (no
    weather and no boards), the render is skipped entirely: writing a blank dashboard would clobber
    the last good images and waste paid generations, so the previous outputs are left in place and
    an empty :class:`RunResult` is returned. Dashboards are isolated from one another — a render or
    write failure for one is logged (with its name) and the remaining dashboards still render; the
    failed names are returned so a one-shot caller can exit non-zero.
    """
    log.info("dashboard render starting for %d dashboard(s)", len(cfg.dashboards))
    data = gather(cfg)
    if data.weather is None and len(data.boards) == 0:
        log.warning("all sources unavailable; keeping the last dashboard image(s)")
        return RunResult(written=[], failed=[])
    written: list[Path] = []
    failed: list[str] = []
    for name, dash in cfg.dashboards.items():
        try:
            png = render(cfg, data, dash)
            dash.path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(dash.path, png)
            log.info("wrote dashboard %r to %s", name, dash.path)
            written.append(dash.path)
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


def run(cfg: Config) -> None:
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
                run_once(cfg)
            except Exception:  # any source/render failure — keep the loop alive
                log.exception("dashboard render failed; retrying next interval")
            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("stopping dashboard loop")
