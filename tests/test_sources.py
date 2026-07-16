"""Source registry / config-resolution tests.

Cover ``build_sources``: it validates each ``[sources.<name>]`` slice against the registered
plugin's ``Config``, and fails fast on an unknown source name or an invalid slice.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from kindle_dash_gen import plugins
from kindle_dash_gen.sources import registry as source_registry_mod
from kindle_dash_gen.sources.registry import SourceError, build_sources, register_source


class _DemoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: int


class _DemoSource:
    """A throwaway source: carries its Config model and echoes the config on fetch."""

    Config = _DemoConfig

    def __init__(self, config: _DemoConfig) -> None:
        self.config = config

    def fetch(self, now: datetime) -> _DemoConfig:
        return self.config


@pytest.fixture
def source_registry():
    """Snapshot/restore the source registry so a test's registrations don't leak."""
    plugins.load_plugins()
    saved = dict(source_registry_mod._SOURCES)
    yield source_registry_mod._SOURCES
    source_registry_mod._SOURCES.clear()
    source_registry_mod._SOURCES.update(saved)


def test_build_sources_validates_and_resolves(source_registry) -> None:
    register_source("demo", _DemoSource)
    resolved = build_sources({"demo": {"value": 7}})
    cls, config = resolved["demo"]
    assert cls is _DemoSource
    assert isinstance(config, _DemoConfig)
    assert config.value == 7


def test_build_sources_rejects_unknown_name(source_registry) -> None:
    with pytest.raises(SourceError):
        build_sources({"nope": {}})


def test_build_sources_rejects_extra_key(source_registry) -> None:
    # Each source keeps extra="forbid", so an unknown key in its slice is a validation error.
    register_source("demo_strict", _DemoSource)
    with pytest.raises(ValidationError):
        build_sources({"demo_strict": {"value": 1, "bogus": True}})


def test_build_sources_empty_is_empty(source_registry) -> None:
    # No configured sources is valid (every render then legitimately skips).
    assert build_sources({}) == {}


def test_build_sources_resolves_bundled_nws_and_mta() -> None:
    # The real bundled sources validate their own [sources.<name>] slices into their Config models.
    from kindle_dash_gen.sources.builtins.mta.source import MtaConfig
    from kindle_dash_gen.sources.builtins.nws.source import NwsConfig

    resolved = build_sources(
        {
            "nws": {"latitude": 40.7, "longitude": -73.9, "user_agent": "x"},
            "mta": {"stations": {"Union Sq": {"platforms": [{"lines": ["L"], "stop_id": "L03"}]}}},
        }
    )

    assert set(resolved) == {"nws", "mta"}
    assert isinstance(resolved["nws"][1], NwsConfig)
    assert isinstance(resolved["mta"][1], MtaConfig)


def test_build_sources_rejects_extra_key_in_bundled_source() -> None:
    # Each bundled source keeps extra="forbid", so an unknown key in its slice is rejected.
    with pytest.raises(ValidationError):
        build_sources({"nws": {"latitude": 1.0, "longitude": 2.0, "user_agent": "x", "bogus": 1}})
