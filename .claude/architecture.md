# Architecture & Data Flow

The system is a single linear pipeline that runs once per interval. All orchestration lives in
`pipeline.py`; the CLI (`cli.py`) is a thin typer wrapper that also exposes each pipeline stage
as a standalone debug command.

The **fetch path and the pipeline runtime are async**; the **render path is deliberately synchronous
and sequential**. `gather()`, `run_once()`, and `run()` are coroutines and every source is fetched
concurrently, but `render`/`render_raw`/`post_process` (and the `Layout.render` protocol) stay plain
sync and dashboards render one after another. This split is intentional — do not make render async.
The CLI bridges the boundary with `asyncio.run(...)` at each command (`run`, the `source <name>`
fetch, and `dashboard render`'s gather); typer commands themselves stay plain sync `def`.

## Pipeline (pipeline.py)

```
run() loop  ──every interval_minutes──▶  run_once(cfg)
                                             │
                                    gather(cfg) ──▶ DashboardData
                                             │        (generated_at, source_data{type: value})
                       all sources empty? ──▶ skip render, return RunResult([], [])
                                             │
                            for each [dashboards.<name>] (shared data):
                                    render(cfg, data, dash):
                                       render_raw(cfg, data, dash) ──▶ raw Pillow Image
                                          └ layout.render(data, ...)   # build layout from layout_config, draw
                                       post_process(image, w, h, gray_levels, method) ──▶ PNG bytes
                                             │
                                    _atomic_write(dash.output_path, png)   # per-dashboard, isolated
```

`render_raw()` builds the dashboard's named `layout` from its `layout_config` and draws
`DashboardData` at the panel size, returning a raw Pillow `Image`. `post_process()` then grayscales,
fits, and quantizes it into Kindle-ready PNG bytes.

- **`gather()`** (async) stamps `now = datetime.now(UTC)` (aware — it becomes
  `DashboardData.generated_at` and is the `now` handed to every source), then iterates the
  discovered source plugins (`build_sources(cfg.sources)`
  resolves each `[sources.<name>]` to its plugin class + validated config), constructs and `fetch`es
  every source **concurrently** via `asyncio.gather(..., return_exceptions=True)`, then reduces the
  results **deterministically in `build_sources` order** (not completion order). A source that raises
  `SourceError` drops its data (logged) and the render proceeds; any other exception is *not*
  isolated and propagates (fail loud). It keys each non-`None` result by `type(result)` into
  `DashboardData.source_data`; a failed or empty source is simply absent. Two sources producing the
  same data type is a misconfiguration and raises (fail loud, not degrade).
- **`run_once()`** (async) gathers once, then renders every `[dashboards.<name>]` from that shared
  data,
  each to its own `output_path`. It short-circuits when every source is empty
  (`len(source_data) == 0`): writing a blank dashboard would clobber the last good images, so it
  returns an empty `RunResult` instead. A single dashboard's render/write failure is isolated
  (logged, others proceed) and its
  name collected in `RunResult.failed`, which the `run --one-shot` CLI turns into a non-zero exit.
- **`run()`** (async) wraps `run_once()` in a `while True` + `await asyncio.sleep`. Any unexpected exception
  (i.e. not an isolated per-source or per-dashboard error, both already swallowed) is logged via
  `log.exception` and retried next interval. `KeyboardInterrupt` exits cleanly.
- Logging is stdlib `logging` configured in the `run` CLI command (INFO, `%H:%M:%S`).

## Sources

Sources are **discovered plugins** (the source-side mirror of the render layouts). Each lives under
`sources/builtins/<name>/` as a three-file package — `__init__.py` (imports `source.py` so the
`register_source` call fires; discovery imports the subpackage, not its inner modules), `source.py`
(the `Source` class + its `Config` + client), and `model.py` (the data class the source produces,
which it owns) — or a local `plugins_path` dir. Each registers a `Source` protocol class via
`register_source(name, factory)` at import, and declares a pydantic `Config` class attribute for its
`[sources.<name>]` table. There is no shared cross-provider data model: each source owns its own
produced type, and a layout reconciles multiple providers in its own adapter.
`sources/registry.py` holds the empty registry, the `Source` protocol, and
`build_sources()`; `sources/toolkit.py` exposes `SourceError`, the base every source error
subclasses. `gather()` (above) drives them. See `docs/sources.md` for the contract. Below is what
each bundled source does internally.

### Datetimes: aware UTC across every boundary

The `now` a source receives is aware UTC and every timestamp it returns must be aware UTC, so
values from providers in different regions are directly comparable and a single `gather()` can feed
dashboards in different zones. The `Source` protocol docstring in `sources/registry.py` states the
convention, but the registry does **not** enforce it (no runtime check, no coercion) —
`docs/sources.md` ("Datetimes are aware UTC") is the enforcement mechanism. Display conversion is a
layout's job, not a source's.

Two things must still be computed in **local** time before the conversion, and both bundled weather
sources do so deliberately:

- **Calendar dates.** `today` is derived from the local `as_of` (`as_of_local.date()`) *before*
  `.astimezone(UTC)`. Past roughly 20:00 local the UTC date is already tomorrow, so anchoring on
  the UTC value would skew high/low by a day every evening.
- **Hourly bucket matching.** Provider hourly timestamps sit on *local* hour boundaries, so the
  current hour is matched locally and only then stored as UTC. Truncating a UTC instant to the hour
  misaligns in every zone whose offset is not a whole number of hours (India +05:30, Nepal +05:45,
  Chatham +12:45).

### NWS weather (sources/builtins/nws/)

The `nws` source (`NwsSource` + `NwsConfig`, in `source.py`) wraps `NwsClient` and produces
`NwsData` (in `model.py`); `WeatherError` subclasses `SourceError`. The NWS API is multi-step.
`NwsClient.fetch(lat, lon)` opens a `niquests.AsyncSession` (`async with`) and:

1. `GET /points/{lat},{lon}` (coords rounded to 4 dp — NWS rejects more) → returns per-location
   URLs: `forecast`, `forecastHourly`, `forecastGridData`, `observationStations`, plus a
   `relativeLocation` used for the display `location_name`. This must complete first (it yields the
   downstream URLs).
2. The **five downstream calls run concurrently** (`asyncio.gather`), since they are independent:
   - `GET` the hourly and daily forecast URLs with `?units=si`.
   - `GET` the gridpoint data → parse the `apparentTemperature` time series (windows of
     `(start, value)`), used to attach a `feels_like` to each `Temperature`.
   - `GET` the nearest observation station's latest observation → derive `raining` (keyword scan
     over `presentWeather`, falling back to the text description) and `observed_conditions`. This
     is enrichment only: failure returns `(None, None)`, never fails the report.
   - `GET /alerts/active?point={lat},{lon}` → every active alert for the point, parsed into
     `WeatherAlert` (`_parse_alert`) and carried unfiltered on `NwsData.alerts`. Enrichment only:
     a request failure degrades to `[]`, and a single malformed feature is skipped rather than
     dropping the valid siblings (per-item `try/except`). Only `event` is required per alert;
     other CAP fields default to `"Unknown"`/`""`/`None`.

**High/Low:** module-level `_day_high_low(daily_periods, day, apparent)` pairs a day's daytime
(high) and nighttime (low) periods and returns a `DailyHighLow`; `fetch` calls it twice to fill
`today` and `tomorrow`. Apparent high/low are the max/min feels-like across each period's window.
Two deliberate choices here:

- It matches the day **exactly**, never falling forward to the next available period. NWS drops a
  day's daytime period once it has passed, so from that evening today's high is genuinely unknown
  and reports `None`. The old fall-forward returned *tomorrow's* high labelled with today's date.
- "Today" is anchored on the *local* `as_of` date (read off `as_of_local` before the value is
  normalized to UTC — see the datetimes section above), **not** the first
  daily period's date. The first daily period only looks like today's because NWS truncates an
  in-progress period's `startTime` to roughly now. Anchoring on `as_of` is robust and matches how
  open-meteo anchors (`daily.time[0]` under `timezone=auto`), so the two providers agree on "today"
  for a layout that mixes them.

Every stored timestamp (`as_of`, each `hourly[].time`, and the alert
`effective`/`onset`/`expires`/`ends`) is normalized with `.astimezone(UTC)` — NWS supplies offsets,
so parsing is lossless. `_parse_alert_time` swallows the module-level `_ALERT_TIME_SWALLOW`
`(ValueError, TypeError)` tuple rather than a bare inline `except (…)`, matching how the module
names its other external-vocab literals.

All parsing failures raise `WeatherError`. Values stay SI at full precision.

### Open-Meteo weather + air quality (sources/builtins/open_meteo/)

The `open-meteo` source (`OpenMeteoSource` + `OpenMeteoConfig`, in `source.py`) wraps
`OpenMeteoClient` and produces `OpenMeteoData` (in `model.py`); `OpenMeteoError` subclasses
`SourceError`. Open-Meteo is keyless and global (no `user_agent`); `OpenMeteoConfig` is `latitude`,
`longitude`, `hourly_hours` (default 4). `OpenMeteoClient.fetch(lat,
lon)` opens a `niquests.AsyncSession` (`async with`) and hits **two independent endpoints
concurrently** via `asyncio.gather(..., return_exceptions=True)`:

1. `GET /v1/forecast` (`timezone=auto`, `wind_speed_unit=kmh`, `forecast_days=2` so both days'
   high/low are always available) → current conditions, the hourly strip, and daily hi/lo.
2. `GET` the air-quality API → `us_aqi` + particulates (`pm2_5`, `pm10`, `aerosol_optical_depth`).

`return_exceptions=True` lets both settle even when one fails (no orphaned request on a closing
session). A **forecast** failure is re-raised as `OpenMeteoError` and isolated by the pipeline; an
**air-quality** failure **degrades** — those fields become `None` while the rest of the report still
lands (best-effort enrichment).

Times come back **naive-local** (`timezone=auto`) and are made aware UTC via
`ZoneInfo(forecast["timezone"])` — the response's **named** zone, deliberately *not* its
`utc_offset_seconds`, since that offset is the zone's offset at request time and applying it
uniformly puts hours past a DST transition on the wrong instant. Two traps here, both recorded in
code comments and worth keeping:

- **`timezone=auto` must not become `timezone=UTC`**, tempting as that is for skipping the
  conversion. The parameter also sets the boundaries Open-Meteo aggregates `daily` over: under UTC
  a San Francisco high/low would be taken across a 17:00–17:00 local window (18.1 vs 20.8 on a
  sample day) and `daily.time[0]` would flip to tomorrow every afternoon.
- **`_hours` matches the current hour in local time** before converting, because the feed's
  timestamps sit on local hour boundaries (see the datetimes section above).

The current hour is excluded from the hourly strip (matching NWS) but its
precip probability surfaces as "this hour". A module-level `_day_high_low(daily, day)` fills the same
`today`/`tomorrow` pair as NWS, with a `_reading()` helper that degrades a `null` provider value to
`None` rather than building a `Temperature` whose `real` is `None`. The produced
`weather_code` is the **raw WMO integer** (not a description) — the model owns a `wmo_description`
helper for canonical text, but **icon selection is deliberately left to the layout**. All values stay
SI at full precision.

### MTA subway (sources/builtins/mta/)

The `mta` source (`MtaSource` + `MtaConfig`, in `source.py`) owns its config models — `Platform` and
`Station` live here, not in central config — and wraps its boards in an `MtaData` value (`.boards`,
in `model.py`); `MtaError` subclasses `SourceError`. `MtaClient(stations).fetch()` builds one
`StationBoard` per configured station name.

- Each MTA GTFS-realtime feed covers a group of lines (e.g. one feed for N/Q/R/W). Feed URLs
  come from `nyct_gtfs.NYCTFeed._train_to_url` (`_LINE_TO_URL`). The client collects the
  distinct feed URLs across all platforms of all stations and loads each **at most once** per
  fetch (`_load_feeds`), then reuses them. The distinct feeds are loaded **concurrently** via
  `asyncio.gather`.
- Per platform, it filters trips by `line_id`, `headed_for_stop_id` (the platform stop id with
  `N`/`S` suffixes per its `direction`), and `underway=True`, extracts the predicted arrival at
  the matching stop, and drops arrivals already in the past.
- A station merges all its platforms' arrivals, groups by `Direction`, and sorts each group by
  time (N-then-S order). No truncation here — boards carry every upcoming arrival; the layout
  decides how many to show (see the Render section).
- Feed load / parse failures raise `MtaError`. An unknown line id also raises `MtaError`.

**Arrival times (`_arrival_at`) round-trip through the host zone on purpose.** `nyct_gtfs` builds
its arrival with `datetime.fromtimestamp(epoch)`, i.e. a naive value in the *host's* local wall
clock. The source does nothing but `stop.arrival.astimezone(UTC)`, which recovers the exact
original instant on any host: `astimezone` interprets a naive value as host-local, the same zone
`fromtimestamp` rendered it in, so the two cancel. It is exact across DST fall-back as well, since
`fromtimestamp` sets `fold` on the ambiguous hour and `astimezone` honors it. This is deliberately
why **no nyct_gtfs internals are touched** — `TrainArrival.arrival` is aware UTC and a layout
converts it for display. `tests/test_mta.py` asserts this by actually moving the process zone.

`feed_loader` is an injectable async seam for tests (`Callable[[str], Awaitable[NYCTFeed]]`); the
default builds `NYCTFeed(url, fetch_immediately=False)` then `await feed.refresh_async()`.

## Render

A single renderer: a named **layout** draws `DashboardData` with Pillow and returns a raw `Image`;
`post_process()` then makes it Kindle-ready. There is no backend concept — a dashboard just names a
`layout` and supplies its `layout_config`.

### Layout (render/layout.py + plugins)

`render(data, *, width, height, layout, layout_config)` (in `render/layout.py`) builds the named
layout from its config and draws the dashboard deterministically with Pillow at the exact panel
size, returning a raw `"L"`-mode `Image` (the caller post-processes it). It first calls
`plugins.load_plugins()` to populate `_LAYOUTS` (a name → layout-class registry), then dispatches by
`layout` name. Unknown layout / unresolvable font / missing asset raise `LayoutError`. Free,
offline, exact, never garbles the data.

**Layouts are plugins that own their config — nothing is special-cased.** `_LAYOUTS` starts empty;
every layout (including bundled `glanceable`) registers itself via `register_layout(name, cls)` at
import time, and discovery imports them. A layout class implements the `Layout` protocol: a
`Config: ClassVar[type[BaseModel]]`, `__init__(config, *, width, height)`, and
`render(data) -> Image`. Concrete layouts subclass `Layout[TheirConfig]` (e.g.
`class _Glanceable(Layout[GlanceableConfig])`). `validate_layout(name, raw)` and
`build_layout(name, raw, *, width, height)` mirror `sources/registry.py`'s `build_sources`: they
validate a `[dashboards.<name>.layout_config]` table against the layout's own `Config`
(`extra="forbid"`), then construct it at the panel size. The bundled `glanceable`'s `GlanceableConfig`
declares `font: str | None`, `weather_temp_units: Literal["us","si","both"]`, and a **required,
default-less** `timezone: ZoneInfo`.

**A layout owns display-time conversion.** Everything it is handed is aware UTC, so a bare
`strftime` would print UTC. `glanceable` stores `self.tz = config.timezone` and applies
`.astimezone(self.tz)` at all three formatting sites: the title clock, the hourly strip labels, and
the subway board clock. `timezone` is typed `ZoneInfo` directly — pydantic 2.9+ parses the IANA
name from TOML and rejects an unknown zone at config load, so there is no custom validator. It has
no default on purpose: a default would silently render the wrong clock rather than fail. This is
also the piece that makes one process able to render a New York and a Bay Area dashboard from the
single shared `gather()`. `docs/plugins.md` states the contract for plugin authors.

- **Toolkit (`render/toolkit.py`)** is the public surface a plugin builds on: `Fonts` (fontconfig
  `fc-match` resolution → file + face index, verified against the requested family so a missing
  font fails fast), `INK`/`PAPER`, `fit_font` (shrink-to-fit), `load_asset_image(package, rel_path)`,
  the `format.py` display helpers, and `LayoutError`. `glanceable` uses only this — so any private
  plugin (or a 1:1 recreation of `glanceable`) can be built without core internals.
- **Discovery (`plugins.py`)** serves both plugin kinds by identical logic. It imports two bundled
  roots (always) — `kindle_dash_gen.render.builtins` for layouts and `kindle_dash_gen.sources.builtins`
  for sources, each behind its own idempotency flag — plus an optional local directory named by
  `Config.plugins_path` (imported by directory name after putting its parent on `sys.path`), which
  may hold either kind. `load_plugins(local_dir=None)` is idempotent; a missing bundled package is a
  silent no-op, but a present-but-broken plugin propagates. `pipeline` passes `cfg.plugins_path`;
  `layout.render()` and `build_sources()` load the bundled roots on their own for direct callers.
- **Bundled `glanceable`** lives at `render/builtins/glanceable/` — a self-contained plugin
  subpackage owning its `assets/icons/*.png` (pasted with alpha). It carries the concrete
  **multi-provider weather adapter**: a private `_weather(data)` **combines** whichever weather
  providers are present into a layout-local normalized draw surface (`_GlanceWeather`, `_Temp`,
  `_GlanceHour` — current temp, wind, a resolved icon, the hourly strip, plus `aqi` and `alerts`),
  the only surface the rest of the layout touches. The hero/hourly come from the **preferred**
  provider (`OpenMeteoData`, falling back to `NwsData`); **AQI is read off Open-Meteo and alerts off
  NWS independently**, so a dashboard configured with both shows the Open-Meteo hero *and* NWS alerts
  (each field is simply absent — `aqi=None` / `alerts=()` — when its provider is not configured, so
  the draw code never inspects a provider type). A dashboard with neither weather source renders no
  weather. Icon resolution lives in the adapter, per provider: NWS via the shared `weather_icon()`
  (keyword match on observed/forecast conditions), Open-Meteo via a local `_wmo_icon(code)`
  WMO-code→icon map (the source keeps the raw code, so the layout, not the source, owns the
  classification). The hero draws AQI (`format_aqi`, EPA breakpoints) and, most-severe-first
  (`_SEVERITY_RANK`), the top active alert with a `+N more` tail. Both go through one `_metric_row`
  (shared with wind): an alert — or an AQI of "Unhealthy" or worse, per `aqi_is_unhealthy` — is set
  bold behind the bundled `assets/icons/warning.png`. The icon is sized and centered off the row
  font's **cap band** (`_cap_height`/`_cap_midline`), not Pillow's `lm` anchor, which centers the em
  box (ascent + descent) and so leaves a small-descent face's ink well below the anchor.
  This is the concrete realization of the "a layout reconciles multiple providers in its own local
  adapter" principle. See `docs/plugins.md` for the full contract.

### Post-process (render/postprocess.py)

`post_process(image, *, width, height, gray_levels, method, rotate)` takes the layout's raw Pillow
`Image` (no PNG round-trip) and, in order:

1. `convert("L")` → grayscale.
2. `_fit` to exactly `(width, height)` by `method`:
   - `resize` — stretch to fill, ignoring aspect (minor distortion).
   - `crop` — `ImageOps.fit` scale-to-cover + center-crop.
   - `pad` — `ImageOps.pad` fit + white (255) e-ink bars.
3. `_quantize_lut` — a 256-entry LUT snapping each value to the nearest of `gray_levels`
   evenly-spaced grays (models the Voyage's fixed hardware palette; requires `levels >= 2`).
4. Optionally `ROTATE_90` (for a physically rotated device).

Returns PNG bytes. Defaults target the Kindle Voyage: 1072×1448 portrait, 16 gray levels. Since the
layout already draws at exact size, the fit step is effectively a no-op and only quantization
changes the pixels.

## Configuration (config.py)

`load_config(path)` reads TOML via `tomllib` and validates into `Config`. Every model uses
`extra="forbid"`. Notable pieces:

- `sources: dict[str, dict[str, Any]]` — the raw `[sources.<name>]` tables. `Config` does **not**
  validate their contents; after plugin discovery, `build_sources()` validates each slice against
  its plugin's own `Config` model (the `nws` plugin's `NwsConfig`: `latitude`, `longitude`,
  `user_agent`, `hourly_hours`; the keyless `open-meteo` plugin's `OpenMeteoConfig`:
  `latitude`, `longitude`, `hourly_hours` — no `user_agent`; the `mta` plugin's
  `MtaConfig`: `stations`, whose `Station`/`Platform` models also live in that plugin). An unknown
  source name or key fails fast
  there. Zero sources is valid. The old top-level `[location]`, `[weather]`, `[stations.*]` sections
  are gone.
- `Dashboard` — the **output spec** only: `layout` (name, default `"glanceable"`), `output_path`,
  pixel `width`/`height`, `gray_levels`, `post_process_method`, `rotate`, plus a raw
  `layout_config: dict[str, Any]`. `Config` does **not** validate `layout_config`; `validate_layout`
  checks it against the named layout's own `Config` after discovery. Render knobs (`font`,
  `weather_temp_units`, `timezone`) live in `layout_config`, not on the dashboard. `timezone` is
  **required** in every `glanceable` dashboard's `layout_config` — a breaking change for existing
  configs, covered by README's "Upgrading an existing config" alongside the removal of
  `rollover_hour`.
- `plugins_path` — optional absolute dir of private layout and/or source plugins.
- `Schedule.interval_minutes` (default 5).

## Design Decisions

- **Presentation stays out of the domain models** — `DashboardData` and the source models
  carry no formatting; that lives in `format.py` and the layouts.
- **Sources report; layouts decide** — a source reports every value the provider gives, neutrally,
  and reports `None` when a value is genuinely unknown. Which of them to *show* is a layout's call.
  This is why the weather sources return both `today` and `tomorrow` rather than a single
  "current" high/low, why open-meteo keeps the raw WMO code instead of an icon name, and why mta
  boards are uncapped. See the "Report data, not display decisions" section of `docs/sources.md`.
- **Aware UTC internally, local at display** — the same shape as "SI internally, round at display".
  Sources normalize to aware UTC; only a layout converts to a display zone. The forcing case is
  `nyct_gtfs` returning host-local wall clock: with naive datetimes, one process could not
  correctly render a New York and a Bay Area dashboard, and `gather()` deliberately does one fetch
  for all of them.
- **One source of truth for formatting** — display formatting lives only in `format.py`
  (re-exported through `render/toolkit.py`) and is applied by the layouts. The `source run` debug
  command deliberately prints the *raw* produced data (SI, unformatted) via rich, so it shows
  exactly what a source hands the renderer.
- **Fail fast on explicit config, degrade gracefully on source outages** — a bad `layout_config`
  or source key errors immediately, but a dead weather API just drops a panel.
