"""Tests for config loading."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from kindle_dash_gen import config as config_module
from kindle_dash_gen.config import Secret, load_config

EXAMPLE = """
[sources.nws]
user_agent = "test-agent (test@example.com)"

[sources.nws.locations."home"]
latitude = 40.7484
longitude = -73.9857

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
    assert cfg.sources["nws"]["locations"]["home"]["latitude"] == 40.7484
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


# ── Secret ─────────────────────────────────────────────────────────────────────────────────────
# A plugin config types a credential field as Secret so the value can stay out of the config file
# (see docs/sources.md). Exactly one of the three sources must be set.


def test_secret_value_resolves_literal() -> None:
    assert Secret(value="hunter2").value == "hunter2"


def test_secret_from_cmd_resolves_stdout() -> None:
    assert Secret(value_from_cmd="printf 'from-cmd'").value == "from-cmd"


def test_secret_from_cmd_strips_whitespace() -> None:
    # `echo` appends a newline; a credential must not carry it into an HTTP header.
    assert Secret(value_from_cmd="echo padded").value == "padded"


def test_secret_from_cmd_nonzero_exit_raises() -> None:
    with pytest.raises(RuntimeError):
        _ = Secret(value_from_cmd="exit 3").value


def test_secret_from_env_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDG_TEST_SECRET", "from-env")
    assert Secret(value_from_env="KDG_TEST_SECRET").value == "from-env"


def test_secret_from_env_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDG_TEST_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        _ = Secret(value_from_env="KDG_TEST_SECRET").value


@pytest.mark.parametrize(
    "kwargs",
    [
        {},  # none set
        {"value": "a", "value_from_cmd": "echo b"},
        {"value": "a", "value_from_env": "X"},
        {"value_from_cmd": "echo b", "value_from_env": "X"},
        {"value": "a", "value_from_cmd": "echo b", "value_from_env": "X"},
    ],
)
def test_secret_requires_exactly_one(kwargs: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        Secret(**kwargs)


def test_secret_rejects_unknown_key() -> None:
    with pytest.raises(ValidationError):
        Secret(value="a", bogus="b")


def test_secret_is_exposed_on_both_plugin_toolkits() -> None:
    # Secret is part of the public plugin surface, so a plugin never imports app config internals.
    from kindle_dash_gen.render.toolkit import Secret as render_secret
    from kindle_dash_gen.sources.toolkit import Secret as source_secret

    assert render_secret is Secret
    assert source_secret is Secret


def test_secret_from_cmd_does_not_run_at_validation(tmp_path: Path) -> None:
    # Laziness is the point: constructing (and so loading a whole config) must never shell out.
    marker = tmp_path / "ran"
    Secret(value_from_cmd=f"touch {marker}")
    assert not marker.exists()


def test_secret_from_cmd_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    # An interactive password-manager prompt would otherwise hang the unattended run forever.
    # The real ceiling is shortened here so the suite doesn't actually wait it out.
    monkeypatch.setattr(config_module, "_CMD_TIMEOUT_SECONDS", 0.2)
    with pytest.raises(RuntimeError):
        _ = Secret(value_from_cmd="sleep 30").value


def test_secret_from_env_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    # `export K=$(cat file)` commonly leaves a trailing newline, which would break an auth header.
    monkeypatch.setenv("KDG_TEST_SECRET", "  padded-env\n")
    assert Secret(value_from_env="KDG_TEST_SECRET").value == "padded-env"


def test_secret_from_cmd_is_cached_after_first_resolve(tmp_path: Path) -> None:
    # Resolution is cached so a source can call resolve() per fetch without shelling out every
    # interval (the subprocess would otherwise block the event loop on the concurrent hot path).
    runs = tmp_path / "runs"
    secret = Secret(value_from_cmd=f"echo x >> {runs}; echo the-key")
    assert secret.value == "the-key"
    assert secret.value == "the-key"
    assert runs.read_text().count("x") == 1


def test_secret_failed_resolve_is_not_cached(tmp_path: Path) -> None:
    # A transient failure must not poison the secret for the life of the process.
    flag = tmp_path / "ok"
    secret = Secret(value_from_cmd=f"test -f {flag} && echo the-key")
    with pytest.raises(RuntimeError):
        _ = secret.value
    flag.write_text("")
    assert secret.value == "the-key"


def test_secret_caching_means_rotation_needs_a_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    # The documented tradeoff of caching: a secret rotated under a running process is not seen.
    monkeypatch.setenv("KDG_TEST_SECRET", "first")
    secret = Secret(value_from_env="KDG_TEST_SECRET")
    assert secret.value == "first"
    monkeypatch.setenv("KDG_TEST_SECRET", "rotated")
    assert secret.value == "first"


def test_secret_literal_is_masked_in_repr_and_errors() -> None:
    # The type is named Secret, so it must not leak the credential when a config object is logged
    # or when a sibling field fails validation (pydantic echoes input values in error output).
    secret = Secret(value="sk-live-do-not-leak")
    assert "sk-live-do-not-leak" not in repr(secret)
    assert secret.value == "sk-live-do-not-leak"
