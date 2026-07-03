"""Command-line interface for the Kindle dashboard generator."""

from __future__ import annotations

import csv
import logging
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Annotated

import typer

from . import __version__, pipeline
from .config import Config, load_config
from .format import format_eta, format_reading, format_temp, format_wind
from .models import Direction
from .render.openrouter import OpenRouterClient
from .render.postprocess import post_process
from .sources.mta import MtaClient
from .sources.weather import NwsClient

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
    return load_config(ctx.obj)


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
    """Generate the Kindle dashboard on the configured interval (or once with ``--one-shot``).

    Each run gathers weather + subway data, renders the image via OpenRouter, post-processes
    it for the Kindle, and writes it to the dashboard.path in your config.
    """
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
    )
    cfg = _config(ctx)
    if one_shot:
        pipeline.run_once(cfg)
    else:
        pipeline.run(cfg)


@app.command()
def weather(ctx: typer.Context) -> None:
    """Fetch and print the current NWS forecast (debug)."""
    cfg = _config(ctx)
    client = NwsClient(cfg.weather.user_agent, cfg.weather.rollover_hour, cfg.weather.hourly_hours)
    r = client.fetch(cfg.location.latitude, cfg.location.longitude)
    units = cfg.weather.units

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
    boards = MtaClient(cfg.stations).fetch()
    now = datetime.now()
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
    data = files("kindle_dash_gen_nyc").joinpath("assets/mta/stations.csv")
    with data.open() as f:
        rows = [(r["stop_id"], ",".join(r["routes"].split()), r["name"]) for r in csv.DictReader(f)]
    id_width = max(len(stop_id) for stop_id, _, _ in rows)
    routes_width = max(len(routes) for _, routes, _ in rows)
    for stop_id, routes, name in rows:
        typer.echo(f"{stop_id:<{id_width}}  {routes:<{routes_width}}  {name}")


@dashboard_app.command("preview-prompt")
def dashboard_preview_prompt(ctx: typer.Context) -> None:
    """Fetch live data and print the OpenRouter prompt without generating an image (debug)."""
    cfg = _config(ctx)
    if cfg.openrouter is None:
        raise typer.BadParameter("preview-prompt applies only to the 'llm' backend ([openrouter])")
    data = pipeline.gather(cfg)
    client = OpenRouterClient(cfg.openrouter.model)
    prompt, _ = pipeline.build_prompt(cfg, data, client)
    typer.echo(prompt)


@dashboard_app.command("render")
def dashboard_render(
    ctx: typer.Context,
    output_file: Annotated[
        Path | None, typer.Argument(help="Where to write the PNG (defaults to dashboard.path).")
    ] = None,
) -> None:
    """Fetch live data and render the dashboard PNG via the configured backend.

    Writes the raw rendered image (before Kindle post-processing); run ``dashboard post-process``
    to massage it for the device.
    """
    cfg = _config(ctx)
    data = pipeline.gather(cfg)
    png = pipeline.render_raw(cfg, data)

    path = output_file or cfg.dashboard.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


@dashboard_app.command("post-process")
def dashboard_post_process(
    ctx: typer.Context,
    input_file: Annotated[Path, typer.Argument(help="Existing PNG to massage for the Kindle.")],
    output_file: Annotated[Path, typer.Argument(help="Where to write the processed PNG.")],
) -> None:
    """Fit, grayscale, and quantize an existing PNG into a Kindle-ready image."""
    cfg = _config(ctx)
    png = post_process(
        input_file.read_bytes(),
        width=cfg.dashboard.width,
        height=cfg.dashboard.height,
        gray_levels=cfg.dashboard.gray_levels,
        method=cfg.dashboard.post_process_method,
    )
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_bytes(png)


def run() -> None:
    """Console-script entry point."""
    app()
