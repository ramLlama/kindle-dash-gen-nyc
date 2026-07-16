"""The bundled ``mta`` source: real-time NYC subway arrivals via the nyct-gtfs feeds.

A source plugin like any other (registers via :func:`register_source` at import). The client and
config live in :mod:`.source`; the data it produces in :mod:`.model`. Registration must happen here
in the package ``__init__`` because plugin discovery imports each source subpackage (not its inner
modules), so importing :mod:`.source` here is what makes the ``register_source`` call fire.
"""

from __future__ import annotations

from kindle_dash_gen.sources.registry import register_source

from .source import MtaSource

register_source("mta", MtaSource)
