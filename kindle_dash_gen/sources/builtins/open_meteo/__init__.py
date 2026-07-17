"""The bundled ``open-meteo`` data source (weather + air quality), discovered as a plugin.

Importing this package registers the source; the registry and pipeline never special-case it.
See :mod:`.source` for the client/config and :mod:`.model` for the produced data type.
"""

from __future__ import annotations

from kindle_dash_gen.sources.registry import register_source

from .source import OpenMeteoSource

register_source("open-meteo", OpenMeteoSource)
