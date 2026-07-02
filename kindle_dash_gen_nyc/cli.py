"""Command-line interface for the Kindle dashboard generator."""

from __future__ import annotations

import csv
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .config import Config, load_config
from .format import format_eta, format_reading, format_temp, format_wind
from .models import DashboardData, Direction
from .render.openrouter import OpenRouterClient
from .render.prompt import render_prompt
from .sources.mta import MtaClient
from .sources.weather import NwsClient, WeatherError

app = typer.Typer(
    help="Generate a Kindle e-ink dashboard with NYC weather and subway info.",
    no_args_is_help=True,
)
mta_app = typer.Typer(help="Real-time subway arrivals and station lookup.", no_args_is_help=True)
app.add_typer(mta_app, name="mta")
dashboard_app = typer.Typer(help="Render the dashboard image via OpenRouter.", no_args_is_help=True)
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


def _gather(cfg: Config) -> DashboardData:
    """Fetch weather and subway data for one dashboard render.

    A weather-source failure degrades to ``None`` (the dashboard omits the weather panel)
    rather than aborting the render; the subway fetch is not yet isolated (see M5 pipeline).
    """
    weather_client = NwsClient(
        cfg.weather.user_agent, cfg.weather.rollover_hour, cfg.weather.hourly_hours
    )
    try:
        weather = weather_client.fetch(cfg.location.latitude, cfg.location.longitude)
    except WeatherError as exc:
        typer.echo(f"warning: weather unavailable ({exc}); omitting weather panel", err=True)
        weather = None
    boards = MtaClient(cfg.stations).fetch()
    return DashboardData(weather=weather, boards=boards, generated_at=datetime.now())


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


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
    data = files("kindle_dash_gen_nyc").joinpath("data/stations.csv")
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
    data = _gather(cfg)
    client = OpenRouterClient(cfg.openrouter.model)
    aspect = client.resolve_aspect_ratio(
        cfg.output.width, cfg.output.height, cfg.output.aspect_ratio
    )
    prompt = render_prompt(
        data,
        units=cfg.weather.units,
        width=cfg.output.width,
        height=cfg.output.height,
        aspect=aspect,
        template=cfg.openrouter.prompt_template,
    )
    typer.echo(prompt)


@dashboard_app.command("render")
def dashboard_render(
    ctx: typer.Context,
    output_file: Annotated[
        Path | None, typer.Argument(help="Where to write the PNG (defaults to output.path).")
    ] = None,
) -> None:
    """Fetch live data, render the prompt, and generate the dashboard PNG via OpenRouter."""
    cfg = _config(ctx)
    data = _gather(cfg)
    client = OpenRouterClient(cfg.openrouter.model, cfg.openrouter.api_key.resolve())
    aspect = client.resolve_aspect_ratio(
        cfg.output.width, cfg.output.height, cfg.output.aspect_ratio
    )
    prompt = render_prompt(
        data,
        units=cfg.weather.units,
        width=cfg.output.width,
        height=cfg.output.height,
        aspect=aspect,
        template=cfg.openrouter.prompt_template,
    )
    png = client.generate(prompt, aspect_ratio=aspect, resolution=cfg.output.resolution)

    path = output_file or cfg.output.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def run() -> None:
    """Console-script entry point."""
    app()
