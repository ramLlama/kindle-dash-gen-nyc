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


class Station(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    lines: list[str]
    stop_id: str
    direction: Literal["north", "south", "both"] = "both"
    max_arrivals: int = 3


class OpenRouter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    api_key: Secret
    prompt_template: Path | None = None


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path
    width: int = 1448
    height: int = 1072
    gray_levels: int = 16
    rotate: int = 0


class Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interval_minutes: int = 5


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location: Location
    weather: Weather
    stations: list[Station]
    openrouter: OpenRouter
    output: Output
    schedule: Schedule = Schedule()


def load_config(path: Path) -> Config:
    """Load and validate the TOML config at ``path``."""
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return Config.model_validate(data)
