# Architecture & Data Flow

The system is a single linear pipeline that runs once per interval. All orchestration lives in
`pipeline.py`; the CLI (`cli.py`) is a thin typer wrapper that also exposes each pipeline stage
as a standalone debug command.

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

- **`gather()`** iterates the discovered source plugins (`build_sources(cfg.sources)` resolves each
  `[sources.<name>]` to its plugin class + validated config), constructs and `fetch`es each inside
  its own try/except, and logs a degradation on `SourceError`. It keys each non-`None` result by
  `type(result)` into `DashboardData.source_data`; a failed or empty source is simply absent. Two
  sources producing the same data type is a misconfiguration and raises (fail loud, not degrade).
- **`run_once()`** gathers once, then renders every `[dashboards.<name>]` from that shared data,
  each to its own `output_path`. It short-circuits when every source is empty
  (`len(source_data) == 0`): writing a blank dashboard would clobber the last good images, so it
  returns an empty `RunResult` instead. A single dashboard's render/write failure is isolated
  (logged, others proceed) and its
  name collected in `RunResult.failed`, which the `run --one-shot` CLI turns into a non-zero exit.
- **`run()`** wraps `run_once()` in a `while True` + `time.sleep`. Any unexpected exception
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

### NWS weather (sources/builtins/nws/)

The `nws` source (`NwsSource` + `NwsConfig`, in `source.py`) wraps `NwsClient` and produces
`NwsData` (in `model.py`); `WeatherError` subclasses `SourceError`. The NWS API is multi-step.
`NwsClient.fetch(lat, lon)`:

1. `GET /points/{lat},{lon}` (coords rounded to 4 dp — NWS rejects more) → returns per-location
   URLs: `forecast`, `forecastHourly`, `forecastGridData`, `observationStations`, plus a
   `relativeLocation` used for the display `location_name`.
2. `GET` the hourly and daily forecast URLs with `?units=si`.
3. `GET` the gridpoint data → parse the `apparentTemperature` time series (windows of
   `(start, value)`), used to attach a `feels_like` to each `Temperature`.
4. `GET` the nearest observation station's latest observation → derive `raining` (keyword scan
   over `presentWeather`, falling back to the text description) and `observed_conditions`. This
   is enrichment only: failure returns `(None, None)`, never fails the report.

**High/Low rollover:** `_high_low` picks today's daytime high and overnight low, but after
`rollover_hour` (default 20:00 local) it targets the next day. It selects the first daytime/
nighttime periods on or after the target date, since the current day's daytime period may have
already dropped out of the feed by evening. Apparent high/low are the max/min feels-like across
each period's window.

All parsing failures raise `WeatherError`. Values stay SI at full precision.

### MTA subway (sources/builtins/mta/)

The `mta` source (`MtaSource` + `MtaConfig`, in `source.py`) owns its config models — `Platform` and
`Station` live here, not in central config — and wraps its boards in an `MtaData` value (`.boards`,
in `model.py`); `MtaError` subclasses `SourceError`. `MtaClient(stations).fetch()` builds one
`StationBoard` per configured station name.

- Each MTA GTFS-realtime feed covers a group of lines (e.g. one feed for N/Q/R/W). Feed URLs
  come from `nyct_gtfs.NYCTFeed._train_to_url` (`_LINE_TO_URL`). The client collects the
  distinct feed URLs across all platforms of all stations and loads each **at most once** per
  fetch (`_load_feeds`), then reuses them.
- Per platform, it filters trips by `line_id`, `headed_for_stop_id` (the platform stop id with
  `N`/`S` suffixes per its `direction`), and `underway=True`, extracts the predicted arrival at
  the matching stop, and drops arrivals already in the past.
- A station merges all its platforms' arrivals, groups by `Direction`, and sorts each group by
  time (N-then-S order). No truncation here — boards carry every upcoming arrival; the layout
  decides how many to show (see the Render section).
- Feed load / parse failures raise `MtaError`. An unknown line id also raises `MtaError`.

`feed_loader` is injectable for tests (defaults to `lambda url: NYCTFeed(url)`).

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
declares `font: str | None` and `weather_temp_units: Literal["us","si","both"]`.

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
  subpackage owning its `assets/icons/*.png` (chosen by `format.weather_icon()`, pasted with alpha).
  See `docs/plugins.md` for the full contract.

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
  `user_agent`, `rollover_hour`, `hourly_hours`; the `mta` plugin's `MtaConfig`: `stations`, whose
  `Station`/`Platform` models also live in that plugin). An unknown source name or key fails fast
  there. Zero sources is valid. The old top-level `[location]`, `[weather]`, `[stations.*]` sections
  are gone.
- `Dashboard` — the **output spec** only: `layout` (name, default `"glanceable"`), `output_path`,
  pixel `width`/`height`, `gray_levels`, `post_process_method`, `rotate`, plus a raw
  `layout_config: dict[str, Any]`. `Config` does **not** validate `layout_config`; `validate_layout`
  checks it against the named layout's own `Config` after discovery. Render knobs (`font`,
  `weather_temp_units`) live in `layout_config`, not on the dashboard.
- `plugins_path` — optional absolute dir of private layout and/or source plugins.
- `Schedule.interval_minutes` (default 5).

## Design Decisions

- **Presentation stays out of the domain models** — `DashboardData` and the source models
  carry no formatting; that lives in `format.py` and the layouts.
- **One source of truth for formatting** — the debug CLIs and the layouts call the same
  `format.py` helpers (re-exported through `render/toolkit.py`), so formatting has one home.
- **Fail fast on explicit config, degrade gracefully on source outages** — a bad `layout_config`
  or source key errors immediately, but a dead weather API just drops a panel.
