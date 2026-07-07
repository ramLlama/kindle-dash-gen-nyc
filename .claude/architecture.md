# Architecture & Data Flow

The system is a single linear pipeline that runs once per interval. All orchestration lives in
`pipeline.py`; the CLI (`cli.py`) is a thin typer wrapper that also exposes each pipeline stage
as a standalone debug command.

## Pipeline (pipeline.py)

```
run() loop  ──every interval_minutes──▶  run_once(cfg)
                                             │
                                    gather(cfg) ──▶ DashboardData
                                             │        (weather?, boards, generated_at)
                       all sources empty? ──▶ skip render, return RunResult([], [])
                                             │
                            for each [dashboards.<name>] (shared data):
                                    render(cfg, data, dash):
                                       render_raw(cfg, data, dash) ──▶ raw PNG   # dispatch on backend
                                          ├ pillow: layout.render(data, ...)
                                          └ llm:    build_prompt(...) + client.generate(...)
                                       post_process(raw, w, h, gray_levels, method) ──▶ PNG
                                             │
                                    _atomic_write(dash.path, png)   # per-dashboard, isolated
```

`render_raw()` dispatches on the dashboard's `backend`. The **pillow** backend (`_render_pillow`)
draws locally; the **llm** backend (`_render_llm`) resolves the aspect ratio, renders the Jinja2
prompt, and calls the image model. Both return raw PNG bytes and share `post_process()`.

- **`gather()`** constructs `NwsClient` and `MtaClient`, fetches from each inside its own
  try/except, and logs a degradation on `WeatherError` / `MtaError`. Returns `DashboardData`
  with `weather=None` and/or `boards=[]` on partial failure.
- **`run_once()`** gathers once, then renders every `[dashboards.<name>]` from that shared data,
  each to its own `path`. It short-circuits when both sources are empty: writing a blank dashboard
  would clobber the last good images and waste paid generations, so it returns an empty `RunResult`
  instead. A single dashboard's render/write failure is isolated (logged, others proceed) and its
  name collected in `RunResult.failed`, which the `run --one-shot` CLI turns into a non-zero exit.
- **`run()`** wraps `run_once()` in a `while True` + `time.sleep`. Any unexpected exception
  (i.e. not an isolated per-source or per-dashboard error, both already swallowed) is logged via
  `log.exception` and retried next interval. `KeyboardInterrupt` exits cleanly.
- Logging is stdlib `logging` configured in the `run` CLI command (INFO, `%H:%M:%S`).

## Sources

### NWS weather (sources/weather.py)

The NWS API is multi-step. `NwsClient.fetch(lat, lon)`:

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

### MTA subway (sources/mta.py)

`MtaClient(stations).fetch()` builds one `StationBoard` per configured station name.

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

Two backends produce raw PNG bytes from `DashboardData`, selected per dashboard by its `backend`
(`"pillow"` default, `"llm"` opt-in). `post_process()` is shared by both.

### Layout — pillow backend (render/layout.py + plugins)

`render(data, *, units, width, height, layout, font)` (in `render/layout.py`) draws the dashboard
deterministically with Pillow at the exact panel size and returns PNG bytes. It first calls
`plugins.load_plugins()` to populate `_LAYOUTS` (a name → renderer-class registry), then dispatches
by `layout` name. Unknown layout / unresolvable font / missing asset raise `LayoutError`. Free,
offline, exact, never garbles the data.

**Layouts are plugins — nothing is special-cased.** `_LAYOUTS` starts empty; every layout
(including bundled `glanceable`) registers itself via `register_layout(name, cls)` at import time,
and discovery imports them. A layout class implements the `Layout` protocol: `__init__(width,
height, fonts: Fonts, units)` + `render(data) -> Image`.

- **Toolkit (`render/toolkit.py`)** is the public surface a plugin builds on: `Fonts` (fontconfig
  `fc-match` resolution → file + face index, verified against the requested family so a missing
  font fails fast), `INK`/`PAPER`, `fit_font` (shrink-to-fit), `load_asset_image(package, rel_path)`,
  and `LayoutError`. `glanceable` uses only this — so any private plugin (or a 1:1 recreation of
  `glanceable`) can be built without core internals.
- **Discovery (`plugins.py`)** scans two roots by identical logic: the bundled
  `kindle_dash_gen_nyc.render.layouts` package (always), and an optional local directory named by
  `Config.plugins_path` (imported by directory name after putting its parent on `sys.path`).
  `load_plugins(local_dir=None)` is idempotent; a missing package is a silent no-op. `pipeline`
  passes `cfg.plugins_path`; `layout.render()` loads the bundled root on its own for direct callers.
- **Bundled `glanceable`** lives at `render/layouts/glanceable/` — a self-contained plugin
  subpackage owning its `assets/icons/*.png` (chosen by `format.weather_icon()`, pasted with alpha).
  See `docs/plugins.md` for the full contract.

### Prompt — llm backend (render/prompt.py)

`render_prompt(data, *, units, width, height, aspect, template)` resolves the template (a
bundled name under `assets/dashboard_prompts/*.j2`, else a filesystem path) and renders it.

**Public template context contract** (custom user templates depend on this — treat as an API):

- Variables: `weather` (`WeatherReport | None`), `boards` (`list[StationBoard]`), `units`
  (`"us"|"si"|"both"`), `width`, `height`, `aspect` (e.g. `"4:3"`), `now` (= `generated_at`).
- Helper globals (same `format.py` functions the debug CLIs use, so formatting has one source
  of truth): `format_reading`, `format_apparent`, `format_temp`, `format_wind`, `format_eta`.

Jinja env: `autoescape=False`, `trim_blocks=True`, `lstrip_blocks=True`. The prompt is plain
text describing the dashboard layout to the image model; the `dense.j2` template instructs the
model not to render its own field labels, only the data values.

### OpenRouter (render/openrouter.py)

`OpenRouterClient(model, api_key=None)` talks to the Unified Image API at
`https://openrouter.ai/api/v1`.

- **Capability discovery:** `supported_parameters` (a `cached_property`) fetches
  `/images/models/{model}/endpoints` and merges `supported_parameters` across endpoints —
  enum `values` are unioned (order-preserved, deduped). No auth needed for this or for aspect
  resolution; only `generate()` requires the key.
- `resolve_aspect_ratio(w, h, override)` returns `override` if the model supports it, else the
  supported ratio nearest to `w/h` (`nearest_aspect_ratio`). An unsupported override fails fast
  with the valid list.
- `generate(prompt, *, aspect_ratio, resolution)` POSTs to `/images`, validates a
  `resolution` override against the model's enum first, and base64-decodes `data[0].b64_json`
  into raw image bytes. All failures raise `OpenRouterError`.

### Post-process (render/postprocess.py)

`post_process(png, *, width, height, gray_levels, method)`, in order:

1. `convert("L")` → grayscale.
2. `_fit` to exactly `(width, height)` by `method`:
   - `resize` — stretch to fill, ignoring aspect (minor distortion).
   - `crop` — `ImageOps.fit` scale-to-cover + center-crop.
   - `pad` — `ImageOps.pad` fit + white (255) e-ink bars.
3. `_quantize_lut` — a 256-entry LUT snapping each value to the nearest of `gray_levels`
   evenly-spaced grays (models the Voyage's fixed hardware palette; requires `levels >= 2`).

Returns PNG bytes. Defaults target the Kindle Voyage: 1072×1448 portrait, 16 gray levels.

## Configuration (config.py)

`load_config(path)` reads TOML via `tomllib` and validates into `Config`. Every model uses
`extra="forbid"`. Notable pieces:

- `Secret` — exactly one of `value` / `value_from_cmd` (enforced by a model validator);
  `resolve()` returns the literal or runs the shell command and returns stripped stdout.
- `stations: dict[str, Station]` — display name → board; each `Station` has `platforms`; each
  `Platform` has `lines`, `stop_id`, `direction`.
- `Dashboard` — output `path`, pixel `width`/`height`, `gray_levels`, `post_process_method`,
  and optional `aspect_ratio` / `resolution` overrides.
- `Schedule.interval_minutes` (default 5).

## Design Decisions

- **Presentation stays out of the domain models** — `DashboardData` and the source models
  carry no formatting; that lives in `format.py` and the templates.
- **One source of truth for formatting** — the debug CLIs and the prompt template call the
  same `format.py` helpers, so what you preview matches what the model is told.
- **Fail fast on explicit overrides, degrade gracefully on source outages** — a bad
  `aspect_ratio` override errors immediately, but a dead weather API just drops a panel.
