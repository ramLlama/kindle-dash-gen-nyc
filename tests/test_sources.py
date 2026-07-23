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
from kindle_dash_gen.sources.toolkit import Secret


class _DemoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: int


class _DemoSource:
    """A throwaway source: carries its Config model and echoes the config on fetch."""

    Config = _DemoConfig

    def __init__(self, config: _DemoConfig) -> None:
        self.config = config

    async def fetch(self, now: datetime) -> _DemoConfig:
        return self.config


class _SecretConfig(BaseModel):
    """A throwaway config typing a credential as Secret, the way a keyed source does."""

    model_config = ConfigDict(extra="forbid")

    api_key: Secret


class _SecretSource:
    Config = _SecretConfig

    def __init__(self, config: _SecretConfig) -> None:
        self.config = config

    async def fetch(self, now: datetime) -> _SecretConfig:
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


def test_build_sources_resolves_a_secret_field(source_registry, monkeypatch) -> None:
    # A Secret-typed credential survives the real config path: build_sources validates the raw
    # [sources.<name>] table into the plugin's Config, and the value resolves at use time.
    monkeypatch.setenv("KDG_TEST_SOURCE_KEY", "resolved-key")
    register_source("demo_secret", _SecretSource)
    _, config = build_sources(
        {"demo_secret": {"api_key": {"value_from_env": "KDG_TEST_SOURCE_KEY"}}}
    )["demo_secret"]
    assert isinstance(config, _SecretConfig)
    assert config.api_key.value == "resolved-key"


def test_build_sources_resolves_bundled_nws_and_mta() -> None:
    # The real bundled sources validate their own [sources.<name>] slices into their Config models.
    from kindle_dash_gen.sources.builtins.mta.source import MtaConfig
    from kindle_dash_gen.sources.builtins.nws.source import NwsConfig

    resolved = build_sources(
        {
            "nws": {
                "user_agent": "x",
                "locations": {"home": {"latitude": 40.7, "longitude": -73.9}},
            },
            "mta": {"stations": {"Union Sq": {"platforms": [{"lines": ["L"], "stop_id": "L03"}]}}},
        }
    )

    assert set(resolved) == {"nws", "mta"}
    assert isinstance(resolved["nws"][1], NwsConfig)
    assert isinstance(resolved["mta"][1], MtaConfig)


def test_build_sources_rejects_extra_key_in_bundled_source() -> None:
    # Each bundled source keeps extra="forbid", so an unknown key in its slice is rejected.
    with pytest.raises(ValidationError):
        build_sources(
            {
                "nws": {
                    "user_agent": "x",
                    "locations": {"h": {"latitude": 1.0, "longitude": 2.0}},
                    "bogus": 1,
                }
            }
        )
