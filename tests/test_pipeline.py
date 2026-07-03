"""Pipeline orchestration tests: source isolation, one-shot write, and the run loop."""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from kindle_dash_gen_nyc import pipeline
from kindle_dash_gen_nyc.config import Config
from kindle_dash_gen_nyc.models import StationBoard
from kindle_dash_gen_nyc.sources.mta import MtaError
from kindle_dash_gen_nyc.sources.weather import WeatherError

CONFIG: dict = {
    "location": {"latitude": 40.7484, "longitude": -73.9857},
    "weather": {"user_agent": "test-agent (test@example.com)"},
    "stations": {"Union Sq": {"platforms": [{"lines": ["N", "Q"], "stop_id": "R20"}]}},
    "openrouter": {"model": "test/model", "api_key": {"value": "sk-or-test"}},
    "dashboard": {"path": "out/dashboard.png", "width": 100, "height": 140},
    "schedule": {"interval_minutes": 5},
}


def _config(tmp_path) -> Config:
    cfg = Config.model_validate(CONFIG)
    cfg.dashboard.path = tmp_path / "out" / "dashboard.png"  # parent dir does not exist yet
    return cfg


def _png_bytes(size=(120, 90)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, (120, 120, 120)).save(buffer, format="PNG")
    return buffer.getvalue()


class _FakeMtaClient:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def fetch(self):
        return []


class _FakeMtaClientWithBoard:
    """Returns one (empty-arrivals) board so the render path has data to work with."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def fetch(self):
        return [StationBoard(name="Union Sq", arrivals_by_direction={})]


class _FakeOpenRouterClient:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def resolve_aspect_ratio(self, width, height, override=None) -> str:
        return "4:3"

    def generate(self, prompt, *, aspect_ratio, resolution=None) -> bytes:
        return _png_bytes()


def _patch_render_clients(monkeypatch) -> None:
    """Stub the network clients so gather/render run offline against fakes (with subway data)."""
    monkeypatch.setattr(pipeline, "MtaClient", _FakeMtaClientWithBoard)
    monkeypatch.setattr(pipeline, "OpenRouterClient", _FakeOpenRouterClient)


def _fake_nws(returns=None, raises=None):
    class _FakeNwsClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def fetch(self, lat, lon):
            if raises is not None:
                raise raises
            return returns

    return _FakeNwsClient


def test_gather_isolates_weather_failure(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "NwsClient", _fake_nws(raises=WeatherError("down")))
    monkeypatch.setattr(pipeline, "MtaClient", _FakeMtaClient)

    data = pipeline.gather(Config.model_validate(CONFIG))

    assert data.weather is None  # weather dropped, render still proceeds
    assert data.boards == []


def test_gather_isolates_subway_failure(monkeypatch) -> None:
    sentinel = SimpleNamespace(conditions="Sunny")  # stands in for a WeatherReport
    monkeypatch.setattr(pipeline, "NwsClient", _fake_nws(returns=sentinel))

    class _FailingMtaClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def fetch(self):
            raise MtaError("feed down")

    monkeypatch.setattr(pipeline, "MtaClient", _FailingMtaClient)

    data = pipeline.gather(Config.model_validate(CONFIG))

    assert data.weather is sentinel  # weather survives a subway outage
    assert data.boards == []


def test_run_once_writes_kindle_ready_image(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "NwsClient", _fake_nws(returns=None))
    _patch_render_clients(monkeypatch)
    cfg = _config(tmp_path)

    path = pipeline.run_once(cfg)

    assert path == cfg.dashboard.path
    out = Image.open(BytesIO(path.read_bytes()))
    assert out.size == (100, 140)  # fitted to the configured dimensions
    assert out.mode == "L"  # grayscale for e-ink


def test_run_loops_until_interrupted(monkeypatch) -> None:
    calls = {"count": 0}

    def _run_once(cfg):
        calls["count"] += 1

    # Let three iterations complete, then interrupt from within sleep to exit the loop.
    def _sleep(seconds):
        if calls["count"] >= 3:
            raise KeyboardInterrupt
        return None

    monkeypatch.setattr(pipeline, "run_once", _run_once)
    monkeypatch.setattr(pipeline.time, "sleep", _sleep)

    pipeline.run(Config.model_validate(CONFIG))  # returns cleanly on KeyboardInterrupt

    assert calls["count"] == 3


def test_run_continues_after_iteration_failure(monkeypatch) -> None:
    calls = {"count": 0}

    def _run_once(cfg):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("transient render failure")

    def _sleep(seconds):
        if calls["count"] >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(pipeline, "run_once", _run_once)
    monkeypatch.setattr(pipeline.time, "sleep", _sleep)

    pipeline.run(Config.model_validate(CONFIG))  # first iteration raises but loop survives

    assert calls["count"] == 2  # retried after the failure


def test_run_once_propagates_unhandled_error(tmp_path, monkeypatch) -> None:
    # A render failure (not an isolated source error) surfaces from run_once so the loop can
    # log-and-retry; only per-source WeatherError/MtaError are swallowed inside gather().
    monkeypatch.setattr(pipeline, "NwsClient", _fake_nws(returns=None))
    monkeypatch.setattr(pipeline, "MtaClient", _FakeMtaClientWithBoard)

    class _BoomClient(_FakeOpenRouterClient):
        def generate(self, prompt, *, aspect_ratio, resolution=None):
            raise RuntimeError("openrouter exploded")

    monkeypatch.setattr(pipeline, "OpenRouterClient", _BoomClient)

    cfg = _config(tmp_path)
    cfg.dashboard.backend = "llm"  # exercise the llm render path so the boom client is reached
    with pytest.raises(RuntimeError):
        pipeline.run_once(cfg)


def test_run_once_skips_render_when_all_sources_down(tmp_path, monkeypatch) -> None:
    # Both sources empty: run_once must not spend a paid generation or overwrite the last image.
    monkeypatch.setattr(pipeline, "NwsClient", _fake_nws(returns=None))
    monkeypatch.setattr(pipeline, "MtaClient", _FakeMtaClient)  # returns []

    def _must_not_render(cfg, data):
        raise AssertionError("render() must not run when all sources are down")

    monkeypatch.setattr(pipeline, "render", _must_not_render)
    cfg = _config(tmp_path)
    cfg.dashboard.path.parent.mkdir(parents=True)
    cfg.dashboard.path.write_bytes(b"PREVIOUS-IMAGE")  # a prior good dashboard

    result = pipeline.run_once(cfg)

    assert result is None  # signals "nothing written"
    assert cfg.dashboard.path.read_bytes() == b"PREVIOUS-IMAGE"  # last image preserved
