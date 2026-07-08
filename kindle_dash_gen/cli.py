"""Command-line interface for the Kindle dashboard generator."""

from __future__ import annotations

import csv
import logging
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Literal

import typer
from PIL import Image

from . import __version__, pipeline, plugins
from .config import Config, Dashboard, load_config
from .format import format_eta, format_reading, format_temp, format_wind
from .models import Direction
from .render.layout import validate_layout
from .render.postprocess import post_process
from .sources.registry import build_sources

app = typer.Typer(
    help="Generate a Kindle e-ink dashboard with NYC weather and subway info.",
    no_args_is_help=True,
)
mta_app = typer.Typer(help="Real-time subway arrivals and station lookup.", no_args_is_help=True)
app.add_typer(mta_app, name="mta")
dashboard_app = typer.Typer(help="Render the dashboard image.", no_args_is_help=True)
app.add_typer(dashboard_app, name="dashboard")

ConfigOption = Annotated[
    Path,
    typer.Option("--config", "-c", help="Path to the TOML config file."),
]

_DIRECTION_LABELS = {Direction.NORTH: "Northbound ↑", Direction.SOUTH: "Southbound ↓"}


@app.callback()
def main(ctx: typer.Context, config: ConfigOption = Path("config.toml")) -> None:
    """Store the config path for subcommands to load on demand."""
    ctx.obj = config


def _config(ctx: typer.Context) -> Config:
    """Load the config, discover plugins, and eagerly validate every source (fail fast)."""
    cfg = load_config(ctx.obj)
    plugins.load_plugins(cfg.plugins_path)
    build_sources(cfg.sources)  # validate each [sources.<name>] against its plugin now, not mid-run
    for dash in cfg.dashboards.values():  # and each dashboard's layout_config against its layout
        validate_layout(dash.layout, dash.layout_config)
    return cfg


NameOption = Annotated[
    list[str] | None,
    typer.Option("--name", "-n", help="Dashboard name(s) to act on; repeatable. Default: all."),
]


def _selected_dashboards(cfg: Config, names: list[str] | None) -> dict[str, Dashboard]:
    """Every dashboard, or just the named subset (error on any unknown name)."""
    if names is None or len(names) == 0:
        return cfg.dashboards
    unknown = [n for n in names if n not in cfg.dashboards]
    if len(unknown) > 0:
        raise typer.BadParameter(f"unknown dashboard(s) {unknown}; have: {sorted(cfg.dashboards)}")
    return {n: cfg.dashboards[n] for n in names}


def _one_dashboard(cfg: Config, names: list[str] | None) -> tuple[str, Dashboard]:
    """Exactly one dashboard: the sole configured one, or a single --name; error otherwise."""
    selected = _selected_dashboards(cfg, names)
    if len(selected) != 1:
        raise typer.BadParameter(
            f"this command acts on one dashboard; pass a single --name (have {sorted(selected)})"
        )
    return next(iter(selected.items()))


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command(name="run")
def run_dashboard(
    ctx: typer.Context,
    one_shot: Annotated[
        bool,
        typer.Option("--one-shot", help="Run a single iteration and exit instead of looping."),
    ] = False,
) -> None:
    """Generate the Kindle dashboard(s) on the configured interval (or once with ``--one-shot``).

    Each run gathers weather + subway data once, then renders every configured dashboard,
    post-processes each for the Kindle, and writes it to that dashboard's path in your config.
    """
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
    )
    cfg = _config(ctx)
    if one_shot:
        # Surface render failures to the exit code so a cron/systemd one-shot doesn't report
        # success when a dashboard silently failed. "All sources down" is a legitimate skip (no
        # failures), so it still exits 0. The loop, by contrast, just retries next interval.
        result = pipeline.run_once(cfg)
        if len(result.failed) > 0:
            raise typer.Exit(code=1)
    else:
        pipeline.run(cfg)


UnitsOption = Annotated[
    Literal["us", "si", "both"],
    typer.Option("--units", help="Display units for weather temperatures."),
]


@app.command()
def weather(ctx: typer.Context, units: UnitsOption = "us") -> None:
    """Fetch and print the current NWS forecast (debug)."""
    cfg = _config(ctx)
    resolved = build_sources(cfg.sources)
    if "nws" not in resolved:
        raise typer.BadParameter("no [sources.nws] section configured")
    source_cls, source_cfg = resolved["nws"]
    r = source_cls(source_cfg).fetch(datetime.now())

    typer.echo(f"{r.location_name or 'Location'} — as of {r.as_of:%a %H:%M}")
    typer.echo(f"Now: {format_reading(r.temperature, units)}  {r.conditions}")
    if r.raining is not None:
        raining = "yes" if r.raining else "no"
        typer.echo(f"Observed: {r.observed_conditions or '—'} (raining: {raining})")

    details: list[str] = []
    if r.humidity is not None:
        details.append(f"humidity {r.humidity}%")
    if r.precip_probability is not None:
        details.append(f"precip {r.precip_probability}%")
    if r.wind_speed_kmh is not None:
        details.append(f"wind {format_wind(r.wind_speed_kmh, r.wind_direction, units)}")
    if r.dewpoint is not None:
        details.append(f"dew {format_temp(r.dewpoint, units)}")
    if len(details) > 0:
        typer.echo("  ".join(details))

    label = "Tomorrow" if r.high_low_date != r.as_of.date() else "Today"
    high, low = format_reading(r.high, units), format_reading(r.low, units)
    typer.echo(f"{label}: High {high}  Low {low}")
    typer.echo(f"{r.forecast_name}: {r.forecast}")
    if len(r.hourly) > 0:
        typer.echo("Next hours:")
        for h in r.hourly:
            pop = f"  {h.precip_probability}%" if h.precip_probability is not None else ""
            temp = format_reading(h.temperature, units)
            typer.echo(f"  {h.time:%H:%M}  {temp}  {h.conditions}{pop}")


@mta_app.command("get-current")
def mta_get_current(ctx: typer.Context) -> None:
    """Fetch and print upcoming subway arrivals."""
    cfg = _config(ctx)
    resolved = build_sources(cfg.sources)
    if "mta" not in resolved:
        raise typer.BadParameter("no [sources.mta] section configured")
    source_cls, source_cfg = resolved["mta"]
    now = datetime.now()
    boards = source_cls(source_cfg).fetch(now).boards
    for board in boards:
        typer.echo(f"\n{board.name}")
        if len(board.arrivals_by_direction) == 0:
            typer.echo("  (no upcoming trains)")
            continue
        for direction, arrivals in board.arrivals_by_direction.items():
            typer.echo(f"  {_DIRECTION_LABELS.get(direction, direction)}")
            for a in arrivals:
                typer.echo(f"    {a.route} → {a.destination}  {format_eta(a.arrival, now)}")


@mta_app.command("list-stations")
def mta_list_stations() -> None:
    """Dump every MTA station (stop id, routes, name) — grep it to fill in config."""
    data = files("kindle_dash_gen").joinpath("assets/mta/stations.csv")
    with data.open() as f:
        rows = [(r["stop_id"], ",".join(r["routes"].split()), r["name"]) for r in csv.DictReader(f)]
    id_width = max(len(stop_id) for stop_id, _, _ in rows)
    routes_width = max(len(routes) for _, routes, _ in rows)
    for stop_id, routes, name in rows:
        typer.echo(f"{stop_id:<{id_width}}  {routes:<{routes_width}}  {name}")


@dashboard_app.command("render")
def dashboard_render(
    ctx: typer.Context,
    names: NameOption = None,
    output_file: Annotated[
        Path | None,
        typer.Argument(help="Write a single dashboard's PNG here (needs one --name if several)."),
    ] = None,
) -> None:
    """Fetch live data once and render every dashboard's PNG via its pillow layout.

    Writes the raw rendered image (before Kindle post-processing) to each dashboard's output_path;
    run ``dashboard post-process`` to massage it for the device. Restrict to a subset with repeated
    ``--name``, and pass an output path to redirect a single dashboard elsewhere.
    """
    cfg = _config(ctx)
    selected = _selected_dashboards(cfg, names)
    if output_file is not None and len(selected) > 1:
        raise typer.BadParameter("output_file writes one dashboard; pass a single --name")
    data = pipeline.gather(cfg)  # one fetch, shared across all rendered dashboards
    for dash in selected.values():
        image = pipeline.render_raw(cfg, data, dash)
        path = output_file or dash.output_path
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path, format="PNG")


@dashboard_app.command("post-process")
def dashboard_post_process(
    ctx: typer.Context,
    input_file: Annotated[Path, typer.Argument(help="Existing PNG to massage for the Kindle.")],
    output_file: Annotated[Path, typer.Argument(help="Where to write the processed PNG.")],
    names: NameOption = None,
) -> None:
    """Fit, grayscale, and quantize an existing PNG into a Kindle-ready image (per dashboard)."""
    cfg = _config(ctx)
    _, dash = _one_dashboard(cfg, names)
    with Image.open(input_file) as image:
        png = post_process(
            image,
            width=dash.width,
            height=dash.height,
            gray_levels=dash.gray_levels,
            method=dash.post_process_method,
            rotate=dash.rotate,
        )
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_bytes(png)


def run() -> None:
    """Console-script entry point."""
    app()
