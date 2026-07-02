"""Configuration model loaded from a TOML file."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class Secret(BaseModel):
    """A secret resolved from either a literal value or the stdout of a command.

    Exactly one of ``value`` or ``value_from_cmd`` must be set.
    """

    model_config = ConfigDict(extra="forbid")

    value: str | None = None
    value_from_cmd: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Secret:
        has_value = self.value is not None
        has_cmd = self.value_from_cmd is not None
        if has_value == has_cmd:
            raise ValueError("set exactly one of 'value' or 'value_from_cmd'")
        return self

    def resolve(self) -> str:
        """Return the literal value, or run the command and return stripped stdout."""
        if self.value is not None:
            return self.value
        assert self.value_from_cmd is not None  # guaranteed by validator
        result = subprocess.run(
            self.value_from_cmd,
            shell=True,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"value_from_cmd failed ({result.returncode}): "
                f"{self.value_from_cmd!r}\n{result.stderr.strip()}"
            )
        return result.stdout.strip()


class Location(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latitude: float
    longitude: float


class Weather(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_agent: str
    units: Literal["us", "si", "both"] = "us"  # display units; data is always SI internally
    rollover_hour: int = 20  # after this local hour, high/low show the next day
    hourly_hours: int = 4  # number of upcoming hourly forecasts to include


class Platform(BaseModel):
    """One physical platform: a GTFS stop id plus the lines that serve it."""

    model_config = ConfigDict(extra="forbid")

    lines: list[str]
    stop_id: str
    direction: Literal["north", "south", "both"] = "both"


class Station(BaseModel):
    """A display board: one or more platforms merged, capped per direction.

    Several platforms under one station are merged (e.g. the N/Q/R/W and the L platforms of
    "Union Sq"); ``max_arrivals`` caps each direction across all of them.
    """

    model_config = ConfigDict(extra="forbid")

    platforms: list[Platform]
    max_arrivals: int = 3  # per direction, across all platforms


class OpenRouter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    api_key: Secret
    prompt_template: str = "dense"  # bundled template name, or a filesystem path to a .j2 file


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path
    width: int = 1072  # Kindle Voyage, portrait (native orientation)
    height: int = 1448
    gray_levels: int = 16
    rotate: int = 0
    aspect_ratio: str | None = None  # e.g. "4:3"; unset picks the model's nearest supported
    resolution: str | None = None  # e.g. "1K"; unset uses the model's default


class Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interval_minutes: int = 5


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location: Location
    weather: Weather
    stations: dict[str, Station]  # display name -> station board
    openrouter: OpenRouter
    output: Output
    schedule: Schedule = Schedule()


def load_config(path: Path) -> Config:
    """Load and validate the TOML config at ``path``."""
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return Config.model_validate(data)
