"""Plugin discovery and registration tests.

Cover the layout and source registry APIs, discovery of the bundled plugins, and loading a local
plugins directory named by config (the same mechanism a private plugin like the home MTA map uses,
and which serves layouts and sources alike).
"""

from __future__ import annotations

import pytest

from kindle_dash_gen import plugins
from kindle_dash_gen.render import layout
from kindle_dash_gen.render.layout import LayoutError, register_layout
from kindle_dash_gen.sources import registry as source_registry_mod
from kindle_dash_gen.sources.registry import SourceError, register_source


@pytest.fixture
def registry():
    """Snapshot the layout registry and restore it, so test registrations don't leak.

    Loads the bundled plugins first so they're in the snapshot (and survive restore): once a plugin
    module is imported it won't re-register, so rolling it out of the registry would be permanent.
    """
    plugins.load_plugins()
    saved = dict(layout._LAYOUTS)
    yield layout._LAYOUTS
    layout._LAYOUTS.clear()
    layout._LAYOUTS.update(saved)


@pytest.fixture
def source_registry():
    """Snapshot the source registry and restore it, so test registrations don't leak."""
    plugins.load_plugins()
    saved = dict(source_registry_mod._SOURCES)
    yield source_registry_mod._SOURCES
    source_registry_mod._SOURCES.clear()
    source_registry_mod._SOURCES.update(saved)


class _Stub:
    def __init__(self, width, height, fonts, units) -> None:
        pass

    def render(self, data):
        return None


def test_register_layout_adds_by_name(registry) -> None:
    register_layout("stub_added", _Stub)
    assert registry["stub_added"] is _Stub


def test_register_layout_rejects_duplicate(registry) -> None:
    register_layout("stub_dup", _Stub)
    with pytest.raises(LayoutError):
        register_layout("stub_dup", _Stub)


def test_load_plugins_discovers_bundled_glanceable() -> None:
    # The bundled layout is not special-cased: it's discovered like any plugin.
    plugins.load_plugins()
    assert "glanceable" in layout._LAYOUTS


def test_load_plugins_is_idempotent() -> None:
    # Calling twice must not raise (no duplicate registration) — the one invariant most likely
    # to regress if discovery ever re-imports and re-registers.
    plugins.load_plugins()
    plugins.load_plugins()


def test_load_plugins_missing_local_dir_is_noop(tmp_path) -> None:
    plugins.load_plugins(local_dir=tmp_path / "not_a_package")  # must not raise


def test_load_plugins_local_idempotent(tmp_path, registry) -> None:
    pkg = tmp_path / "plugs"
    (pkg / "one").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "one" / "__init__.py").write_text(
        "from kindle_dash_gen.render.layout import register_layout\n"
        "register_layout('once_only', object)\n"
    )
    plugins.load_plugins(local_dir=pkg)
    plugins.load_plugins(local_dir=pkg)  # second scan must not re-register / raise
    assert "once_only" in layout._LAYOUTS


def test_broken_local_plugin_propagates(tmp_path) -> None:
    # A plugin that exists but fails to import must surface, not be silently skipped.
    pkg = tmp_path / "brokenplugs"
    (pkg / "bad").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "bad" / "__init__.py").write_text("import nonexistent_dependency_xyz\n")
    with pytest.raises(ModuleNotFoundError):
        plugins.load_plugins(local_dir=pkg)


def test_load_plugins_discovers_a_local_plugin(tmp_path, registry) -> None:
    # A local plugins directory named by config is imported by directory name; its subpackage
    # registers a layout on import, exactly like the bundled ones.
    pkg = tmp_path / "myplugins"
    (pkg / "hi").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "hi" / "__init__.py").write_text(
        "from kindle_dash_gen.render.layout import register_layout\n"
        "class Hi:\n"
        "    def __init__(self, width, height, fonts, units):\n"
        "        pass\n"
        "    def render(self, data):\n"
        "        return None\n"
        "register_layout('hi_local', Hi)\n"
    )

    plugins.load_plugins(local_dir=pkg)

    assert "hi_local" in layout._LAYOUTS


def test_register_source_adds_by_name(source_registry) -> None:
    register_source("src_added", object)
    assert source_registry["src_added"] is object


def test_register_source_rejects_duplicate(source_registry) -> None:
    register_source("src_dup", object)
    with pytest.raises(SourceError):
        register_source("src_dup", object)


def test_load_plugins_discovers_a_local_source(tmp_path, source_registry) -> None:
    # One local plugins dir serves both kinds: a subpackage that calls register_source is
    # discovered by the same mechanism as a layout plugin.
    pkg = tmp_path / "mysources"
    (pkg / "feed").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "feed" / "__init__.py").write_text(
        "from kindle_dash_gen.sources.registry import register_source\n"
        "register_source('feed_local', object)\n"
    )

    plugins.load_plugins(local_dir=pkg)

    assert "feed_local" in source_registry_mod._SOURCES
