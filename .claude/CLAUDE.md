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
- **niquests** `3.*` — HTTP client (NWS); `niquests-mock` in tests
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
  config.py            # TOML -> pydantic Config; Dashboard (output spec + layout_config)
  pipeline.py          # gather -> layout.render -> post_process -> atomic write
  format.py            # display formatters (temp/reading/apparent/wind/eta); SI -> display
  models/              # frozen dataclasses (domain models, no presentation)
    dashboard_data.py  # DashboardData (source_data keyed by produced type) — the only model here
  sources/             # data-source plugins (source-side mirror of render/)
    toolkit.py         # public plugin API: SourceError (base all source errors subclass)
    registry.py        # Source protocol, register_source, build_sources() dispatch
    builtins/          # bundled source plugins (discovered, not special-cased)
      nws/             # "nws" source (three-file package):
        __init__.py    #   imports source.py -> register_source("nws", NwsSource)
        source.py      #   NwsSource + NwsConfig + NwsClient
        model.py       #   NwsData (+ Temperature, HourlyForecast) — the produced data class
      mta/             # "mta" source (three-file package, owns its assets):
        __init__.py    #   imports source.py -> register_source("mta", MtaSource)
        source.py      #   MtaSource (+ cli() verb `list-stations`) + MtaConfig (Platform/Station) + MtaClient
        model.py       #   MtaData (+ Direction, StationBoard, TrainArrival) — the produced data class
        assets/stations.csv  #   bundled station lookup (for `source mta list-stations`)
  render/              # turn data into a Kindle-ready PNG (pillow layout + post-process)
    layout.py          # Layout protocol (owns its Config), register/validate/build_layout, render()
    toolkit.py         # layout public plugin API (Fonts, INK/PAPER, fit_font, assets, format helpers)
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
  aggregate handed to the renderer: `generated_at` (also used as "now" for ETAs) plus
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
- **NwsData** (`sources/builtins/nws/model.py`) carries current conditions, today/tomorrow
  high-low, and upcoming hours. `Temperature` bundles a `real` value with an optional `feels_like`
  (apparent).

## Architecture Overview

Linear pipeline, wired in `pipeline.py`:
`gather()` (iterate the discovered source plugins **once**, isolating each) → for each configured
`[dashboards.<name>]`: `render_raw()` (`layout.render()` builds the dashboard's layout from its
`layout_config` and draws `DashboardData` at native size, returning a raw Pillow `Image`) →
`post_process()` (grayscale, fit, quantize — takes the `Image`, returns PNG bytes) → atomic write
to the dashboard's `output_path`. `run_once()` returns a `RunResult(written, failed)`; one
dashboard's render failure is isolated (logged, others proceed). The fit step is effectively a
no-op since the layout already draws at exact size, so only quantization matters. The `dashboard`
CLI subcommands expose each step in isolation for debugging.

See [architecture.md](architecture.md) for data flow, the NWS multi-step fetch, MTA feed
deduplication, and the layout/post-process details.

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
    `load_asset_image`, `LayoutError`). See `docs/plugins.md`.
  - **Sources** register via `register_source` at import, bundled root
    `kindle_dash_gen.sources.builtins` (the `nws` and `mta` sources). A source is a `Source`
    protocol class with a `Config` and a `fetch(now)`; `build_sources` validates each
    `[sources.<name>]` table. A source may also define an optional `cli(cls) -> typer.Typer` for
    source-specific CLI verbs (the `mta` source ships `source mta list-stations`). Build on
    `sources/toolkit.py` (`SourceError`). See `docs/sources.md`.
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
- **Per-source isolation.** In `gather()`, each source's `SourceError` (subclasses: `WeatherError`,
  `MtaError`) drops just that source's data (logged) and the render proceeds with whatever remains.
  Only `SourceError` is swallowed; two sources producing the same data type is a misconfiguration
  and fails loud. If *every* source is empty (`len(source_data) == 0`), `run_once()` skips the
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
  bundled `glanceable`'s `GlanceableConfig` has `font` and `weather_temp_units`), not on the
  dashboard.
- **Milestone-per-commit.** History is built as discrete milestones (data sources reworked into
  discovered plugins; the latest removed the LLM/OpenRouter backend entirely and made layouts own
  their config, mirroring the source plugin system), one feature/refactor per commit, Conventional
  Commits style.

## Context Files

- [Architecture & Data Flow](architecture.md)
- [Style Guide](style-guide.md)
- [Testing](context/testing.md)
