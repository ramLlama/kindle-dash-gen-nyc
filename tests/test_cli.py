"""Light CLI tests for the `dashboard` command group."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

from kindle_dash_gen import pipeline
from kindle_dash_gen.cli import app
from kindle_dash_gen.sources.builtins.mta.model import Direction, StationBoard, TrainArrival
from kindle_dash_gen.sources.builtins.nws.model import (
    DailyHighLow,
    LocationWeather,
    NwsData,
    Temperature,
)

runner = CliRunner()

CONFIG = """
[sources.nws]
user_agent = "test-agent (test@example.com)"

[sources.nws.locations."home"]
latitude = 40.7484
longitude = -73.9857

[sources.mta.stations."Union Sq"]

  [[sources.mta.stations."Union Sq".platforms]]
  lines = ["N", "Q", "R", "W"]
  stop_id = "R20"

[dashboards.main]
output_path = "./out/dashboard.png"
width = 100
height = 140

[dashboards.main.layout_config]
title = "Test"
timezone = "America/New_York"
weather_location = "home"
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
        '[dashboards.second.layout_config]\ntitle = "Test"\ntimezone = "America/New_York"\n'
        'weather_location = "home"\n'
    )
    return f"{main}\n{second}"


class _FakeNwsClient:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def fetch(self, locations):
        return None


class _FakeMtaClient:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def fetch(self, now=None):
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

    async def _once(cfg):
        called["once"] += 1
        return pipeline.RunResult(written=[], failed=[])

    async def _loop(cfg) -> None:
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
    async def _once(cfg):
        return pipeline.RunResult(written=[], failed=["main"])

    monkeypatch.setattr("kindle_dash_gen.pipeline.run_once", _once)
    config_path = _write_config(tmp_path)

    result = runner.invoke(app, ["--config", str(config_path), "run", "--one-shot"])

    assert result.exit_code == 1


def test_run_one_shot_exits_zero_when_all_sources_down(tmp_path, monkeypatch) -> None:
    # An empty result with no failures is a legitimate skip, not an error → exit 0.
    async def _once(cfg):
        return pipeline.RunResult(written=[], failed=[])

    monkeypatch.setattr("kindle_dash_gen.pipeline.run_once", _once)
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
user_agent = "test-agent (test@example.com)"

[sources.nws.locations."home"]
latitude = 40.7484
longitude = -73.9857

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
    return NwsData(locations={"home": _location_report()})


def _location_report() -> LocationWeather:
    return LocationWeather(
        temperature=Temperature(30.0, 32.0),
        conditions="Sunny",
        humidity=50,
        dewpoint=18.0,
        wind_speed_kmh=10.0,
        wind_direction="NW",
        precip_probability=10,
        raining=False,
        observed_conditions="Clear",
        today=DailyHighLow(
            day=date(2026, 7, 1), high=Temperature(31.0, None), low=Temperature(20.0, None)
        ),
        tomorrow=DailyHighLow(
            day=date(2026, 7, 2), high=Temperature(29.0, None), low=Temperature(19.0, None)
        ),
        forecast="Sunny all day",
        forecast_name="Today",
        hourly=[],
        as_of=datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC),
        location_name="New York, NY",
    )


class _FakeNwsWithReport:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def fetch(self, locations):
        return _weather_report()


class _FakeMtaWithBoard:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def fetch(self, now=None):
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


def test_source_name_fetches_and_prints(tmp_path, monkeypatch) -> None:
    # `source nws` (no verb) is the default action: fetch the source and pretty-print what it makes.
    monkeypatch.setattr("kindle_dash_gen.sources.builtins.nws.source.NwsClient", _FakeNwsWithReport)
    cfg = str(_write(tmp_path, NWS_ONLY))
    result = runner.invoke(app, ["--config", cfg, "source", "nws"])
    assert result.exit_code == 0, result.output
    assert "NwsData" in result.output  # the produced dataclass, pretty-printed via rich


def test_source_name_fetches_mta_data(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kindle_dash_gen.sources.builtins.mta.source.MtaClient", _FakeMtaWithBoard)
    cfg = str(_write(tmp_path, MTA_ONLY))
    result = runner.invoke(app, ["--config", cfg, "source", "mta"])
    assert result.exit_code == 0, result.output
    assert "Union Sq" in result.output  # board fetched through the mta source


def test_source_name_drives_the_real_client_with_an_aware_now(tmp_path, monkeypatch) -> None:
    """`source <name>` must hand the source an aware-UTC `now`, like the pipeline does.

    Every other CLI source test substitutes the client, so the real one is never driven here. That
    let a naive `datetime.now()` survive the aware-UTC migration and crash `source mta` with
    "can't compare offset-naive and offset-aware datetimes" the moment it filtered past arrivals.
    """
    # nyct-gtfs renders arrivals naive in the host's zone; mirror that so the real conversion runs.
    naive_local = (datetime.now(UTC) + timedelta(minutes=5)).astimezone().replace(tzinfo=None)

    class _Stop:
        stop_id = "L03N"
        arrival = naive_local

    class _Trip:
        route_id, direction, headsign_text = "L", "N", "8 Av"
        underway, stop_time_updates = True, [_Stop()]

    class _Feed:
        def filter_trips(self, **kw):
            return [_Trip()]

    async def _loader(url):
        return _Feed()

    monkeypatch.setattr("kindle_dash_gen.sources.builtins.mta.source._default_feed_loader", _loader)
    result = runner.invoke(app, ["--config", str(_write(tmp_path, MTA_ONLY)), "source", "mta"])
    assert result.exit_code == 0, result.output
    assert "Union Sq" in result.output


def test_source_mta_list_stations(tmp_path) -> None:
    # The mta source owns a `list-stations` verb (source-defined cli()); it reads the bundled CSV
    # and needs no live config or network.
    cfg = str(_write(tmp_path, MTA_ONLY))
    result = runner.invoke(app, ["--config", cfg, "source", "mta", "list-stations"])
    assert result.exit_code == 0, result.output
    assert "Union Sq" in result.output  # a known station in the bundled table


def test_source_sf_bay_511_agencies(tmp_path) -> None:
    # A source-defined verb needing neither config nor network.
    cfg = str(_write(tmp_path, MTA_ONLY))
    result = runner.invoke(app, ["--config", cfg, "source", "sf-bay-511", "agencies"])
    assert result.exit_code == 0, result.output
    assert "BART" in result.output  # the readable label, not "Bart" or the "BA" code


SF511_ONLY = """
[sources.sf-bay-511]
api_key = { value = "cfg-key" }

[sources.sf-bay-511.boards."Embarcadero"]
  [[sources.sf-bay-511.boards."Embarcadero".stops]]
  agency = "BA"
  stopcode = "901162"

[dashboards.main]
output_path = "./out/dashboard.png"
[dashboards.main.layout_config]
title = "SF"
timezone = "America/Los_Angeles"
"""


def test_source_sf_bay_511_list_stops(tmp_path, monkeypatch) -> None:
    # `list-stops` is live-only (511 has 40+ operators whose stop lists change). The key comes from
    # the configured source table, so there is no second place to put a credential.
    import niquests

    stops = {
        "Contents": {
            "dataObjects": {
                "ScheduledStopPoint": [
                    {
                        "id": "901162",
                        "Name": "Embarcadero",
                        "Extensions": {"PlatformCode": "2", "ParentStation": "901169"},
                    }
                ]
            }
        }
    }

    sent = {}

    class _Resp:
        content = json.dumps(stops).encode("utf-8")

        def raise_for_status(self):
            return None

    def _get(url, params=None, **kw):
        sent.update(params or {})
        return _Resp()

    monkeypatch.setattr(niquests, "get", _get)
    cfg = str(_write(tmp_path, SF511_ONLY))
    result = runner.invoke(
        app, ["--config", cfg, "source", "sf-bay-511", "list-stops", "--agency", "BA"]
    )
    assert result.exit_code == 0, result.output
    assert "901162" in result.output
    assert "Embarcadero" in result.output
    assert sent["api_key"] == "cfg-key"  # taken from the config's Secret, not a flag


def test_source_sf_bay_511_list_stops_needs_the_source_configured(tmp_path) -> None:
    # Without a [sources.sf-bay-511] table there is no key to use; say so instead of tracebacking.
    cfg = str(_write(tmp_path, MTA_ONLY))
    result = runner.invoke(
        app, ["--config", cfg, "source", "sf-bay-511", "list-stops", "--agency", "BA"]
    )
    assert result.exit_code != 0
    assert "Traceback" not in result.output


def test_source_sf_bay_511_list_stops_reports_a_bad_response_cleanly(tmp_path, monkeypatch) -> None:
    # A parse failure must render as a typer error, not a bare traceback.
    import niquests

    class _Resp:
        content = b'{"unexpected": true}'

        def raise_for_status(self):
            return None

    monkeypatch.setattr(niquests, "get", lambda *a, **kw: _Resp())
    cfg = str(_write(tmp_path, SF511_ONLY))
    result = runner.invoke(
        app, ["--config", cfg, "source", "sf-bay-511", "list-stops", "--agency", "BA"]
    )
    assert result.exit_code != 0
    assert "Traceback" not in result.output


def test_source_name_errors_when_registered_but_not_configured(tmp_path) -> None:
    # mta is registered but absent from this config: the fetch default names it unconfigured.
    cfg = str(_write(tmp_path, NWS_ONLY))
    result = runner.invoke(app, ["--config", cfg, "source", "mta"])
    assert result.exit_code != 0
    assert "no [sources.mta] section" in result.output


def test_source_unknown_name_errors(tmp_path) -> None:
    # A name that isn't a registered source resolves to no subcommand (clean click error).
    cfg = str(_write(tmp_path, NWS_ONLY))
    result = runner.invoke(app, ["--config", cfg, "source", "bogus"])
    assert result.exit_code != 0


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["--config", "foo.toml", "source", "nws"], "foo.toml"),
        (["--config=foo.toml"], "foo.toml"),
        (["-c", "foo.toml", "source"], "foo.toml"),
        (["-c=foo.toml"], "foo.toml"),
        (["-cfoo.toml"], "foo.toml"),  # click's attached short form
        (["source", "nws"], "config.toml"),  # default
        ([], "config.toml"),
    ],
)
def test_config_path_from_argv(argv, expected) -> None:
    from kindle_dash_gen.cli import _config_path_from_argv

    assert _config_path_from_argv(argv) == Path(expected)


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["source", "nws"], "source"),
        (["-c", "foo.toml", "source", "nws"], "source"),  # skips the option's value
        (["--config=foo.toml", "source"], "source"),
        (["version"], "version"),
        (["--config", "foo.toml"], None),  # no positional command
        ([], None),
    ],
)
def test_invoked_command(argv, expected) -> None:
    from kindle_dash_gen.cli import _invoked_command

    assert _invoked_command(argv) == expected


_LOCAL_SOURCE = (
    "from pydantic import BaseModel, ConfigDict\n"
    "from kindle_dash_gen.sources.registry import Source, register_source\n"
    "class Cfg(BaseModel):\n"
    "    model_config = ConfigDict(extra='forbid')\n"
    "    who: str = 'world'\n"
    "class Greeter(Source):\n"
    "    Config = Cfg\n"
    "    def __init__(self, config):\n"
    "        self._who = config.who\n"
    "    async def fetch(self, now):\n"
    "        return {'greeting_for': self._who}\n"
    "register_source('greeter', Greeter)\n"
)


def test_source_local_plugin_is_first_class(tmp_path) -> None:
    # A source discovered from the config's plugins_path is mounted and fetchable exactly like a
    # bundled one — the whole point of wiring sources after sniffing --config in run().
    from kindle_dash_gen import cli as cli_mod
    from kindle_dash_gen import plugins
    from kindle_dash_gen.sources import registry as reg

    pkg = tmp_path / "cliplugins"
    (pkg / "greeter").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "greeter" / "__init__.py").write_text(_LOCAL_SOURCE)
    config = (
        f'plugins_path = "{pkg.as_posix()}"\n\n'
        '[sources.greeter]\nwho = "nyc"\n\n'
        '[dashboards.main]\noutput_path = "./out/dash.png"\n'
    )
    cfg = str(_write(tmp_path, config))

    # Snapshot the registry + wiring so the temp source doesn't leak into other tests. (The pkg
    # also lingers in sys.modules under "cliplugins"; keep local-plugin dir names unique per test.)
    saved_sources = dict(reg._SOURCES)
    saved_wired = set(cli_mod._wired_sources)
    saved_groups = list(cli_mod.source_app.registered_groups)
    try:
        plugins.load_plugins(local_dir=pkg)  # register the local source (run() does this)
        cli_mod._wire_source_commands()  # mount it as `source greeter`
        fetched = runner.invoke(app, ["--config", cfg, "source", "greeter"])
        assert fetched.exit_code == 0, fetched.output
        assert "greeting_for" in fetched.output and "nyc" in fetched.output
        listed = runner.invoke(app, ["--config", cfg, "source", "list"])
        assert "greeter" in listed.output  # and it appears in `source list`
    finally:
        reg._SOURCES.clear()
        reg._SOURCES.update(saved_sources)
        cli_mod._wired_sources.clear()
        cli_mod._wired_sources.update(saved_wired)
        cli_mod.source_app.registered_groups[:] = saved_groups


def test_bad_layout_config_key_fails_fast(tmp_path) -> None:
    # _config eagerly validates each dashboard's layout_config against its layout, so a bad key
    # fails the command up front (before any fetch/render), not mid-run.
    # CONFIG already ends with the [dashboards.main.layout_config] table, so append the bad key
    # under it (a second table header would be a TOML duplicate, not the validation error we want).
    text = CONFIG + "bogus = 1\n"
    config_path = tmp_path / "config.toml"
    config_path.write_text(text)
    result = runner.invoke(app, ["--config", str(config_path), "run", "--one-shot"])
    assert result.exit_code != 0
