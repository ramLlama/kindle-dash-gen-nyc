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
    """A display board: one or more platforms merged into per-direction arrival lists.

    Several platforms under one station are merged (e.g. the N/Q/R/W and the L platforms of
    "Union Sq"). Boards carry every upcoming arrival, sorted; how many to show is a render-time
    decision made by the layout (see docs/plugins.md), not a data-collection cap.
    """

    model_config = ConfigDict(extra="forbid")

    platforms: list[Platform]
    # Label a layout shows instead of the station's name (the config key). The key stays the
    # canonical name that plugins match on (e.g. home_mta_map), so renaming the display never
    # breaks that match. Unset means show the name as-is.
    display_name: str | None = None


class OpenRouter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    api_key: Secret
    prompt_template: str = "dense"  # bundled template name, or a filesystem path to a .j2 file


# How the generated image is fitted to the Kindle's exact pixel dimensions:
#   resize -- stretch to fill, ignoring aspect (minor distortion)
#   crop   -- scale to cover, center-crop the excess (no distortion, trims a sliver)
#   pad    -- scale to fit, add white e-ink bars (nothing cropped or distorted)
PostProcessMethod = Literal["resize", "crop", "pad"]

# Which rendering backend draws the dashboard:
#   pillow -- deterministic local layout (free, offline, exact); see render/layout.py
#   llm    -- an OpenRouter image model renders from a prompt; needs the [openrouter] section
RenderBackend = Literal["pillow", "llm"]


class Dashboard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path
    backend: RenderBackend = "pillow"
    layout: str = "glanceable"  # pillow backend: registered layout plugin (see docs/plugins.md)
    # pillow backend: system font family (resolved via fontconfig). None = unspecified, letting a
    # layout choose its own default (glanceable falls back to toolkit.DEFAULT_FONT; home_mta_map
    # uses Futura + Helvetica Neue). A set value overrides the layout's default for every glyph.
    font: str | None = None
    width: int = 1072  # Kindle Voyage, portrait (native orientation)
    height: int = 1448
    gray_levels: int = 16
    post_process_method: PostProcessMethod = "resize"
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
    openrouter: OpenRouter | None = None  # required only when a dashboard's backend == "llm"
    dashboards: dict[str, Dashboard]  # name -> output; one shared data fetch renders each
    plugins_path: Path | None = None  # absolute dir of private render plugins (see docs/plugins.md)
    schedule: Schedule = Schedule()

    @model_validator(mode="after")
    def _validate_dashboards(self) -> Config:
        if len(self.dashboards) == 0:
            raise ValueError("at least one [dashboards.<name>] section is required")
        if self.openrouter is None and any(d.backend == "llm" for d in self.dashboards.values()):
            raise ValueError("a dashboard with backend = 'llm' requires an [openrouter] section")
        # Absolute so plugin discovery is unambiguous regardless of the process's working directory.
        if self.plugins_path is not None and not self.plugins_path.is_absolute():
            raise ValueError(f"plugins_path must be an absolute path, got {self.plugins_path}")
        return self


def load_config(path: Path) -> Config:
    """Load and validate the TOML config at ``path``."""
    with path.open("rb") as f:
        data = tomllib.load(f)
    return Config.model_validate(data)
