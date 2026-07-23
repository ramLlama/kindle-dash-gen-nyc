# kindle-dash-gen

## What This Project Does

A Python CLI that periodically generates Kindle e-ink dashboard images. It pulls weather (NWS,
Open-Meteo) and real-time transit arrivals (MTA subway, SF Bay Area 511), renders each dashboard
with a deterministic local **pillow** layout, post-processes the resulting PNG for a Kindle Voyage
(grayscale, exact pixel dimensions, 16 hardware gray levels), and writes it to a configured path
for syncing to the device. Intended to run unattended on an interval (e.g. every 5 minutes). One
run can serve dashboards for **different regions** — a single shared fetch feeds every configured
dashboard, and each layout converts the aware-UTC data to its own display timezone.

## Tech Stack

- **Python 3.14+** (uses `tomllib`, `StrEnum`, `X | None` unions everywhere)
- **uv** for env/deps. The project is `package = false` — run in place, never installed.
- **typer** `0.26.*` — CLI framework
- **pydantic** `2.*` — config validation (`extra="forbid"` on every model)
- **niquests** `3.*` — HTTP client (NWS, Open-Meteo, 511; uses `AsyncSession` for concurrent
  fetches); `niquests-mock` in tests
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
    toolkit.py         # public plugin API: SourceError (base all source errors subclass), Secret,
                       #   source_config(ctx, name, Config) for a source's own cli() verbs
    registry.py        # Source protocol, register_source, build_sources() dispatch
    builtins/          # bundled source plugins (discovered, not special-cased)
      nws/             # "nws" source (three-file package, multi-location):
        __init__.py    #   imports source.py -> register_source("nws", NwsSource)
        source.py      #   NwsSource + NwsConfig (+ Location) + NwsClient
        model.py       #   NwsData (wraps locations{name: LocationWeather}; + Temperature, HourlyForecast)
      open_meteo/      # "open-meteo" source (three-file package, keyless + global, multi-location):
        __init__.py    #   imports source.py -> register_source("open-meteo", OpenMeteoSource)
        source.py      #   OpenMeteoSource + OpenMeteoConfig (+ Location) + OpenMeteoClient (async, forecast + AQI)
        model.py       #   OpenMeteoData (wraps locations{name: LocationWeather}; + Temperature, wmo_description)
      mta/             # "mta" source (three-file package, owns its assets):
        __init__.py    #   imports source.py -> register_source("mta", MtaSource)
        source.py      #   MtaSource (+ cli() verb `list-stations`) + MtaConfig (Platform/Station) + MtaClient
        model.py       #   MtaData (+ Direction, StationBoard, TrainArrival) — the produced data class
        assets/stations.csv  #   bundled station lookup (for `source mta list-stations`)
      sf_bay_511/      # "sf-bay-511" source (three-file package, keyed — first Secret consumer):
        __init__.py    #   imports source.py -> register_source("sf-bay-511", SfBay511Source)
        source.py      #   SfBay511Source (+ cli() verbs `list-stops`, `agencies`) + SfBay511Config
                       #     (Board/StopRequest) + SfBay511Client (async SIRI StopMonitoring)
        model.py       #   SfBay511Data (+ Agency, per-agency Direction enums, StopBoard,
                       #     TransitArrival) — the produced data class
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
- **Direction** (mta) is a `StrEnum` with values `"N"`/`"S"` (GTFS uptown/downtown, nominal for
  the L). The `sf-bay-511` source has its own, unrelated per-agency direction enums (below).
- **SfBay511Data** (`sources/builtins/sf_bay_511/model.py`) wraps `list[StopBoard]` (as `.boards`),
  the same single-typed-value shape as `MtaData`. A **Board** (config) merges one or more
  **StopRequest**s — an `Agency` + that operator's `stopcode`, plus an optional `lines` allowlist —
  into one display board; boards are **uncapped**, the layout decides how many arrivals to show.
  Direction is typed **per agency**: an `Agency` StrEnum (`BART`=BA, `MUNI`=SF, `CALTRAIN`=CT,
  `AC_TRANSIT`=AC) and a separate enum per operator — `BartDirection`/`CaltrainDirection` (N/S),
  `MuniDirection` (IB/OB **plus** N/S, since Muni's feed emits both), `AcTransitDirection`
  (N/S/E/W) — unioned as `Direction` and disambiguated by each arrival's `agency` field. See the
  gotcha below: these enums are **not disjoint by value**.
- **Both weather sources are multi-location, keyed by name** — the same wrapper shape `MtaData`
  established. `NwsConfig`/`OpenMeteoConfig` each take `locations: dict[str, Location]` (each source
  owns its own tiny `Location` = just `latitude`/`longitude`; `user_agent`/`hourly_hours` stay
  source-level), and `NwsData`/`OpenMeteoData` are thin wrappers holding `locations:
  dict[str, LocationWeather]` — the per-location weather class each source owns (formerly the flat
  `NwsData`/`OpenMeteoData` body). The client fans out over the locations concurrently. A layout
  looks a location up by name (`nws.locations["NYC"]`), and the **name is the join key across
  providers**: the same "NYC" pairs Open-Meteo's forecast with NWS's alerts.
- **NwsData** (`sources/builtins/nws/model.py`) wraps `locations: dict[str, LocationWeather]`. Each
  `LocationWeather` carries current conditions, `today` and `tomorrow`
  (each a `DailyHighLow(day, high, low)` — both days always present, readings `None` when unknown;
  each source owns its own copy of this class, like every other type), upcoming hours, and active
  weather **alerts** (`list[WeatherAlert]`, defaults to `[]`).
  `Temperature` bundles a `real` value with an optional `feels_like` (apparent). `WeatherAlert`
  mirrors the CAP alert fields NWS supplies (`event`, `category`, `severity`, `certainty`,
  `urgency`, `status`, `message_type`, `area_desc`, `sender_name`, `headline`, `description`,
  `instruction`, `response`, and the `effective`/`onset`/`expires`/`ends` timestamps); alerts are
  carried unfiltered (no severity knob) and are unused by any layout until a layout draws them.
- **OpenMeteoData** (`sources/builtins/open_meteo/model.py`) wraps `locations:
  dict[str, LocationWeather]` too, its `LocationWeather` a provider-owned peer to NWS's
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
concretely: the bundled `glanceable` layout has a private `_weather(data, location)` adapter that
**combines** whichever weather providers cover the **selected `weather_location`** into one
layout-local draw surface — hero/hourly from the preferred provider (Open-Meteo, NWS fallback),
**AQI off Open-Meteo and alerts off NWS independently** (each absent when its provider isn't
configured), so `render()` never inspects a provider type. The location name is the **join key
across providers** (the same "NYC" pairs Open-Meteo's forecast with NWS's alerts); a name no source
produced this run renders no weather. The hero draws the AQI badge (`format_aqi`) and the most-severe active alert
(`+N more` tail) through one shared `_metric_row`, which flags an alert — or an "Unhealthy"-or-worse
AQI (`aqi_is_unhealthy`, EPA 151+) — in bold behind the bundled `warning.png` icon. A peer
`_transit` adapter does the same for the transit band, combining MTA and 511 boards into one
normalized draw surface (MTA columns first) so the draw code never inspects a provider type.

The transit panel has the **same shape as the weather adapter**: a private
`_transit(data, transit_boards)` combines whichever transit providers are present into a
layout-local normalized draw surface (`_GlanceBoard` / `_GlanceGroup` / `_GlanceArrival`) via
per-provider adapters (`_from_mta`, `_from_sf_bay_511`), filtering to the `transit_boards` allowlist
(by canonical name, in source order) first, so `glanceable` draws **both MTA subway and SF Bay 511
boards** and the draw methods reference no provider type. Together the `_weather` and `_transit` adapters realize the same
principle: "a layout reconciles multiple providers in its own local adapter."

See [architecture.md](architecture.md) for data flow, the NWS multi-step fetch, the Open-Meteo
concurrent forecast+AQI fetch, MTA feed deduplication, the 511 stop fan-out, and the
layout/post-process details.

## Development Workflow

```sh
uv sync
cp config.example.toml config.toml     # edit; config.toml is gitignored

# Run in place (NOT installed — always via -m):
uv run python -m kindle_dash_gen --help
uv run python -m kindle_dash_gen --config config.toml dashboard render out.png  # render only
uv run python -m kindle_dash_gen --config config.toml run --one-shot            # one iteration
uv run python -m kindle_dash_gen --config config.toml run                       # loop

# Verification gates (all must pass; pre-commit runs the same three):
uv run pytest
uv run ruff check .
uv run mypy
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
- **The 511 per-agency direction enums are NOT disjoint by value.** `BartDirection.NORTH` and
  `MuniDirection.NORTH` are both the string `"N"` and, being `StrEnum`s, compare **and hash** equal.
  Only the enum *type* distinguishes them. Three consequences, each of which looks like needless
  ceremony until you know this:
  - `_check_direction` is **`isinstance`-based**, not an equality/membership check.
  - `StopBoard.arrivals` nests **agency → direction → arrivals**. A flat direction-keyed dict would
    silently collide two operators' northbound arrivals into one bucket.
  - `BartDirection` and `CaltrainDirection` are **kept distinct despite being identical** (both
    N/S). Collapsing them into an alias would defeat the per-agency type check. Do not "simplify"
    this — there is a test pinning it.
- **`Agency.label` and `direction_enum(agency)` use `match`, not a dict**, deliberately: `match`
  over enum members gives *static* exhaustiveness, so adding an `Agency` member without a label or
  a direction vocabulary is a mypy "Missing return statement" at check time rather than a
  `KeyError` once that agency is first configured. Verified empirically; this replaced a runtime
  "every agency has an entry" test. See [style-guide.md](style-guide.md).
- **511's `ParentStation` ids are NOT `StopMonitoring` stopcodes.** A stopcode is one *platform*,
  which for rail means one *direction*, so a two-direction board must merge the platform stopcodes
  (Embarcadero = 901161 southbound + 901162 northbound). Querying the parent id (901169) instead
  returns a grab-bag spanning 16 different stations. Multi-stop board merging is therefore the
  **normal case for rail**, not an edge case — don't "simplify" a board down to one parent id.
  `source sf-bay-511 list-stops` dumps code / platform / parent / name live so the right codes can
  be grepped out.
- **511 specifics that must not regress.** (a) **HTTPS**, not the `http://` URLs 511's own docs
  print — the API key travels as a **query parameter**, so plaintext would leak it every polling
  interval (caught in review). (b) The fetch is **all-or-nothing**: any request failing fails the
  whole source, because a half-populated board reads as "nothing more is coming" rather than "we
  don't know", and the pipeline already degrades a missing source gracefully. (c) Requests are
  **deduped per distinct `(agency, stopcode)`** — the default rate limit is 60/hour/key, about four
  stops at the 5-minute interval. (d) Bodies are decoded `utf-8-sig`: 511 prefixes its JSON with a
  BOM that a plain UTF-8 decode carries into the first key. (e) A shared `_as_list` normalizes
  SIRI-JSON's object-or-array collapse for **both** the delivery and the visit list (a stop with
  exactly one train due is the case that would otherwise iterate a dict's string keys).
  (f) Over half a live BART station's visits have `LineRef` **and** `DirectionRef` null (scheduled
  trips with no vehicle assigned) and are skipped; a *half*-null pair **raises**, since a frozen
  dataclass does no runtime type checking and `line=None` would otherwise reach a layout.
  Direction casing and surrounding whitespace are tolerated.
- **One fetch, many dashboards — the layout selects its slice.** Config has
  `dashboards: dict[str, Dashboard]` (named `[dashboards.<name>]` tables). `gather()` runs once and
  every dashboard renders from that **one shared** `source_data`. Sources now hold **multiple**
  locations/stations (weather `locations`, transit `stations`/`boards`), so per-dashboard selection
  is a **layout** concern, done by name, not routing at the source level. `glanceable` has two
  `layout_config` selectors, both keyed on the **canonical name** (config key / `board.name`), not
  the display label:
  - `weather_location: str` — **required, no default**. Names which location's weather to draw; the
    adapter looks it up in each provider's `locations` dict. Because it's required, a transit-only
    glanceable dashboard isn't possible (a deliberate choice).
  - `transit_boards: list[str] | None = None` — an allowlist of board names. `None` draws every
    board a source produced; a list keeps only those, in **source order** (MTA first), *not* the
    list's order (it's a filter, not a reorder). The 3-board cap applies **after** the filter, so a
    config with more stations than fit is fine as long as each dashboard selects a drawable few.

  So sibling dashboards fed by one fetch each render a different city and different stations. This
  answers the original "multiple weather results" question at the **data layer**: the model now
  supports many weather locations keyed by name (like stations); `glanceable` renders exactly one
  (the selected `weather_location`); a future layout could render several. Open-Meteo is still
  preferred over NWS **for a given location** when both cover it (correct — that's provider
  reconciliation, not a lost city). The bundled sources/layout are sample templates to fork.
- **Weather degrades per-location; transit is all-or-nothing.** Both weather sources fetch their
  `locations` concurrently and drop just the ones that fail (logged), raising their `WeatherError`
  only if **every** location fails; a dashboard drawing a dropped city renders no hero. This
  deliberately differs from the transit sources' all-or-nothing rule (a request failing fails the
  whole source): a missing city just shows no weather, whereas a half-populated arrival board reads
  as "nothing more is coming" rather than "we don't know". The per-location fan-out uses
  `return_exceptions=True`, swallows the source's *own* error per-location, and **re-raises any
  other exception** (fail-loud preserved). The reasoning lives in the two `fetch` docstrings.
- **Transit boards are capped at three columns.** `glanceable` draws at most
  `_MAX_TRANSIT_BOARDS = 3` transit boards, counted **across all providers combined** (two MTA + two
  511 is four, and fails). `_transit_boards` raises `LayoutError` on a fourth, because past three the
  clocks and route badges collide. This is a **render-time** check, not config-load: a station only
  becomes a board once its source is fetched, so the count isn't known statically. The pipeline
  isolates the failure to the offending dashboard (logged, skipped, its last image preserved).
  Documented in `config.example.toml`.
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
    `kindle_dash_gen.sources.builtins` (the `nws`, `open-meteo`, `mta`, and `sf-bay-511` sources).
    A source is a `Source`
    protocol class with a `Config` and an **async** `fetch(now)` (`async def fetch(self, now) -> Any`)
    so the pipeline can fetch every source concurrently — `await` I/O inside it (e.g.
    `niquests.AsyncSession`); `build_sources` validates each `[sources.<name>]` table. A source may also define an optional `cli(cls) -> typer.Typer` for
    source-specific CLI verbs (`source mta list-stations`; `source sf-bay-511 list-stops` /
    `agencies`). A `cli()` verb reads its **own** config by declaring `ctx: typer.Context` and
    calling `source_config(ctx, name, ConfigCls)` rather than re-taking settings as flags — that is
    how `list-stops` gets its API key, so a credential lives in exactly one place and any `Secret`
    form keeps working. Only that source's slice is validated, so inspecting one source never fails
    because an unrelated one is misconfigured. Build on
    `sources/toolkit.py` (`SourceError`, `Secret`, `source_config` — note it imports `typer`).
    See `docs/sources.md`.
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
- **Per-source isolation, including construction.** `gather()` fetches all sources concurrently
  (`asyncio.gather(..., return_exceptions=True)`) then reduces the results deterministically in
  `build_sources` order. Each source is **constructed inside** its isolated coroutine
  (`build_and_fetch`), not in the `gather` argument list — building there runs every `__init__`
  eagerly while the generator is unpacked, *outside* the isolation `return_exceptions` provides, so
  one source raising in `__init__` (reading a credential, say) killed the whole run and stranded
  its siblings' coroutines un-awaited. Verified empirically; keep the construction inside.
  Correspondingly, a source that needs a credential resolves its `Secret` in `fetch()`, not
  `__init__`, and wraps a read failure in its own `SourceError` subclass (see `SfBay511Source`).
  Each source's `SourceError` (subclasses: `WeatherError`, `OpenMeteoError`, `MtaError`,
  `SfBay511Error`) drops
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
  bundled `glanceable`'s `GlanceableConfig` has a **required** `title` header, a **required**
  `timezone`, and a **required** `weather_location` — plus optional `transit_boards`, `font`, and
  `weather_temp_units`), not on the
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
