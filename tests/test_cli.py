"""Light CLI tests for the `dashboard` command group."""

from __future__ import annotations

from datetime import date, datetime
from io import BytesIO
from pathlib import Path

from PIL import Image
from typer.testing import CliRunner

from kindle_dash_gen import pipeline
from kindle_dash_gen.cli import app
from kindle_dash_gen.sources.builtins.mta.model import Direction, StationBoard, TrainArrival
from kindle_dash_gen.sources.builtins.nws.model import NwsData, Temperature

runner = CliRunner()

CONFIG = """
[sources.nws]
latitude = 40.7484
longitude = -73.9857
user_agent = "test-agent (test@example.com)"

[sources.mta.stations."Union Sq"]

  [[sources.mta.stations."Union Sq".platforms]]
  lines = ["N", "Q", "R", "W"]
  stop_id = "R20"

[dashboards.main]
output_path = "./out/dashboard.png"
width = 100
height = 140
"""


def _write_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG)
    return path


def _two_dashboard_text(first_path: Path, second_path: Path) -> str:
    """CONFIG with `main` pointed at first_path plus a second dashboard at second_path."""
    main = CONFIG.replace(
        'output_path = "./out/dashboard.png"', f'output_path = "{first_path.as_posix()}"'
    )
    second = (
        f'[dashboards.second]\noutput_path = "{second_path.as_posix()}"\n'
        "width = 100\nheight = 140\n"
    )
    return f"{main}\n{second}"


class _FakeNwsClient:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def fetch(self, lat, lon):
        return None


class _FakeMtaClient:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def fetch(self, now=None):
        return [StationBoard(name="Union Sq", arrivals_by_direction={})]


def _patch_clients(monkeypatch) -> None:
    # gather() constructs the source clients inside the bundled source modules; patch them there so
    # the pillow layout renders offline against fakes.
    monkeypatch.setattr("kindle_dash_gen.sources.builtins.nws.source.NwsClient", _FakeNwsClient)
    monkeypatch.setattr("kindle_dash_gen.sources.builtins.mta.source.MtaClient", _FakeMtaClient)


def _assert_png(path: Path, size: tuple[int, int]) -> None:
    img = Image.open(BytesIO(path.read_bytes()))
    assert img.size == size


def test_dashboard_render_writes_image_to_output_path(tmp_path, monkeypatch) -> None:
    _patch_clients(monkeypatch)
    config_path = _write_config(tmp_path)
    output_path = tmp_path / "out" / "dashboard.png"  # parent dir does not exist yet

    result = runner.invoke(
        app, ["--config", str(config_path), "dashboard", "render", str(output_path)]
    )

    assert result.exit_code == 0, result.output
    _assert_png(output_path, (100, 140))  # the raw layout image at the panel size


def test_dashboard_render_renders_all_dashboards_from_one_gather(tmp_path, monkeypatch) -> None:
    # With two dashboards and no --name, `render` fetches once and writes both to their own paths.
    gathers = {"count": 0}
    real_gather = pipeline.gather

    def _counting_gather(cfg):
        gathers["count"] += 1
        return real_gather(cfg)

    _patch_clients(monkeypatch)
    monkeypatch.setattr("kindle_dash_gen.pipeline.gather", _counting_gather)

    first_path = tmp_path / "out" / "first.png"
    second_path = tmp_path / "out" / "second.png"
    config_path = tmp_path / "config.toml"
    config_path.write_text(_two_dashboard_text(first_path, second_path))

    result = runner.invoke(app, ["--config", str(config_path), "dashboard", "render"])

    assert result.exit_code == 0, result.output
    assert gathers["count"] == 1  # single shared fetch
    _assert_png(first_path, (100, 140))
    _assert_png(second_path, (100, 140))


def test_dashboard_render_name_selects_a_subset(tmp_path, monkeypatch) -> None:
    # Repeated --name renders only the named dashboards; the unnamed one is left untouched.
    _patch_clients(monkeypatch)

    first_path = tmp_path / "out" / "first.png"
    second_path = tmp_path / "out" / "second.png"
    config_path = tmp_path / "config.toml"
    config_path.write_text(_two_dashboard_text(first_path, second_path))

    result = runner.invoke(
        app, ["--config", str(config_path), "dashboard", "render", "--name", "second"]
    )

    assert result.exit_code == 0, result.output
    _assert_png(second_path, (100, 140))
    assert not first_path.exists()  # not named, so not rendered


def test_dashboard_post_process_writes_kindle_ready_image(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    input_path = tmp_path / "raw.png"
    output_path = tmp_path / "out" / "dash.png"  # parent dir does not exist yet
    Image.new("RGB", (200, 150), (120, 120, 120)).save(input_path)  # non-target aspect

    args = ["--config", str(config_path), "dashboard", "post-process"]
    result = runner.invoke(app, [*args, str(input_path), str(output_path)])

    assert result.exit_code == 0, result.output
    out = Image.open(BytesIO(output_path.read_bytes()))
    assert out.size == (100, 140)  # config width x height
    assert out.mode == "L"


def _patch_pipeline_entrypoints(monkeypatch) -> dict[str, int]:
    """Replace the pipeline one-shot/loop entrypoints with counters; return the call tally."""
    called = {"once": 0, "loop": 0}

    def _once(cfg):
        called["once"] += 1
        return pipeline.RunResult(written=[], failed=[])

    def _loop(cfg) -> None:
        called["loop"] += 1

    monkeypatch.setattr("kindle_dash_gen.pipeline.run_once", _once)
    monkeypatch.setattr("kindle_dash_gen.pipeline.run", _loop)
    return called


def test_run_one_shot_invokes_single_iteration(tmp_path, monkeypatch) -> None:
    called = _patch_pipeline_entrypoints(monkeypatch)
    config_path = _write_config(tmp_path)

    result = runner.invoke(app, ["--config", str(config_path), "run", "--one-shot"])

    assert result.exit_code == 0, result.output
    assert called == {"once": 1, "loop": 0}  # single iteration, no loop


def test_run_without_flag_starts_loop(tmp_path, monkeypatch) -> None:
    called = _patch_pipeline_entrypoints(monkeypatch)
    config_path = _write_config(tmp_path)

    result = runner.invoke(app, ["--config", str(config_path), "run"])

    assert result.exit_code == 0, result.output
    assert called == {"once": 0, "loop": 1}  # entered the loop, not a one-shot


def test_run_one_shot_exits_nonzero_when_a_dashboard_fails(tmp_path, monkeypatch) -> None:
    # A one-shot must fail loudly (non-zero) so cron/systemd sees a failed render.
    monkeypatch.setattr(
        "kindle_dash_gen.pipeline.run_once",
        lambda cfg: pipeline.RunResult(written=[], failed=["main"]),
    )
    config_path = _write_config(tmp_path)

    result = runner.invoke(app, ["--config", str(config_path), "run", "--one-shot"])

    assert result.exit_code == 1


def test_run_one_shot_exits_zero_when_all_sources_down(tmp_path, monkeypatch) -> None:
    # An empty result with no failures is a legitimate skip, not an error → exit 0.
    monkeypatch.setattr(
        "kindle_dash_gen.pipeline.run_once",
        lambda cfg: pipeline.RunResult(written=[], failed=[]),
    )
    config_path = _write_config(tmp_path)

    result = runner.invoke(app, ["--config", str(config_path), "run", "--one-shot"])

    assert result.exit_code == 0, result.output


def _two_dashboard_config(tmp_path: Path) -> Path:
    text = CONFIG + '\n[dashboards.second]\noutput_path = "./out/second.png"\n'
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_render_unknown_name_errors(tmp_path) -> None:
    config_path = _write_config(tmp_path)  # only "main" configured
    result = runner.invoke(app, ["--config", str(config_path), "dashboard", "render", "-n", "nope"])
    assert result.exit_code != 0


def test_render_output_file_with_multiple_dashboards_errors(tmp_path) -> None:
    config_path = _two_dashboard_config(tmp_path)
    out = tmp_path / "out.png"
    result = runner.invoke(app, ["--config", str(config_path), "dashboard", "render", str(out)])
    assert result.exit_code != 0  # output_file needs a single --name when several dashboards exist


def test_post_process_requires_single_dashboard(tmp_path) -> None:
    config_path = _two_dashboard_config(tmp_path)
    input_path = tmp_path / "raw.png"
    Image.new("RGB", (200, 150), (120, 120, 120)).save(input_path)
    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "dashboard",
            "post-process",
            str(input_path),
            str(tmp_path / "out.png"),
        ],
    )
    assert result.exit_code != 0  # ambiguous without --name across multiple dashboards


# --- the generic `source` debug command (source list / source run) ---

NWS_ONLY = """
[sources.nws]
latitude = 40.7484
longitude = -73.9857
user_agent = "test-agent (test@example.com)"

[dashboards.main]
output_path = "./out/dash.png"
"""

MTA_ONLY = """
[sources.mta.stations."Union Sq"]

  [[sources.mta.stations."Union Sq".platforms]]
  lines = ["N"]
  stop_id = "R20"

[dashboards.main]
output_path = "./out/dash.png"
"""


def _weather_report() -> NwsData:
    return NwsData(
        temperature=Temperature(30.0, 32.0),
        conditions="Sunny",
        humidity=50,
        dewpoint=18.0,
        wind_speed_kmh=10.0,
        wind_direction="NW",
        precip_probability=10,
        raining=False,
        observed_conditions="Clear",
        high=Temperature(31.0, None),
        low=Temperature(20.0, None),
        high_low_date=date(2026, 7, 1),
        forecast="Sunny all day",
        forecast_name="Today",
        hourly=[],
        as_of=datetime(2026, 7, 1, 12, 0, 0),
        location_name="New York, NY",
    )


class _FakeNwsWithReport:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def fetch(self, lat, lon):
        return _weather_report()


class _FakeMtaWithBoard:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def fetch(self, now=None):
        return [
            StationBoard(
                name="Union Sq",
                arrivals_by_direction={
                    Direction.NORTH: [
                        TrainArrival(
                            route="N",
                            direction=Direction.NORTH,
                            destination="Astoria",
                            arrival=now or datetime.now(),
                        )
                    ]
                },
            )
        ]


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_source_list_marks_configured_sources(tmp_path) -> None:
    # `source list` shows every registered source (nws + mta are bundled) and marks the configured
    # ones. NWS_ONLY configures only nws.
    result = runner.invoke(app, ["--config", str(_write(tmp_path, NWS_ONLY)), "source", "list"])
    assert result.exit_code == 0, result.output
    assert "nws" in result.output
    assert "mta" in result.output
    assert "(configured)" in result.output  # nws is configured, so at least one is marked


def test_source_run_prints_produced_data(tmp_path, monkeypatch) -> None:
    # `source run nws` fetches the source in isolation and pretty-prints the produced data object.
    monkeypatch.setattr("kindle_dash_gen.sources.builtins.nws.source.NwsClient", _FakeNwsWithReport)
    cfg = str(_write(tmp_path, NWS_ONLY))
    result = runner.invoke(app, ["--config", cfg, "source", "run", "nws"])
    assert result.exit_code == 0, result.output
    assert "NwsData" in result.output  # the produced dataclass, pretty-printed via rich


def test_source_run_prints_mta_data(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kindle_dash_gen.sources.builtins.mta.source.MtaClient", _FakeMtaWithBoard)
    cfg = str(_write(tmp_path, MTA_ONLY))
    result = runner.invoke(app, ["--config", cfg, "source", "run", "mta"])
    assert result.exit_code == 0, result.output
    assert "Union Sq" in result.output  # board fetched through the mta source


def test_source_run_errors_when_registered_but_not_configured(tmp_path) -> None:
    # mta is a registered source but absent from this config: the error names it as unconfigured.
    cfg = str(_write(tmp_path, NWS_ONLY))
    result = runner.invoke(app, ["--config", cfg, "source", "run", "mta"])
    assert result.exit_code != 0
    assert "no [sources.mta] section" in result.output


def test_source_run_errors_on_unknown_source(tmp_path) -> None:
    # A name that isn't a registered plugin at all reports "unknown source", not "not configured".
    cfg = str(_write(tmp_path, NWS_ONLY))
    result = runner.invoke(app, ["--config", cfg, "source", "run", "bogus"])
    assert result.exit_code != 0
    assert "unknown source" in result.output


def test_bad_layout_config_key_fails_fast(tmp_path) -> None:
    # _config eagerly validates each dashboard's layout_config against its layout, so a bad key
    # fails the command up front (before any fetch/render), not mid-run.
    text = CONFIG + "\n[dashboards.main.layout_config]\nbogus = 1\n"
    config_path = tmp_path / "config.toml"
    config_path.write_text(text)
    result = runner.invoke(app, ["--config", str(config_path), "run", "--one-shot"])
    assert result.exit_code != 0
