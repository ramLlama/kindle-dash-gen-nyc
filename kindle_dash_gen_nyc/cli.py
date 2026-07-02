"""Command-line interface for the Kindle dashboard generator."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .config import load_config
from .format import format_reading, format_temp, format_wind
from .sources.weather import NwsClient

app = typer.Typer(
    help="Generate a Kindle e-ink dashboard with NYC weather and subway info.",
    no_args_is_help=True,
)

# Shared --config option for commands that read the config file.
ConfigOption = Annotated[
    Path,
    typer.Option("--config", "-c", help="Path to the TOML config file."),
]


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command()
def weather(config: ConfigOption = Path("config.toml")) -> None:
    """Fetch and print the current NWS forecast (debug)."""
    cfg = load_config(config)
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


def run() -> None:
    """Console-script entry point."""
    app()
