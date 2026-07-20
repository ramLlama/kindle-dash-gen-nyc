# kindle-dash-gen

## What This Project Does

A Python CLI that periodically generates a Kindle e-ink dashboard image for NYC. It pulls the
local NWS weather forecast plus real-time MTA subway arrivals, renders the whole dashboard with a
deterministic local **pillow** layout, post-processes the resulting PNG for a Kindle Voyage
(grayscale, exact pixel dimensions, 16 hardware gray levels), and writes it to a configured path
for syncing to the device. Intended to run unattended on an interval (e.g. every 5 minutes).

## Tech Stack

- **Python 3.14+** (uses `tomllib`, `StrEnum`, `X | None` unions everywhere)
- **uv** for env/deps. The project is `package = false` — run in place, never installed.
- **typer** `0.26.*` — CLI framework
- **pydantic** `2.*` — config validation (`extra="forbid"` on every model)
- **niquests** `3.*` — HTTP client (NWS + Open-Meteo; uses `AsyncSession` for concurrent fetches);
  `niquests-mock` in tests
- **nyct-gtfs** `2.*` — MTA GTFS-realtime feed parsing
- **pillow** `12.*` — the rendering layout and image post-processing
- **fontconfig** (`fc-match`, system tool) — a layout resolves a font family name to a file;
  required at runtime
- **pytest** `9.*`, **ruff** `0.15.*` — test + lint gates

## Repository Structure

```
kindle_dash_gen/
  __main__.py          # `python -m kindle_dash_gen` entry -> cli.run()
  cli.py               # typer app: version, run, source group (dynamic), dashboard group
  config.py            # TOML -> pydantic Config; Dashboard (output spec + layout_config); Secret
  pipeline.py          # gather -> layout.render -> post_process -> atomic write
  format.py            # display formatters (temp/reading/apparent/wind/eta); SI -> display
  models/              # frozen dataclasses (domain models, no presentation)
    dashboard_data.py  # DashboardData (source_data keyed by produced type) — the only model here
  sources/             # data-source plugins (source-side mirror of render/)
    toolkit.py         # public plugin API: SourceError (base all source errors subclass) + Secret
    registry.py        # Source protocol, register_source, build_sources() dispatch
    builtins/          # bundled source plugins (discovered, not special-cased)
      nws/             # "nws" source (three-file package):
        __init__.py    #   imports source.py -> register_source("nws", NwsSource)
        source.py      #   NwsSource + NwsConfig + NwsClient
        model.py       #   NwsData (+ Temperature, HourlyForecast) — the produced data class
      open_meteo/      # "open-meteo" source (three-file package, keyless + global):
        __init__.py    #   imports source.py -> register_source("open-meteo", OpenMeteoSource)
        source.py      #   OpenMeteoSource + OpenMeteoConfig + OpenMeteoClient (async, forecast + AQI)
        model.py       #   OpenMeteoData (+ Temperature, HourlyForecast, wmo_description) — produced data
      mta/             # "mta" source (three-file package, owns its assets):
        __init__.py    #   imports source.py -> register_source("mta", MtaSource)
        source.py      #   MtaSource (+ cli() verb `list-stations`) + MtaConfig (Platform/Station) + MtaClient
        model.py       #   MtaData (+ Direction, StationBoard, TrainArrival) — the produced data class
        assets/stations.csv  #   bundled station lookup (for `source mta list-stations`)
  render/              # turn data into a Kindle-ready PNG (pillow layout + post-process)
    layout.py          # Layout protocol (owns its Config), register/validate/build_layout, render()
    toolkit.py         # layout public plugin API (Fonts, INK/PAPER, fit_font, assets, format helpers, Secret)
    builtins/          # bundled layout plugins (discovered, not special-cased)
      glanceable/      # the default layout as a self-contained plugin (owns GlanceableConfig + assets/icons/)
    postprocess.py     # post_process(): grayscale, fit, quantize (Pillow); Image in, PNG bytes out
  plugins.py           # plugin discovery: bundled layout + source roots + optional local plugins_path
tests/                 # pytest, one file per module; HTTP mocked with niquests-mock
config.example.toml    # copy to config.toml (gitignored) and edit
docs/plugins.md        # how to write a render layout plugin (the public contract)
docs/sources.md        # how to write a data-source plugin (the public contract)
```

## Key Concepts & Domain Model

- **Provider-shaped data, owned by each source.** There is no shared cross-provider model
  hierarchy: each source defines and owns the data class it produces (in its own `model.py`), and a
  layout reconciles multiple providers in its own local adapter. This is the guiding decision behind
  the multi-provider work (Open-Meteo weather, NWS alerts, AQI).
- **DashboardData** (`models/dashboard_data.py`, the only model left under `models/`) is the
  aggregate handed to the renderer: `generated_at` (aware UTC; also used as "now" for ETAs) plus
  `source_data: dict[type, Any]`, keyed by each source's produced data class (e.g. `NwsData`,
  `MtaData`). Consumers look up defensively: `data.source_data.get(NwsData)`; a failed or empty
  source is simply absent from the dict.
- **MtaData** (`sources/builtins/mta/model.py`) wraps `list[StationBoard]` (as `.boards`) so the
  subway source contributes a single typed value (a bare list can't be a `source_data` key).
- **Station vs Platform** (`sources/builtins/mta/`): the mta source owns these config models. A
  **Station** is a display board keyed by name; it merges one or more **Platform** entries (each a
  GTFS base stop id + the lines serving it) into per-direction arrival lists. Example: "Union Sq"
  merges the N/Q/R/W, 4/5/6, and L platforms into one board. Boards are **uncapped** — the layout
  decides how many arrivals to show at render.
- **Direction** is a `StrEnum` with values `"N"`/`"S"` (GTFS uptown/downtown, nominal for the L).
- **NwsData** (`sources/builtins/nws/model.py`) carries current conditions, `today` and `tomorrow`
  (each a `DailyHighLow(day, high, low)` — both days always present, readings `None` when unknown;
  each source owns its own copy of this class, like every other type), upcoming hours, and active
  weather **alerts** (`list[WeatherAlert]`, defaults to `[]`).
  `Temperature` bundles a `real` value with an optional `feels_like` (apparent). `WeatherAlert`
  mirrors the CAP alert fields NWS supplies (`event`, `category`, `severity`, `certainty`,
  `urgency`, `status`, `message_type`, `area_desc`, `sender_name`, `headline`, `description`,
  `instruction`, `response`, and the `effective`/`onset`/`expires`/`ends` timestamps); alerts are
  carried unfiltered (no severity knob) and are unused by any layout until a layout draws them.
- **OpenMeteoData** (`sources/builtins/open_meteo/model.py`) is a provider-owned peer to `NwsData`
  (its own independent `Temperature`/`HourlyForecast`, no shared hierarchy) for the fields Open-Meteo
  supplies: current conditions, `today`/`tomorrow` high/low, upcoming hours, and a raw WMO
  `weather_code` integer (**not** a description — the layout maps the code to an icon; the model owns
  a `wmo_description(code)` helper for canonical text only). It also carries **air-quality fields NWS
  has no equivalent for** — `us_aqi`, `pm2_5`, `pm10`, `aerosol_optical_depth` — which degrade to
  `None` when only the air-quality endpoint fails. A layout that renders weather reconciles whichever
  provider(s) are present in its own adapter (see the glanceable `_weather` adapter).

## Architecture Overview

Linear pipeline, wired in `pipeline.py`. The **fetch path is async**; the **render path stays
synchronous and sequential by design**. `gather()` (async — fetches every discovered source
**concurrently** via `asyncio.gather`, then reduces in `build_sources` order, isolating each) → for
each configured `[dashboards.<name>]`, rendered **sequentially**: `render_raw()` (`layout.render()`
builds the dashboard's layout from its `layout_config` and draws `DashboardData` at native size,
returning a raw Pillow `Image`) → `post_process()` (grayscale, fit, quantize — takes the `Image`,
returns PNG bytes) → atomic write to the dashboard's `output_path`. `run_once()` (async) returns a
`RunResult(written, failed)`; one dashboard's render failure is isolated (logged, others proceed).
`render`/`render_raw`/`post_process` and the `Layout.render` protocol are **not** async — that split
is deliberate. The CLI bridges with `asyncio.run(...)` at each command boundary; typer commands stay
plain sync `def`. The fit step is effectively a no-op since the layout already draws at exact size,
so only quantization matters. The `dashboard` CLI subcommands expose each step in isolation for
debugging. The "a layout reconciles multiple providers in its own adapter" principle is now realized
concretely: the bundled `glanceable` layout has a private `_weather` adapter that **combines**
whichever weather providers are present into one layout-local draw surface — hero/hourly from the
preferred provider (Open-Meteo, NWS fallback), **AQI off Open-Meteo and alerts off NWS
independently** (each absent when its provider isn't configured), so `render()` never inspects a
provider type. The hero draws the AQI badge (`format_aqi`) and the most-severe active alert
(`+N more` tail) through one shared `_metric_row`, which flags an alert — or an "Unhealthy"-or-worse
AQI (`aqi_is_unhealthy`, EPA 151+) — in bold behind the bundled `warning.png` icon.

See [architecture.md](architecture.md) for data flow, the NWS multi-step fetch, the Open-Meteo
concurrent forecast+AQI fetch, MTA feed deduplication, and the layout/post-process details.

## Development Workflow

```sh
uv sync
cp config.example.toml config.toml     # edit; config.toml is gitignored

# Run in place (NOT installed — always via -m):
uv run python -m kindle_dash_gen --help
uv run python -m kindle_dash_gen --config config.toml dashboard render out.png  # render only
uv run python -m kindle_dash_gen --config config.toml run --one-shot            # one iteration
uv run python -m kindle_dash_gen --config config.toml run                       # loop

# Verification gates (both must pass):
uv run pytest
uv run ruff check .
```

Global `--config` / `-c` defaults to `config.toml`; it is stored on the typer context and each
subcommand loads it on demand via `_config(ctx)`.

## Critical Idiosyncrasies & Gotchas

- **SI internally, round at display.** All weather data is kept in SI (°C, km/h) at full
  precision through the models and sources. Conversion and rounding happen only in `format.py`
  at output time. Do not round or convert units inside sources or models.
- **Sources report data; layouts make display decisions.** A source reports every value the provider
  gives and `None` where a value is genuinely unknown — it never picks *which* value the dashboard
  should show. Don't add a config knob to a source that encodes a display choice (a `rollover_hour`
  knob deciding whether high/low meant today or tomorrow was removed for exactly this reason). The
  worked example and full rule live in `docs/sources.md` ("Report data, not display decisions").
- **Aware UTC everywhere; a layout converts for display.** `gather()` uses `datetime.now(UTC)`, and
  every datetime that crosses a boundary (`generated_at`, `now` passed to `fetch`, every timestamp
  on a produced model) is timezone-aware UTC. Display conversion happens **only** in a layout, via
  its own required `timezone`. This is what lets one process render a New York and a Bay Area
  dashboard from the single shared fetch. The convention is stated in the `Source` protocol
  docstring but is **not enforced** by the registry — `docs/sources.md` is the enforcement
  mechanism, so a new source has to be told. Two traps a source must handle:
  - **Deriving a calendar date** ("today") must happen on the *local* value before converting to
    UTC. Past ~20:00 local the UTC date is already tomorrow, which would skew a daily high/low by a
    day every evening (see `nws/source.py`).
  - **Matching hourly buckets** must also happen in local time: provider timestamps sit on local
    hour boundaries, so truncating a UTC instant to the hour misaligns in half-hour-offset zones
    (India +05:30, Nepal +05:45, Chatham +12:45).
- **The Open-Meteo request must keep `timezone=auto` — never `timezone=UTC`.** That parameter also
  sets the boundaries Open-Meteo aggregates `daily` over. Under UTC a San Francisco high/low would
  be taken across a 17:00–17:00 local window (measurably different: 18.1 vs 20.8 on a sample day)
  and `daily.time[0]` would flip to tomorrow every afternoon. The naive-local timestamps are made
  aware via `ZoneInfo(forecast["timezone"])` — the response's **named** zone, deliberately not its
  `utc_offset_seconds`, since that offset is only correct at request time and applying it uniformly
  puts post-DST-transition hours on the wrong instant. A code comment records this; keep it.
- **MTA arrivals round-trip through the host zone on purpose.** `nyct_gtfs` returns
  `datetime.fromtimestamp(epoch)` — a naive value in the *host's* local wall clock. `_arrival_at`
  just calls `stop.arrival.astimezone(UTC)`: `astimezone` interprets a naive value as host-local,
  the same zone `fromtimestamp` rendered it in, so the two cancel and the exact original instant is
  recovered regardless of host zone (exact across DST fall-back too — `fromtimestamp` sets `fold`
  and `astimezone` honors it). This is why **no nyct_gtfs internals are touched**; don't "fix" it.
- **Multiple dashboards, one fetch.** Config has `dashboards: dict[str, Dashboard]` (named
  `[dashboards.<name>]` tables). `gather()` runs once and every dashboard renders from that shared
  data to its own `output_path`.
- **One renderer: the pillow layout.** Every dashboard renders deterministically via a local
  pillow **layout** (free, offline, exact — never garbles data). There is no backend concept and no
  dispatch: a dashboard just names a `layout`. A layout resolves its `font` family via fontconfig
  (`fc-match`); a missing font/asset raises `LayoutError`.
- **Both layouts and sources are plugins that own their config (no special builtins).**
  `plugins.load_plugins()` discovers two kinds by identical logic, each from a bundled root plus the
  optional shared local `plugins_path` dir (which hosts both kinds). Registries start empty, and
  each plugin declares a `Config: ClassVar[type[BaseModel]]` (all `extra="forbid"`) validated from
  its own config table — layouts mirror sources here:
  - **Layouts** register via `register_layout` at import, bundled root
    `kindle_dash_gen.render.builtins` (the `glanceable` layout lives at `render/builtins/glanceable/`).
    The `Layout` protocol is `Config` + `__init__(config, *, width, height)` +
    `render(data) -> PIL.Image.Image`. `build_layout`/`validate_layout` (in `render/layout.py`)
    validate the `[dashboards.<name>.layout_config]` table against the layout's `Config`, mirroring
    `build_sources`. Build on `render/toolkit.py` (`Fonts`, `INK`/`PAPER`, `fit_font`,
    `load_asset_image`, `LayoutError`, `Secret`). See `docs/plugins.md`.
  - **Sources** register via `register_source` at import, bundled root
    `kindle_dash_gen.sources.builtins` (the `nws` and `mta` sources). A source is a `Source`
    protocol class with a `Config` and an **async** `fetch(now)` (`async def fetch(self, now) -> Any`)
    so the pipeline can fetch every source concurrently — `await` I/O inside it (e.g.
    `niquests.AsyncSession`); `build_sources` validates each `[sources.<name>]` table. A source may also define an optional `cli(cls) -> typer.Typer` for
    source-specific CLI verbs (the `mta` source ships `source mta list-stations`). Build on
    `sources/toolkit.py` (`SourceError`, `Secret`). See `docs/sources.md`.
- **The `source` CLI subcommands are wired ahead of parsing.** typer has no native dynamic
  subcommands and its `TyperGroup`/vendored-click internals are unsupported, so `cli.py` mounts each
  source as a `source <name>` sub-typer *before* `app()` parses: the **bundled** sources at import
  (a module-level `_wire_source_commands()` call), and **local `plugins_path`** sources in `run()`
  **only for a `source` invocation** (gated via `_invoked_command`, so `version`/`run`/etc. never
  touch the plugin dir). `run()` sniffs `--config` (`_config_path_from_argv`), loads that config's
  `plugins_path` (a broken plugin propagates — fail fast, not a hidden "no such command"), then
  wires. `source <name>` with no verb fetches + rich-prints (a per-source `invoke_without_command`
  callback); a source's `cli()` plain commands graft under it; `list` is a reserved source name
  (enforced in `_wire_source_commands`). Stay on typer's public API — do **not** subclass
  `TyperGroup` or import `typer._click`. Re-mounting a source is a harmless no-op (typer overwrites
  by name); `_wired_sources` just avoids rebuilding. Tests drive `app` directly (bundled sources are
  wired at import); the local-source test calls `_wire_source_commands()` explicitly.

  Do **not** re-add a hardcoded builtin dict for either kind.
- **Per-source isolation.** `gather()` fetches all sources concurrently
  (`asyncio.gather(..., return_exceptions=True)`) then reduces the results deterministically in
  `build_sources` order. Each source's `SourceError` (subclasses: `WeatherError`, `MtaError`) drops
  just that source's data (logged) and the render proceeds with whatever remains. Only `SourceError`
  is swallowed; any other exception propagates (fail loud), and two sources producing the same data
  type is a misconfiguration and fails loud. If *every* source is empty (`len(source_data) == 0`), `run_once()` skips the
  render entirely so it never spends a paid generation or clobbers the last good image.
- **Atomic writes.** Output is written to a `.tmp` sibling then `Path.replace`d, so a crash
  mid-write leaves the previous PNG intact. Keep this when touching the write path.
- **`package = false` / run via `-m`.** There is no install step and no console script on PATH.
  Always invoke `uv run python -m kindle_dash_gen`.
- **protobuf override.** `nyct-gtfs` hard-pins `protobuf==4.25.3`, which crashes on Python 3.14.
  `pyproject.toml` forces `protobuf>=6` via `[tool.uv] override-dependencies`. See the comment
  there and the upstream issue link before touching MTA deps.
- **Config is strict, and plugin config is validated per-plugin.** Every pydantic model sets
  `extra="forbid"`; an unknown TOML key is a validation error. Top-level `Config` does not define
  the source or layout schemas — it holds `sources: dict[str, dict[str, Any]]` (raw
  `[sources.<name>]` tables), and each `Dashboard` holds a raw `layout_config: dict[str, Any]`.
  After plugin discovery, `build_sources()` validates each source slice and `validate_layout()`
  each dashboard's `layout_config` against the respective plugin's own `Config` (each still
  `extra="forbid"`), so unknown/malformed keys fail fast there, not statically in `Config`. An
  unknown source *name* or layout *name* also fails fast. The CLI `_config()` runs both eagerly so a
  bad source or layout_config is caught before any fetch. Zero sources is valid (every render then
  legitimately skips). A `Dashboard` owns the **output spec** — `layout` (name), `output_path`,
  `width`, `height`, `gray_levels`, `post_process_method`, `rotate` — while the layout owns **how it
  draws**: render knobs like the font and display temperature units live in `layout_config` (the
  bundled `glanceable`'s `GlanceableConfig` has a **required** `title` header and a **required**
  `timezone` plus `font` and `weather_temp_units`), not on the
  dashboard. `timezone` is typed `ZoneInfo` — pydantic 2.9+ parses the IANA name natively and
  rejects an unknown zone at config load, so no custom validator. It is intentionally
  **default-less**: without it a layout would silently print UTC clock times.
- **`Secret` is the one way a plugin takes a credential.** Any plugin `Config` (source or layout)
  types a credential field as `Secret`, imported from its own toolkit (`sources/toolkit.py` or
  `render/toolkit.py`) — never from `config.py` directly. `Secret` takes **exactly one** of three
  mutually-exclusive inputs (enforced by a `model_validator`): `value_from_cmd` (a shell command's
  stdout), `value_from_env` (an env var name), or a literal — whose **TOML key is `value`** while the
  field is `value_from_value` (aliased). That asymmetry is deliberate: it keeps the three sources
  symmetrically named and frees `value` for the resolved accessor. Consumers read via the `value`
  `cached_property` (`config.api_key.value`), which strips whitespace uniformly. Non-obvious
  consequences:
  - **Reads are lazy** (at use time, not validation), so loading a config never shells out or
    requires the environment populated. A bad secret surfaces at fetch, not in `_config()`.
  - **A successful read is cached; a failed one is not**, so a transient failure retries — but a
    secret **rotated under a running process is not seen until it restarts**.
  - **`value_from_cmd` runs `subprocess.run(shell=True, timeout=10)`.** Keep the timeout:
    password-manager CLIs block on a passphrase prompt once their agent lock expires, and this app
    runs unattended inside `asyncio.gather`, so an unbounded call would hang the event loop forever.
  - The literal is a pydantic `SecretStr`, so it is masked in `repr` and in validation errors, and
    the resolved value stays out of `model_dump()`. Don't log a resolved `.value`.
- **Milestone-per-commit.** History is built as discrete milestones (data sources reworked into
  discovered plugins; the latest removed the LLM/OpenRouter backend entirely and made layouts own
  their config, mirroring the source plugin system), one feature/refactor per commit, Conventional
  Commits style.

## Context Files

- [Architecture & Data Flow](architecture.md)
- [Style Guide](style-guide.md)
- [Testing](context/testing.md)
