"""Tests for config loading."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from kindle_dash_gen.config import load_config

EXAMPLE = """
[sources.nws]
latitude = 40.7484
longitude = -73.9857
user_agent = "test-agent (test@example.com)"

[sources.mta.stations."Union Sq"]

[[sources.mta.stations."Union Sq".platforms]]
lines = ["N", "Q", "R", "W"]
stop_id = "R20"
direction = "both"

[[sources.mta.stations."Union Sq".platforms]]
lines = ["L"]
stop_id = "L03"

[dashboards.main]
output_path = "./out/dashboard.png"

[schedule]
interval_minutes = 5
"""

# A minimal pillow-only config with no [sources.*] at all (zero sources is valid).
MINIMAL = """
[dashboards.main]
output_path = "./out/dashboard.png"
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_load_config_parses_all_sections(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, EXAMPLE))

    # Sources are kept as raw tables here; each is validated by its own plugin (see test_sources).
    assert cfg.sources["nws"]["latitude"] == 40.7484
    assert list(cfg.sources["mta"]["stations"].keys()) == ["Union Sq"]
    dash = cfg.dashboards["main"]
    assert dash.width == 1072  # default (portrait)
    assert dash.gray_levels == 16  # default
    assert dash.post_process_method == "resize"  # default
    assert dash.layout == "glanceable"  # default layout
    assert dash.layout_config == {}  # default; the layout validates its own table (see test_layout)
    assert cfg.schedule.interval_minutes == 5


def test_example_config_loads() -> None:
    # The shipped example is the documented first-run (cp config.example.toml config.toml), so it
    # must always validate — this guards against a config-schema change outrunning the example.
    load_config(Path("config.example.toml"))


def test_zero_sources_is_valid(tmp_path: Path) -> None:
    # No [sources.*] at all is legal: every render then legitimately skips (keeps the last image).
    cfg = load_config(_write(tmp_path, MINIMAL))
    assert cfg.sources == {}
    assert cfg.dashboards["main"].layout == "glanceable"


def test_load_config_defaults_schedule(tmp_path: Path) -> None:
    text = EXAMPLE.replace("\n[schedule]\ninterval_minutes = 5\n", "")
    cfg = load_config(_write(tmp_path, text))
    assert cfg.schedule.interval_minutes == 5


def test_multiple_dashboards_parse(tmp_path: Path) -> None:
    # Several named [dashboards.<name>] blocks load into the dict, each with its own settings.
    text = EXAMPLE + (
        '\n[dashboards.landscape]\noutput_path = "./out/landscape.png"\n'
        "width = 1448\nheight = 1072\n"
    )
    cfg = load_config(_write(tmp_path, text))
    assert set(cfg.dashboards) == {"main", "landscape"}
    assert cfg.dashboards["landscape"].width == 1448
    assert cfg.dashboards["main"].width == 1072  # default, untouched


def test_at_least_one_dashboard_required(tmp_path: Path) -> None:
    text = EXAMPLE.replace('[dashboards.main]\noutput_path = "./out/dashboard.png"\n', "")
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


def test_unknown_top_level_key_is_rejected(tmp_path: Path) -> None:
    # Config stays strict (extra="forbid") at the top level; per-source strictness is enforced by
    # each plugin's own model (see test_sources).
    text = "bogus = 1\n" + EXAMPLE
    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, text))
