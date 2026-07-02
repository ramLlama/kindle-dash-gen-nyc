"""End-to-end dashboard pipeline: gather → prompt → generate → post-process → write.

Runs as a single one-shot or on an interval loop. Each data source is isolated: a source
that fails degrades the dashboard (drops its panel) rather than aborting the whole render.
In the loop, a wholly failed iteration is logged and retried at the next interval so a
transient outage never kills the runner.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

from .config import Config
from .models import DashboardData, StationBoard
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


def build_prompt(cfg: Config, data: DashboardData, client: OpenRouterClient) -> tuple[str, str]:
    """Resolve the model's aspect ratio and render the OpenRouter prompt for ``data``.

    Returns ``(prompt, aspect_ratio)`` so the caller can pass the same aspect on to generate().
    """
    aspect = client.resolve_aspect_ratio(
        cfg.dashboard.width, cfg.dashboard.height, cfg.dashboard.aspect_ratio
    )
    prompt = render_prompt(
        data,
        units=cfg.weather.units,
        width=cfg.dashboard.width,
        height=cfg.dashboard.height,
        aspect=aspect,
        template=cfg.openrouter.prompt_template,
    )
    return prompt, aspect


def render(cfg: Config, data: DashboardData) -> bytes:
    """Render ``data`` into a Kindle-ready PNG: prompt → generate → post-process."""
    client = OpenRouterClient(cfg.openrouter.model, cfg.openrouter.api_key.resolve())
    prompt, aspect = build_prompt(cfg, data, client)
    log.info("generating image via %s (aspect %s)", cfg.openrouter.model, aspect)
    raw = client.generate(prompt, aspect_ratio=aspect, resolution=cfg.dashboard.resolution)
    log.info(
        "generated %d bytes; post-processing to %dx%d, %d gray levels (%s)",
        len(raw),
        cfg.dashboard.width,
        cfg.dashboard.height,
        cfg.dashboard.gray_levels,
        cfg.dashboard.post_process_method,
    )
    return post_process(
        raw,
        width=cfg.dashboard.width,
        height=cfg.dashboard.height,
        gray_levels=cfg.dashboard.gray_levels,
        method=cfg.dashboard.post_process_method,
    )


def run_once(cfg: Config) -> Path | None:
    """Gather, render, and write one dashboard image; return the path written, or ``None``.

    If every source failed (no weather and no boards), the render is skipped: writing a blank
    dashboard would clobber the last good image and waste a paid generation, so the previous
    output is left in place and ``None`` is returned.
    """
    log.info("dashboard render starting")
    data = gather(cfg)
    if data.weather is None and len(data.boards) == 0:
        log.warning("all sources unavailable; keeping the last dashboard image")
        return None
    png = render(cfg, data)
    path = cfg.dashboard.path
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, png)
    log.info("wrote dashboard to %s", path)
    return path


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
