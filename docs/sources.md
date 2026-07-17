# Data source plugins

A **source** fetches one kind of data (weather, subway arrivals, …) and contributes it to the
dashboard. Sources are plugins, exactly like render [layouts](plugins.md): the registry starts empty
and every source — including the bundled `nws` (US weather), `open-meteo` (global weather + air
quality), and `mta` (subway) — registers itself the same way. There is no privileged builtin. You can add your own source locally without touching the
app, and a private source has access to the same API the bundled ones use, so `nws`/`mta` could be
recreated 1:1 as private plugins.

Each source is configured by a `[sources.<name>]` table in your config, where `<name>` is the
source's registered name. Configure only the sources you want; **zero sources is valid** (every
render then legitimately skips, keeping the last image).

## The two plugin directories

Both are discovered by identical logic (`kindle_dash_gen/plugins.py`), the same mechanism that finds
layouts:

1. **Bundled** — `kindle_dash_gen/sources/builtins/`, shipped with the app. Always loaded.
2. **Local** — a directory of your private plugins, named by `plugins_path` in your config (the same
   directory that holds private layouts — one dir serves both kinds). Loaded only when set, imported
   by directory name, must be a Python package (have an `__init__.py`), and the path must be
   **absolute**. A directory that doesn't exist logs a warning rather than failing.

```toml
# config.toml — an absolute package directory of private plugins (layouts and/or sources)
plugins_path = "/home/you/kindle-dash-gen-nyc/kindle_dash_gen_nyc_plugins"
```

## The contract

A source is a **subpackage** of a plugin directory that, on import, calls `register_source`:

```python
# <plugins_dir>/my_source/__init__.py
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from kindle_dash_gen.sources.registry import Source, register_source
from kindle_dash_gen.sources.toolkit import SourceError


class MyData:
    """Whatever data class your source produces (its type is the source_data key)."""

    def __init__(self, items: list[str]) -> None:
        self.items = items


class MyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")  # reject unknown keys in [sources.my_source]

    endpoint: str
    limit: int = 10


class _MySource(Source[MyConfig]):
    Config = MyConfig  # the registry validates the [sources.my_source] table against this

    def __init__(self, config: MyConfig) -> None:
        self._config = config

    async def fetch(self, now: datetime) -> MyData:
        try:
            # I/O is async: await it (e.g. niquests.AsyncSession) so the pipeline can fetch
            # every source concurrently.
            ...  # hit self._config.endpoint, build MyData
        except SomeLibraryError as exc:
            raise SourceError(f"my_source unavailable: {exc}") from exc
        return MyData(items=[...])


register_source("my_source", _MySource)
```

Then configure it:

```toml
[sources.my_source]
endpoint = "https://example.com/api"
limit = 5
```

## The `Source` protocol

A source class satisfies `kindle_dash_gen.sources.registry.Source`:

- `Config: ClassVar[type[BaseModel]]` — the pydantic model for this source's `[sources.<name>]`
  table. Keep `model_config = ConfigDict(extra="forbid")` so unknown keys in your table are rejected.
  The registry validates the raw table against `Config` before constructing the source, so a bad or
  unknown source fails fast at startup, not mid-run.
- `__init__(self, config)` — receives the validated `Config` instance. Declaring the class as
  `Source[MyConfig]` types this parameter as `MyConfig`.
- `async def fetch(self, now: datetime) -> <data>` — a coroutine returning the source's data object,
  whatever class it is; that class becomes the object's key in `DashboardData.source_data`. `fetch`
  is **async** so the pipeline can fetch every source concurrently — `await` your I/O inside it (e.g.
  `niquests.AsyncSession`). Return `None` when there is simply no data this run. A fetch **failure**
  raises `SourceError` (or a subclass); the pipeline isolates it (drops this source, logs, and
  renders with whatever else was gathered). `now` is the single generation timestamp shared across
  the render.
- `cli(cls) -> typer.Typer` *(optional classmethod)* — source-specific CLI subcommands. The CLI
  mounts every source under `source <name>`: with no verb, `source <name>` runs `fetch` and
  pretty-prints the result (the default); the commands in your returned `typer.Typer` become verbs
  (e.g. the `mta` source exposes `source mta list-stations`). Omit `cli` and the source just gets the
  default fetch. Sources resolve at invocation time, so a local `plugins_path` source's verbs work
  exactly like a bundled source's. (`list` is a reserved name — the group's own listing command.)

## `source_data` keying

`gather()` collects each source's result into `DashboardData.source_data: dict[type, Any]`, keyed by
the produced object's class. Consumers (layouts, prompt templates) look their data up by type and
degrade gracefully when it's absent:

```python
from kindle_dash_gen.sources.builtins.nws.model import NwsData
weather = data.source_data.get(NwsData)  # None if the weather source failed or was absent
```

A source owns the data class it produces (it lives with the source, e.g. `NwsData` in
`sources/builtins/nws/model.py`), so there is no shared cross-source model. Two sources that produce
the **same** data class collide (one would silently overwrite the other), so `gather()` fails fast
with a `SourceError` if that happens. Give each source its own data class.

## Error isolation (`SourceError`)

Every source-specific error subclasses `kindle_dash_gen.sources.toolkit.SourceError`. The pipeline
catches `SourceError` generically: a raising source is dropped (its data absent) and the render
proceeds with the rest. If **every** configured source fails, the render is skipped entirely so the
last good image is left in place. Raise `SourceError` (or your own subclass of it) for a fetch
failure; return `None` for the benign "nothing to report this run" case.

## Notes

- Registering a name already taken raises `SourceError` (two plugins claiming the same source is a
  configuration error, so it fails fast).
- Discovery uses import side-effects, not entry points — the project runs in place
  (`package = false`), so there is no install step.
- The bundled `nws` (`sources/builtins/nws/`), `open-meteo` (`sources/builtins/open_meteo/`), and
  `mta` (`sources/builtins/mta/`) are the worked references: each owns its config models and registers
  itself, structurally identical to a private source. They split logic across three files —
  `model.py` (the produced data class), `source.py` (the client + `Config`), and `__init__.py` (which
  imports `source.py` and calls `register_source`). `nws` and `open-meteo` are two independent weather
  providers producing their own peer data types (`NwsData` vs `OpenMeteoData`, no shared model), a
  worked example of the provider-owned-data principle above; `open-meteo` also shows an `asyncio.gather`
  fan-out where one endpoint (air quality) degrades to `None` on failure while the other fails the source.
  A single `__init__.py` works too; discovery only imports the subpackage, so registration must be
  reachable from `__init__.py`.
- Keep data in SI units through your data classes and round/convert only at display time (the "SI
  internally, round at display" invariant); the display formatters live in the render toolkit.
