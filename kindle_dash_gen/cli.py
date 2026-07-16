"""Command-line interface for the Kindle dashboard generator."""

from __future__ import annotations

import csv
import logging
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Annotated

import typer
from PIL import Image
from rich.console import Console

from . import __version__, pipeline, plugins
from .config import Config, Dashboard, load_config
from .render.layout import validate_layout
from .render.postprocess import post_process
from .sources.registry import build_sources, registered_sources

app = typer.Typer(
    help="Generate a Kindle e-ink dashboard with NYC weather and subway info.",
    no_args_is_help=True,
)
mta_app = typer.Typer(help="Subway station lookup.", no_args_is_help=True)
app.add_typer(mta_app, name="mta")
source_app = typer.Typer(
    help="Run a data source in isolation and print what it produces (debug).",
    no_args_is_help=True,
)
app.add_typer(source_app, name="source")
dashboard_app = typer.Typer(help="Render the dashboard image.", no_args_is_help=True)
app.add_typer(dashboard_app, name="dashboard")

ConfigOption = Annotated[
    Path,
    typer.Option("--config", "-c", help="Path to the TOML config file."),
]


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


@source_app.command("list")
def source_list(ctx: typer.Context) -> None:
    """List the data sources available to run, marking those configured in the current config."""
    cfg = _config(ctx)
    configured = set(cfg.sources)
    console = Console()
    for name in registered_sources():
        console.print(f"{name}{'  (configured)' if name in configured else ''}")


@source_app.command("run")
def source_run(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Source name to run, e.g. 'nws'.")],
) -> None:
    """Fetch a single source and pretty-print the data object it produces (debug).

    Runs the source's ``fetch`` in isolation against the current config and prints the raw produced
    data (SI values, no display formatting) so you can inspect exactly what a source returns.
    """
    cfg = _config(ctx)
    resolved = build_sources(cfg.sources)
    if name not in resolved:
        # Distinguish a typo (no such plugin) from a real source that just isn't configured, so the
        # error points at the right fix instead of implying every name is fixable via config.
        if name not in registered_sources():
            raise typer.BadParameter(f"unknown source {name!r}; registered: {registered_sources()}")
        raise typer.BadParameter(
            f"source {name!r} is registered but has no [sources.{name}] section; "
            f"configured: {sorted(resolved)}"
        )
    source_cls, source_cfg = resolved[name]
    result = source_cls(source_cfg).fetch(datetime.now())
    console = Console()
    if result is None:
        console.print(f"source {name!r} returned no data")
        return
    console.print(result)


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
