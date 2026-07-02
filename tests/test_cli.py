"""Light CLI tests for the `dashboard` command group."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from kindle_dash_gen_nyc.cli import app

runner = CliRunner()

CONFIG = """
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

[openrouter]
model = "google/gemini-3.1-flash-lite-image"
api_key = { value = "sk-or-test" }

[output]
path = "./out/dashboard.png"
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
    monkeypatch.setattr("kindle_dash_gen_nyc.cli.NwsClient", _FakeNwsClient)
    monkeypatch.setattr("kindle_dash_gen_nyc.cli.MtaClient", _FakeMtaClient)
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


def test_dashboard_preview_prompt_prints_without_generating(tmp_path, monkeypatch) -> None:
    _patch_clients(monkeypatch)

    def _no_generate(self, *args, **kwargs):
        raise AssertionError("preview-prompt must not call generate()")

    monkeypatch.setattr(_FakeOpenRouterClient, "generate", _no_generate)
    config_path = _write_config(tmp_path)

    result = runner.invoke(app, ["--config", str(config_path), "dashboard", "preview-prompt"])

    assert result.exit_code == 0, result.output
    assert "4:3" in result.output
