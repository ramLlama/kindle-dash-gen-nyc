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

[dashboards.main]
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
    assert len(station.platforms) == 2
    assert station.platforms[0].lines == ["N", "Q", "R", "W"]
    assert station.platforms[1].stop_id == "L03"
    assert station.platforms[1].direction == "both"  # default
    assert cfg.openrouter.model == "google/gemini-3.1-flash-lite-image"
    dash = cfg.dashboards["main"]
    assert dash.width == 1072  # default (portrait)
    assert dash.gray_levels == 16  # default
    assert dash.post_process_method == "resize"  # default
    assert dash.backend == "pillow"  # default backend
    assert dash.layout == "glanceable"  # default pillow layout
    assert cfg.schedule.interval_minutes == 5


def test_load_config_defaults_schedule(tmp_path: Path) -> None:
    text = EXAMPLE.replace("\n[schedule]\ninterval_minutes = 5\n", "")
    cfg = load_config(_write(tmp_path, text))
    assert cfg.schedule.interval_minutes == 5


def _without_openrouter(text: str) -> str:
    return text.replace(
        '[openrouter]\nmodel = "google/gemini-3.1-flash-lite-image"\n'
        'api_key = { value = "sk-or-test" }\n',
        "",
    )


def test_pillow_backend_needs_no_openrouter(tmp_path: Path) -> None:
    # The default (pillow) backend needs no [openrouter] section.
    cfg = load_config(_write(tmp_path, _without_openrouter(EXAMPLE)))
    assert cfg.openrouter is None
    assert cfg.dashboards["main"].backend == "pillow"


def test_llm_backend_requires_openrouter(tmp_path: Path) -> None:
    text = _without_openrouter(EXAMPLE).replace(
        "[dashboards.main]\n", '[dashboards.main]\nbackend = "llm"\n'
    )
    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, text))


def test_multiple_dashboards_parse(tmp_path: Path) -> None:
    # Several named [dashboards.<name>] blocks load into the dict, each with its own settings.
    text = EXAMPLE + (
        '\n[dashboards.landscape]\npath = "./out/landscape.png"\nwidth = 1448\nheight = 1072\n'
    )
    cfg = load_config(_write(tmp_path, text))
    assert set(cfg.dashboards) == {"main", "landscape"}
    assert cfg.dashboards["landscape"].width == 1448
    assert cfg.dashboards["main"].width == 1072  # default, untouched


def test_at_least_one_dashboard_required(tmp_path: Path) -> None:
    text = EXAMPLE.replace('[dashboards.main]\npath = "./out/dashboard.png"\n', "")
    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, text))


def test_plugins_path_defaults_none_and_parses_absolute(tmp_path: Path) -> None:
    assert load_config(_write(tmp_path, EXAMPLE)).plugins_path is None
    text = 'plugins_path = "/opt/kindle/plugins"\n' + EXAMPLE
    assert load_config(_write(tmp_path, text)).plugins_path == Path("/opt/kindle/plugins")


def test_relative_plugins_path_rejected(tmp_path: Path) -> None:
    text = 'plugins_path = "./local_plugins"\n' + EXAMPLE
    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, text))


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
