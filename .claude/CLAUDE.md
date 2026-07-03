# kindle-dash-gen-nyc

## What This Project Does

A Python CLI that periodically generates a Kindle e-ink dashboard image for NYC. It pulls the
local NWS weather forecast plus real-time MTA subway arrivals, renders the whole dashboard via one
of two backends (a deterministic local **pillow** layout, the default; or an **llm** OpenRouter
image model), post-processes the resulting PNG for a Kindle Voyage (grayscale, exact pixel
dimensions, 16 hardware gray levels), and writes it to a configured path for syncing to the
device. Intended to run unattended on an interval (e.g. every 5 minutes).

## Tech Stack

- **Python 3.14+** (uses `tomllib`, `StrEnum`, `X | None` unions everywhere)
- **uv** for env/deps. The project is `package = false` — run in place, never installed.
- **typer** `0.26.*` — CLI framework
- **pydantic** `2.*` — config validation (`extra="forbid"` on every model)
- **niquests** `3.*` — HTTP client (NWS + OpenRouter); `niquests-mock` in tests
- **nyct-gtfs** `2.*` — MTA GTFS-realtime feed parsing
- **jinja2** `3.*` — prompt templating (llm backend)
- **pillow** `12.*` — image post-processing and the pillow rendering backend
- **fontconfig** (`fc-match`, system tool) — the pillow backend resolves a font family name to a
  file; required at runtime when `backend = "pillow"`
- **pytest** `9.*`, **ruff** `0.15.*` — test + lint gates

## Repository Structure

```
kindle_dash_gen_nyc/
  __main__.py          # `python -m kindle_dash_gen_nyc` entry -> cli.run()
  cli.py               # typer app: version, run, weather, mta group, dashboard group
  config.py            # TOML -> pydantic Config; Secret (value | value_from_cmd)
  pipeline.py          # gather -> build_prompt -> render -> post_process -> atomic write
  format.py            # display formatters (temp/reading/apparent/wind/eta); SI -> display
  models/              # frozen dataclasses (domain models, no presentation)
    weather.py         # Temperature, HourlyForecast, WeatherReport
    mta.py             # Direction (StrEnum), TrainArrival, StationBoard
    dashboard.py       # DashboardData (aggregate of all sources)
  sources/             # external data fetchers, each raising its own *Error
    weather.py         # NwsClient -> WeatherReport
    mta.py             # MtaClient -> list[StationBoard]
  render/              # turn data into a Kindle-ready PNG (two backends)
    prompt.py          # llm backend: render_prompt() Jinja2, public template context contract
    openrouter.py      # llm backend: OpenRouterClient, Unified Image API, capability discovery
    layout.py          # pillow backend: named layouts draw DashboardData directly (fontconfig)
    postprocess.py     # post_process(): grayscale, fit, quantize (Pillow) — shared by both
  assets/
    dashboard_prompts/*.j2       # bundled prompt templates ("dense", "glanceable") — llm backend
    icons/{sunny,cloudy,rain,snow}.png  # weather icons for the pillow backend
    mta/stations.csv             # bundled station lookup (for `mta list-stations`)
tests/                 # pytest, one file per module; HTTP mocked with niquests-mock
config.example.toml    # copy to config.toml (gitignored) and edit
```

## Key Concepts & Domain Model

- **DashboardData** (`models/dashboard.py`) is the aggregate handed to the renderer: an optional
  `WeatherReport`, a list of `StationBoard`, and `generated_at` (also used as "now" for ETAs).
- **Station vs Platform** (`config.py`): a config **Station** is a display board keyed by name;
  it merges one or more **Platform** entries (each a GTFS base stop id + the lines serving it),
  and caps arrivals per direction via `max_arrivals`. Example: "Union Sq" merges the N/Q/R/W,
  4/5/6, and L platforms into one board.
- **Direction** is a `StrEnum` with values `"N"`/`"S"` (GTFS uptown/downtown, nominal for the L).
- **WeatherReport** carries current conditions, today/tomorrow high-low, and upcoming hours.
  `Temperature` bundles a `real` value with an optional `feels_like` (apparent).

## Architecture Overview

Linear pipeline, wired in `pipeline.py`:
`gather()` (fetch weather + subway, isolating each source) → `render_raw()` (dispatch on
`dashboard.backend`) → `post_process()` (grayscale, fit, quantize) → atomic write to
`dashboard.path`. `render_raw()` branches: the **pillow** backend calls `layout.render()` (draws
`DashboardData` at native size); the **llm** backend does `build_prompt()` →
`OpenRouterClient.generate()`. Both return raw PNG bytes, and `post_process()` is shared (for
pillow the fit step is a no-op since it's already exact-sized, so only quantization applies). The
`dashboard` CLI subcommands expose each step in isolation for debugging.

See [architecture.md](architecture.md) for data flow, the NWS multi-step fetch, MTA feed
deduplication, and the OpenRouter capability-discovery details.

## Development Workflow

```sh
uv sync
cp config.example.toml config.toml     # edit; config.toml is gitignored

# Run in place (NOT installed — always via -m):
uv run python -m kindle_dash_gen_nyc --help
uv run python -m kindle_dash_gen_nyc --config config.toml dashboard preview-prompt  # no API spend
uv run python -m kindle_dash_gen_nyc --config config.toml run --one-shot            # one iteration
uv run python -m kindle_dash_gen_nyc --config config.toml run                       # loop

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
- **Secrets never come from environment variables.** The OpenRouter API key is a `Secret`:
  either an inline `{ value = "..." }` or `{ value_from_cmd = "..." }` whose stdout is the key.
  This is a deliberate design choice, not an oversight — do not add env-var fallbacks.
- **Two render backends.** `dashboard.backend` selects `"pillow"` (default: deterministic local
  layout in `render/layout.py`; free, offline, exact — never garbles data) or `"llm"` (OpenRouter
  image model). `[openrouter]` is optional and only required for the llm backend (a `Config`
  validator enforces this). The pillow backend resolves its `font` family via fontconfig
  (`fc-match`) and pastes bundled `assets/icons/*.png`; a missing font/icon raises `LayoutError`.
- **Per-source isolation.** In `gather()`, a `WeatherError` drops the weather panel and an
  `MtaError` drops the arrival boards; the render proceeds with whatever remains. Only these
  typed errors are swallowed. If *both* sources are empty, `run_once()` skips the render
  entirely (returns `None`) so it never spends a paid generation or clobbers the last good image.
- **Atomic writes.** Output is written to a `.tmp` sibling then `Path.replace`d, so a crash
  mid-write leaves the previous PNG intact. Keep this when touching the write path.
- **`package = false` / run via `-m`.** There is no install step and no console script on PATH.
  Always invoke `uv run python -m kindle_dash_gen_nyc`.
- **protobuf override.** `nyct-gtfs` hard-pins `protobuf==4.25.3`, which crashes on Python 3.14.
  `pyproject.toml` forces `protobuf>=6` via `[tool.uv] override-dependencies`. See the comment
  there and the upstream issue link before touching MTA deps.
- **OpenRouter capabilities are discovered at runtime**, not hardcoded — aspect ratios and
  resolutions are queried per model from its `/endpoints` listing, unioned across endpoints. An
  unsupported `aspect_ratio`/`resolution` override fails fast with the valid values listed.
- **Config is strict.** Every pydantic model sets `extra="forbid"`; an unknown TOML key is a
  validation error, not silently ignored.
- **Milestone-per-commit.** History is built as discrete milestones (M1..M6 so far; M6 = the
  pillow rendering backend), one feature/refactor per commit, Conventional Commits style.

## Context Files

- [Architecture & Data Flow](architecture.md)
- [Style Guide](style-guide.md)
- [Testing](context/testing.md)
