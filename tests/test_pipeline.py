"""Pipeline orchestration tests: source isolation, one-shot write, and the run loop."""

from __future__ import annotations

import asyncio
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image
from pydantic import BaseModel, ConfigDict

from kindle_dash_gen import pipeline
from kindle_dash_gen.config import Config
from kindle_dash_gen.sources.builtins.mta import source as mta_mod
from kindle_dash_gen.sources.builtins.mta.model import MtaData, StationBoard
from kindle_dash_gen.sources.builtins.mta.source import MtaError
from kindle_dash_gen.sources.builtins.nws import source as nws_mod
from kindle_dash_gen.sources.builtins.nws.model import NwsData
from kindle_dash_gen.sources.builtins.nws.source import WeatherError
from kindle_dash_gen.sources.registry import SourceError

CONFIG: dict = {
    "sources": {
        "nws": {
            "user_agent": "test-agent (test@example.com)",
            "locations": {"home": {"latitude": 40.7484, "longitude": -73.9857}},
        },
        "mta": {"stations": {"Union Sq": {"platforms": [{"lines": ["N", "Q"], "stop_id": "R20"}]}}},
    },
    "dashboards": {
        "main": {
            "output_path": "out/dashboard.png",
            "width": 100,
            "height": 140,
            "layout_config": {
                "title": "Test",
                "timezone": "America/New_York",
                "weather_location": "home",
            },
        }
    },
    "schedule": {"interval_minutes": 5},
}


def _config(tmp_path) -> Config:
    cfg = Config.model_validate(CONFIG)
    # parent dir does not exist yet
    cfg.dashboards["main"].output_path = tmp_path / "out" / "dashboard.png"
    return cfg


class _FakeMtaClient:
    """MTA fetch that succeeds with no boards (real client is called with `now`)."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def fetch(self, now=None):
        return []


class _FakeMtaClientWithBoard:
    """Returns one (empty-arrivals) board so the render path has data to work with."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def fetch(self, now=None):
        return [StationBoard(name="Union Sq", arrivals_by_direction={})]


class _FailingMtaClient:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def fetch(self, now=None):
        raise MtaError("feed down")


def _fake_nws(returns=None, raises=None):
    """A fake NwsClient (patched into the source module); its fetch(locations) returns/raises."""

    class _FakeNwsClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def fetch(self, locations):
            if raises is not None:
                raise raises
            return returns

    return _FakeNwsClient


def _patch_render_sources(monkeypatch) -> None:
    """Stub the source clients so gather runs offline against fakes (with subway data)."""
    monkeypatch.setattr(mta_mod, "MtaClient", _FakeMtaClientWithBoard)


def test_gather_isolates_weather_failure(monkeypatch) -> None:
    monkeypatch.setattr(nws_mod, "NwsClient", _fake_nws(raises=WeatherError("down")))
    monkeypatch.setattr(mta_mod, "MtaClient", _FakeMtaClient)

    data = asyncio.run(pipeline.gather(Config.model_validate(CONFIG)))

    assert NwsData not in data.source_data  # weather dropped, render still proceeds
    assert data.source_data[MtaData].boards == []  # subway present (empty), render proceeds


def test_gather_isolates_subway_failure(monkeypatch) -> None:
    sentinel = SimpleNamespace(conditions="Sunny")  # stands in for an NwsData
    monkeypatch.setattr(nws_mod, "NwsClient", _fake_nws(returns=sentinel))
    monkeypatch.setattr(mta_mod, "MtaClient", _FailingMtaClient)

    data = asyncio.run(pipeline.gather(Config.model_validate(CONFIG)))

    assert data.source_data[type(sentinel)] is sentinel  # weather survives a subway outage
    assert MtaData not in data.source_data  # subway dropped


def test_gather_keys_source_data_by_produced_type(monkeypatch) -> None:
    # source_data is keyed by the class each source produces, so consumers look up by type.
    weather = SimpleNamespace(conditions="Clear")
    monkeypatch.setattr(nws_mod, "NwsClient", _fake_nws(returns=weather))
    monkeypatch.setattr(mta_mod, "MtaClient", _FakeMtaClientWithBoard)

    data = asyncio.run(pipeline.gather(Config.model_validate(CONFIG)))

    assert set(data.source_data) == {type(weather), MtaData}


def test_gather_fails_loud_on_non_source_error(monkeypatch) -> None:
    # Only SourceError is isolated; any other exception a fetch raises must escape the concurrent
    # gather (return_exceptions=True captures it, but the reduce re-raises non-SourceError), so a
    # real bug fails loud instead of masquerading as a degraded source.
    monkeypatch.setattr(nws_mod, "NwsClient", _fake_nws(raises=ValueError("boom")))
    monkeypatch.setattr(mta_mod, "MtaClient", _FakeMtaClient)

    with pytest.raises(ValueError):
        asyncio.run(pipeline.gather(Config.model_validate(CONFIG)))


def test_gather_rejects_two_sources_producing_the_same_type(monkeypatch) -> None:
    # source_data is keyed by produced type, so two sources yielding the same class would clobber.
    # That's a misconfiguration, not a degraded source: the reduce fails loud rather than pick one.
    # Here the nws fake is coerced to produce an MtaData, colliding with the mta source's MtaData.
    monkeypatch.setattr(nws_mod, "NwsClient", _fake_nws(returns=MtaData(boards=[])))
    monkeypatch.setattr(mta_mod, "MtaClient", _FakeMtaClientWithBoard)

    with pytest.raises(SourceError):
        asyncio.run(pipeline.gather(Config.model_validate(CONFIG)))


def test_run_once_writes_kindle_ready_image(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(nws_mod, "NwsClient", _fake_nws(returns=None))
    _patch_render_sources(monkeypatch)
    cfg = _config(tmp_path)

    result = asyncio.run(pipeline.run_once(cfg))

    assert result.written == [cfg.dashboards["main"].output_path]
    assert result.failed == []
    out = Image.open(BytesIO(result.written[0].read_bytes()))
    assert out.size == (100, 140)  # fitted to the configured dimensions
    assert out.mode == "L"  # grayscale for e-ink


def test_run_once_renders_every_dashboard_from_one_gather(tmp_path, monkeypatch) -> None:
    # Two dashboards render from a single data fetch, each to its own path and dimensions.
    gathers = {"count": 0}
    real_gather = pipeline.gather

    def _counting_gather(cfg):
        gathers["count"] += 1
        return real_gather(cfg)

    monkeypatch.setattr(nws_mod, "NwsClient", _fake_nws(returns=None))
    monkeypatch.setattr(mta_mod, "MtaClient", _FakeMtaClientWithBoard)
    monkeypatch.setattr(pipeline, "gather", _counting_gather)

    cfg = _config(tmp_path)
    cfg.dashboards["wide"] = cfg.dashboards["main"].model_copy(
        update={"output_path": tmp_path / "out" / "wide.png", "width": 160, "height": 90}
    )

    result = asyncio.run(pipeline.run_once(cfg))

    assert gathers["count"] == 1  # data fetched exactly once, shared across dashboards
    assert set(result.written) == {
        cfg.dashboards["main"].output_path,
        cfg.dashboards["wide"].output_path,
    }
    assert Image.open(BytesIO(cfg.dashboards["main"].output_path.read_bytes())).size == (100, 140)
    assert Image.open(BytesIO(cfg.dashboards["wide"].output_path.read_bytes())).size == (160, 90)


def test_run_loops_until_interrupted(monkeypatch) -> None:
    calls = {"count": 0}

    async def _run_once(cfg):
        calls["count"] += 1

    # Let three iterations complete, then interrupt from within sleep to exit the loop.
    async def _sleep(seconds):
        if calls["count"] >= 3:
            raise KeyboardInterrupt
        return None

    monkeypatch.setattr(pipeline, "run_once", _run_once)
    monkeypatch.setattr(pipeline.asyncio, "sleep", _sleep)

    asyncio.run(pipeline.run(Config.model_validate(CONFIG)))  # returns cleanly on KeyboardInterrupt

    assert calls["count"] == 3


def test_run_continues_after_iteration_failure(monkeypatch) -> None:
    calls = {"count": 0}

    async def _run_once(cfg):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("transient render failure")

    async def _sleep(seconds):
        if calls["count"] >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(pipeline, "run_once", _run_once)
    monkeypatch.setattr(pipeline.asyncio, "sleep", _sleep)

    asyncio.run(pipeline.run(Config.model_validate(CONFIG)))  # first iter raises, loop survives

    assert calls["count"] == 2  # retried after the failure


def test_run_once_isolates_a_failing_dashboard(tmp_path, monkeypatch) -> None:
    # A render failure for one dashboard is caught and logged; the other dashboards still render.
    # (Only a SourceError is swallowed inside gather(); render errors are isolated per dashboard
    # here so one bad layout can't sink the rest.)
    monkeypatch.setattr(nws_mod, "NwsClient", _fake_nws(returns=None))
    _patch_render_sources(monkeypatch)

    cfg = _config(tmp_path)
    # A healthy glanceable dashboard, plus one pointing at a layout that doesn't exist (its render
    # raises LayoutError, isolated to that dashboard).
    cfg.dashboards["broken"] = cfg.dashboards["main"].model_copy(
        update={"output_path": tmp_path / "out" / "broken.png", "layout": "does-not-exist"}
    )

    result = asyncio.run(pipeline.run_once(cfg))  # does not raise

    assert result.written == [
        cfg.dashboards["main"].output_path
    ]  # the healthy dashboard still wrote
    assert result.failed == ["broken"]  # the failure is reported, not swallowed
    assert not cfg.dashboards["broken"].output_path.exists()


def test_run_once_skips_render_when_all_sources_down(tmp_path, monkeypatch) -> None:
    # Every source fails: run_once must not spend a paid generation or overwrite the last image.
    # (A source that *succeeds* with empty data still counts as present; skipping needs true
    # failure, so both sources are made to raise/return-nothing here.)
    monkeypatch.setattr(nws_mod, "NwsClient", _fake_nws(returns=None))  # no weather data
    monkeypatch.setattr(mta_mod, "MtaClient", _FailingMtaClient)  # subway feed down

    def _must_not_render(cfg, data, dash):
        raise AssertionError("render() must not run when all sources are down")

    monkeypatch.setattr(pipeline, "render", _must_not_render)
    cfg = _config(tmp_path)
    path = cfg.dashboards["main"].output_path
    path.parent.mkdir(parents=True)
    path.write_bytes(b"PREVIOUS-IMAGE")  # a prior good dashboard

    result = asyncio.run(pipeline.run_once(cfg))

    assert result.written == []  # signals "nothing written"
    assert result.failed == []  # skipped, not failed — a one-shot should still exit 0
    assert path.read_bytes() == b"PREVIOUS-IMAGE"  # last image preserved


def test_a_source_failing_in_init_is_isolated_like_any_other(monkeypatch, tmp_path) -> None:
    """Construction happens inside the per-source coroutine, so it gets the same isolation as fetch.

    Building the sources in ``gather``'s argument list instead would run every ``__init__`` eagerly
    while the generator is unpacked — outside what ``return_exceptions`` covers. One raising there
    (a source reading a credential, say) would take down the whole run and strand its siblings'
    coroutines un-awaited, rather than dropping just that source.
    """
    from kindle_dash_gen.sources import registry as registry_mod
    from kindle_dash_gen.sources.registry import register_source
    from kindle_dash_gen.sources.toolkit import SourceError

    class _Config(BaseModel):
        model_config = ConfigDict(extra="forbid")

    class _ExplodesOnConstruction:
        Config = _Config

        def __init__(self, config: _Config) -> None:
            raise SourceError("credential unreadable")

        async def fetch(self, now):  # never reached
            raise AssertionError("fetch should not run")

    class _Healthy:
        Config = _Config

        def __init__(self, config: _Config) -> None:
            pass

        async def fetch(self, now):
            return StationBoard(name="Survivor", arrivals_by_direction={})

    saved = dict(registry_mod._SOURCES)
    try:
        register_source("explodes", _ExplodesOnConstruction)
        register_source("healthy", _Healthy)
        cfg = _config(tmp_path)
        cfg.sources = {"explodes": {}, "healthy": {}}
        data = asyncio.run(pipeline.gather(cfg))
    finally:
        registry_mod._SOURCES.clear()
        registry_mod._SOURCES.update(saved)

    # The bad source is dropped; the healthy one still contributed.
    assert [t.__name__ for t in data.source_data] == ["StationBoard"]
