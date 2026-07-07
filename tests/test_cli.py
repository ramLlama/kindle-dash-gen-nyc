"""Light CLI tests for the `dashboard` command group."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image
from typer.testing import CliRunner

from kindle_dash_gen_nyc import pipeline
from kindle_dash_gen_nyc.cli import app

runner = CliRunner()

CONFIG = """
[location]
latitude = 40.7484
longitude = -73.9857

[weather]
user_agent = "test-agent (test@example.com)"

[stations."Union Sq"]

  [[stations."Union Sq".platforms]]
  lines = ["N", "Q", "R", "W"]
  stop_id = "R20"

[openrouter]
model = "google/gemini-3.1-flash-lite-image"
api_key = { value = "sk-or-test" }

[dashboards.main]
backend = "llm"
path = "./out/dashboard.png"
width = 100
height = 140
"""


def _write_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG)
    return path


class _FakeNwsClient:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def fetch(self, lat, lon):
        return None


class _FakeMtaClient:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def fetch(self):
        return []


class _FakeOpenRouterClient:
    def __init__(self, model, api_key=None, session=None) -> None:
        self.model = model
        self.api_key = api_key

    def resolve_aspect_ratio(self, width, height, override=None) -> str:
        return "4:3"

    def generate(self, prompt, *, aspect_ratio, resolution=None) -> bytes:
        return b"FAKE-PNG-BYTES"


def _patch_clients(monkeypatch) -> None:
    # gather() and the llm render run in pipeline; preview-prompt builds its client in cli.
    monkeypatch.setattr("kindle_dash_gen_nyc.pipeline.NwsClient", _FakeNwsClient)
    monkeypatch.setattr("kindle_dash_gen_nyc.pipeline.MtaClient", _FakeMtaClient)
    monkeypatch.setattr("kindle_dash_gen_nyc.pipeline.OpenRouterClient", _FakeOpenRouterClient)
    monkeypatch.setattr("kindle_dash_gen_nyc.cli.OpenRouterClient", _FakeOpenRouterClient)


def test_dashboard_render_writes_generated_bytes_to_output_path(tmp_path, monkeypatch) -> None:
    _patch_clients(monkeypatch)
    config_path = _write_config(tmp_path)
    output_path = tmp_path / "out" / "dashboard.png"  # parent dir does not exist yet

    result = runner.invoke(
        app, ["--config", str(config_path), "dashboard", "render", str(output_path)]
    )

    assert result.exit_code == 0, result.output
    assert output_path.read_bytes() == b"FAKE-PNG-BYTES"


def test_dashboard_render_renders_all_dashboards_from_one_gather(tmp_path, monkeypatch) -> None:
    # With two dashboards and no --name, `render` fetches once and writes both to their own paths.
    gathers = {"count": 0}
    real_gather = pipeline.gather

    def _counting_gather(cfg):
        gathers["count"] += 1
        return real_gather(cfg)

    _patch_clients(monkeypatch)
    monkeypatch.setattr("kindle_dash_gen_nyc.pipeline.gather", _counting_gather)

    first_path = tmp_path / "out" / "first.png"
    second_path = tmp_path / "out" / "second.png"
    text = CONFIG.replace(
        'path = "./out/dashboard.png"', f'path = "{first_path.as_posix()}"'
    ) + f'\n[dashboards.second]\nbackend = "llm"\npath = "{second_path.as_posix()}"\n'
    config_path = tmp_path / "config.toml"
    config_path.write_text(text)

    result = runner.invoke(app, ["--config", str(config_path), "dashboard", "render"])

    assert result.exit_code == 0, result.output
    assert gathers["count"] == 1  # single shared fetch
    assert first_path.read_bytes() == b"FAKE-PNG-BYTES"
    assert second_path.read_bytes() == b"FAKE-PNG-BYTES"


def test_dashboard_render_name_selects_a_subset(tmp_path, monkeypatch) -> None:
    # Repeated --name renders only the named dashboards; the unnamed one is left untouched.
    _patch_clients(monkeypatch)

    first_path = tmp_path / "out" / "first.png"
    second_path = tmp_path / "out" / "second.png"
    text = CONFIG.replace(
        'path = "./out/dashboard.png"', f'path = "{first_path.as_posix()}"'
    ) + f'\n[dashboards.second]\nbackend = "llm"\npath = "{second_path.as_posix()}"\n'
    config_path = tmp_path / "config.toml"
    config_path.write_text(text)

    result = runner.invoke(
        app, ["--config", str(config_path), "dashboard", "render", "--name", "second"]
    )

    assert result.exit_code == 0, result.output
    assert second_path.read_bytes() == b"FAKE-PNG-BYTES"
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

    monkeypatch.setattr("kindle_dash_gen_nyc.pipeline.run_once", _once)
    monkeypatch.setattr("kindle_dash_gen_nyc.pipeline.run", _loop)
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
        "kindle_dash_gen_nyc.pipeline.run_once",
        lambda cfg: pipeline.RunResult(written=[], failed=["main"]),
    )
    config_path = _write_config(tmp_path)

    result = runner.invoke(app, ["--config", str(config_path), "run", "--one-shot"])

    assert result.exit_code == 1


def test_run_one_shot_exits_zero_when_all_sources_down(tmp_path, monkeypatch) -> None:
    # An empty result with no failures is a legitimate skip, not an error → exit 0.
    monkeypatch.setattr(
        "kindle_dash_gen_nyc.pipeline.run_once",
        lambda cfg: pipeline.RunResult(written=[], failed=[]),
    )
    config_path = _write_config(tmp_path)

    result = runner.invoke(app, ["--config", str(config_path), "run", "--one-shot"])

    assert result.exit_code == 0, result.output


def _two_dashboard_config(tmp_path: Path) -> Path:
    text = CONFIG + '\n[dashboards.second]\nbackend = "llm"\npath = "./out/second.png"\n'
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
        ["--config", str(config_path), "dashboard", "post-process",
         str(input_path), str(tmp_path / "out.png")],
    )
    assert result.exit_code != 0  # ambiguous without --name across multiple dashboards


def test_dashboard_preview_prompt_prints_without_generating(tmp_path, monkeypatch) -> None:
    _patch_clients(monkeypatch)

    def _no_generate(self, *args, **kwargs):
        raise AssertionError("preview-prompt must not call generate()")

    monkeypatch.setattr(_FakeOpenRouterClient, "generate", _no_generate)
    config_path = _write_config(tmp_path)

    result = runner.invoke(app, ["--config", str(config_path), "dashboard", "preview-prompt"])

    assert result.exit_code == 0, result.output
    assert "4:3" in result.output
