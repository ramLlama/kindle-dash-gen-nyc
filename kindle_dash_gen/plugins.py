"""Render-plugin discovery.

There are two plugin directories, discovered by identical logic: the **bundled** layouts shipped
with the app (``kindle_dash_gen.render.layouts``, always loaded) and an optional **local**
directory of private plugins named by ``Config.plugins_path``. Each plugin is a subpackage that
calls :func:`kindle_dash_gen.render.layout.register_layout` at import time; discovery just
imports them. No entry-points are used — the project runs in place (``package = false``).
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import pkgutil
import sys
from pathlib import Path

log = logging.getLogger(__name__)

_BUNDLED_ROOT = "kindle_dash_gen.render.layouts"

_bundled_loaded = False
_loaded_local: set[Path] = set()


def load_plugins(local_dir: Path | None = None) -> None:
    """Import the bundled layout plugins, and those in ``local_dir`` if given (both idempotent).

    ``local_dir`` (from ``Config.plugins_path``) is an absolute directory (enforced by config
    validation). A configured directory that does not exist is logged as a warning (a likely
    misconfiguration), but a plugin that exists and fails to import propagates — we never silently
    swallow a broken plugin.
    """
    global _bundled_loaded
    if not _bundled_loaded:
        _import_submodules(_BUNDLED_ROOT)
        _bundled_loaded = True
    if local_dir is not None:
        _load_local(local_dir)


def _load_local(local_dir: Path) -> None:
    """Discover a local plugins dir: put its parent on ``sys.path`` and import it as a package.

    Importing it by directory name (rather than from file paths) keeps normal package semantics, so
    a plugin subpackage's own imports work. A configured dir that is missing warns rather than
    failing the whole render.
    """
    resolved = local_dir.resolve()
    if resolved in _loaded_local:
        return
    if not resolved.is_dir():
        log.warning("plugins_path %s does not exist; no local plugins loaded", resolved)
        _loaded_local.add(resolved)  # don't re-warn every render
        return
    parent = str(resolved.parent)
    # Append (not prepend): the plugin dir becomes importable without shadowing stdlib/site-packages
    # if it happens to contain a name that collides with a real module.
    if parent not in sys.path:
        sys.path.append(parent)
    _import_submodules(resolved.name)
    _loaded_local.add(resolved)


def _import_submodules(package: str) -> None:
    """Import ``package`` and every immediate submodule/subpackage (each self-registers on import).

    A genuinely absent package is a no-op (a fresh clone has no local plugins). But if the package
    exists, any import error inside it — a broken plugin, a missing dependency — propagates rather
    than being silently skipped, so misconfigurations fail fast instead of surfacing later as a
    confusing "unknown layout".
    """
    if importlib.util.find_spec(package) is None:
        return
    pkg = importlib.import_module(package)
    for info in pkgutil.iter_modules(pkg.__path__, pkg.__name__ + "."):
        importlib.import_module(info.name)
