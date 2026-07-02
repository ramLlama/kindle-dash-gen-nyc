"""Tests for config loading and secret resolution."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from kindle_dash_gen_nyc.config import Secret, load_config

EXAMPLE = """
[location]
latitude = 40.7484
longitude = -73.9857

[weather]
user_agent = "test-agent (test@example.com)"

[stations."Union Sq"]
max_arrivals = 3

[[stations."Union Sq".platforms]]
lines = ["N", "Q", "R", "W"]
stop_id = "R20"
direction = "both"

[[stations."Union Sq".platforms]]
lines = ["L"]
stop_id = "L03"

[openrouter]
model = "google/gemini-3.1-flash-lite-image"
api_key = { value = "sk-or-test" }

[output]
path = "./out/dashboard.png"

[schedule]
interval_minutes = 5
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_load_config_parses_all_sections(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, EXAMPLE))

    assert cfg.location.latitude == 40.7484
    assert cfg.weather.units == "us"  # default
    # Two platforms grouped under one station, merged into one board.
    assert list(cfg.stations.keys()) == ["Union Sq"]
    station = cfg.stations["Union Sq"]
    assert station.max_arrivals == 3
    assert len(station.platforms) == 2
    assert station.platforms[0].lines == ["N", "Q", "R", "W"]
    assert station.platforms[1].stop_id == "L03"
    assert station.platforms[1].direction == "both"  # default
    assert cfg.openrouter.model == "google/gemini-3.1-flash-lite-image"
    assert cfg.output.width == 1072  # default (portrait)
    assert cfg.output.gray_levels == 16  # default
    assert cfg.schedule.interval_minutes == 5


def test_load_config_defaults_schedule(tmp_path: Path) -> None:
    text = EXAMPLE.replace("\n[schedule]\ninterval_minutes = 5\n", "")
    cfg = load_config(_write(tmp_path, text))
    assert cfg.schedule.interval_minutes == 5


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    text = EXAMPLE.replace("[weather]\n", "[weather]\nbogus = 1\n")
    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, text))


def test_secret_value_resolves_literal() -> None:
    assert Secret(value="hunter2").resolve() == "hunter2"


def test_secret_from_cmd_resolves_stdout() -> None:
    assert Secret(value_from_cmd="printf 'from-cmd'").resolve() == "from-cmd"


def test_secret_from_cmd_strips_whitespace() -> None:
    assert Secret(value_from_cmd="echo padded").resolve() == "padded"


def test_secret_from_cmd_nonzero_exit_raises() -> None:
    with pytest.raises(RuntimeError):
        Secret(value_from_cmd="exit 3").resolve()


def test_secret_requires_exactly_one() -> None:
    with pytest.raises(ValidationError):
        Secret()
    with pytest.raises(ValidationError):
        Secret(value="a", value_from_cmd="echo b")
