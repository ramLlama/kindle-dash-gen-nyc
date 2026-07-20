"""Data-source plugin registry and dispatch.

The source-side analogue of :mod:`kindle_dash_gen.render.layout`. A *source* fetches one kind of
data (weather, subway arrivals, …) and contributes it to :class:`DashboardData.source_data`, keyed
by the produced data class. Sources are **plugins**: each registers via :func:`register_source` at
import time and is discovered by :mod:`kindle_dash_gen.plugins`. Nothing is special-cased here — the
registry starts empty. See ``docs/sources.md``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar, Protocol, TypeVar

from pydantic import BaseModel

from .toolkit import SourceError

__all__ = [
    "Source",
    "SourceError",
    "build_sources",
    "register_source",
    "registered_sources",
    "source_class",
]

# The config type each source is built from. Its variance is nominal: it appears only in __init__,
# which Protocols exclude from structural checks, so mypy computes the expected variance as
# covariant. A concrete source (e.g. NwsSource) annotates its own __init__ with its real config.
ConfigT_co = TypeVar("ConfigT_co", bound=BaseModel, covariant=True)


class Source(Protocol[ConfigT_co]):
    """The interface a source class implements: declare a config model, construct, then fetch.

    ``Config`` is the pydantic model for this source's ``[sources.<name>]`` table; the registry
    validates the raw TOML slice against it (each source keeps ``extra="forbid"``, so its own
    unknown keys are rejected). The source is constructed from that validated config, then
    :meth:`fetch` (a coroutine — source I/O is async so the pipeline can fetch every source
    concurrently) returns its data object — whatever class the source produces, which becomes its
    key in ``DashboardData.source_data`` — or ``None`` when there is simply no data this run (the
    return is typed ``Any`` since the class varies per source; ``None`` is a valid value the
    pipeline treats as "absent"). A fetch *failure* raises a :class:`SourceError` (or subclass),
    which the pipeline isolates.

    **Datetimes are aware UTC.** ``now`` is aware UTC, and a source should return aware UTC
    datetimes in the data it produces. The registry does not enforce this, but honoring it is what
    lets one process serve dashboards in different regions: values from different sources stay
    directly comparable, and each layout converts to its own display timezone (``docs/sources.md``).
    """

    Config: ClassVar[type[BaseModel]]

    def __init__(self, config: ConfigT_co) -> None: ...

    async def fetch(self, now: datetime) -> Any: ...


# Populated only by plugin discovery (see :mod:`kindle_dash_gen.plugins`) — no builtins here.
_SOURCES: dict[str, type[Source[Any]]] = {}


def register_source(name: str, factory: type[Source[Any]]) -> None:
    """Register source class ``factory`` under ``name``; raise on a duplicate name.

    Plugins call this at import time. ``name`` is the source's ``[sources.<name>]`` config key.
    Duplicate names are a configuration error (two plugins claiming the same source), so they fail
    fast rather than silently shadowing.
    """
    if name in _SOURCES:
        raise SourceError(f"source {name!r} is already registered")
    _SOURCES[name] = factory


def registered_sources() -> list[str]:
    """Every registered source name, discovering plugins first (for CLI listing/debug).

    Unlike :func:`build_sources` (which resolves only the *configured* sources), this reports every
    source the app knows how to run. Loads the bundled plugins as a safety net; a caller wanting
    local (``plugins_path``) sources listed must have loaded those already.
    """
    from .. import plugins  # lazy import: plugins imports source modules that import this module

    plugins.load_plugins()
    return sorted(_SOURCES)


def source_class(name: str) -> type[Source[Any]] | None:
    """The registered source class for ``name``, or ``None`` if no such source is registered.

    Discovers the bundled plugins first; a caller wanting local (``plugins_path``) sources resolved
    must have loaded those already. Lets the CLI resolve a source by name (e.g. for its ``cli()``
    verbs) without needing its ``[sources.<name>]`` config, unlike :func:`build_sources`.
    """
    from .. import plugins  # lazy import: plugins imports source modules that import this module

    plugins.load_plugins()
    return _SOURCES.get(name)


def build_sources(
    sources: dict[str, dict[str, Any]],
) -> dict[str, tuple[type[Source[Any]], BaseModel]]:
    """Resolve each ``[sources.<name>]`` slice to its plugin class and validated config.

    The choke point between raw config and live sources: validates every configured source against
    its plugin's ``Config`` (so a bad or unknown source fails fast, before any fetch), and returns
    ``{name: (source_class, config)}`` for the caller to construct and fetch. Loads the bundled
    plugins first as a safety net; a caller wanting local (``plugins_path``) sources must have
    loaded those already (e.g. via ``plugins.load_plugins(cfg.plugins_path)``).
    """
    from .. import plugins  # lazy import: plugins imports source modules that import this module

    plugins.load_plugins()
    resolved: dict[str, tuple[type[Source[Any]], BaseModel]] = {}
    for name, raw in sources.items():
        if name not in _SOURCES:
            raise SourceError(f"unknown source {name!r}; available: {sorted(_SOURCES)}")
        cls = _SOURCES[name]
        resolved[name] = (cls, cls.Config.model_validate(raw))
    return resolved
