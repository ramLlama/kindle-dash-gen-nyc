# Render layout plugins

The dashboard is drawn locally with Pillow by a named **layout**. Layouts are plugins: the
registry starts empty and every layout — including the bundled `glanceable` — registers itself the
same way. There is no privileged builtin. You can add your own layout locally (e.g. a home-specific
design) without touching the app, and a private plugin has access to the exact same API the bundled
one uses, so `glanceable` could be recreated 1:1 as a private plugin.

> Data **sources** (weather, subway, …) use the same plugin machinery on the input side. See
> [sources.md](sources.md). One `plugins_path` directory can hold both layout and source plugins.

## The two plugin directories

Both are discovered by identical logic (`kindle_dash_gen/plugins.py`):

1. **Bundled** — `kindle_dash_gen/render/builtins/`, shipped with the app. Always loaded.
2. **Local** — a directory of your private plugins, named by `plugins_path` in your config. Loaded
   only when set. It is imported by directory name (its parent is added to `sys.path`), so it must
   be a Python package (have an `__init__.py`). The path must be **absolute** (so discovery is
   unambiguous regardless of the process's working directory); a directory that doesn't exist logs
   a warning rather than failing the render.

```toml
# config.toml — an absolute package directory of private plugins
plugins_path = "/home/you/kindle-dash-gen-nyc/kindle_dash_gen_nyc_plugins"
```

## The contract

A plugin is a **subpackage** of a plugin directory that, on import, calls `register_layout`. A
layout **owns its config**: it declares a pydantic `Config` model (keep `extra="forbid"`) that
validates the dashboard's `[dashboards.<name>.layout_config]` table — the same way a source owns its
`[sources.<name>]` table.

```python
# <plugins_dir>/my_layout/__init__.py
from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict

from zoneinfo import ZoneInfo

from kindle_dash_gen.render.layout import Layout, register_layout
from kindle_dash_gen.render.toolkit import DEFAULT_FONT, INK, PAPER, Fonts


class MyLayoutConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")  # reject unknown keys in [dashboards.x.layout_config]

    font: str | None = None
    timezone: ZoneInfo          # pydantic parses the IANA name and rejects an unknown one
    weather_temp_units: str = "us"


class MyLayout(Layout[MyLayoutConfig]):
    Config = MyLayoutConfig  # the dispatch validates layout_config against this before constructing

    # Constructed with the validated config plus the dashboard's output resolution (keyword-only).
    def __init__(self, config: MyLayoutConfig, *, width: int, height: int) -> None:
        self.fonts = Fonts(config.font if config.font is not None else DEFAULT_FONT)
        self.img = Image.new("L", (width, height), PAPER)
        self.d = ImageDraw.Draw(self.img)

    # Draw DashboardData and return a raw "L"-mode image; the pipeline post-processes + writes it.
    def render(self, data) -> Image.Image:
        self.d.text((20, 20), "hello", font=self.fonts.get(40, "Bold"), fill=INK)
        return self.img


register_layout("my_layout", MyLayout)
```

Then select it per dashboard, and configure it under `layout_config`:

```toml
[dashboards.main]
layout = "my_layout"
output_path = "./out/dashboard.png"
width = 1072
height = 1448

[dashboards.main.layout_config]   # validated by MyLayoutConfig
font = "Futura"
timezone = "America/New_York"
weather_temp_units = "both"
```

## The `Layout` protocol

A layout class satisfies `kindle_dash_gen.render.layout.Layout`:

- `Config: ClassVar[type[BaseModel]]` — the pydantic model for this layout's `layout_config` table.
  Keep `model_config = ConfigDict(extra="forbid")` so unknown keys are rejected. The dispatch
  validates the raw table against `Config` (a bad table fails fast at config load), then constructs
  the layout with the validated instance.
- `__init__(self, config, *, width, height)` — `config` is the validated `Config` instance;
  `width`/`height` are the dashboard's output resolution (the default canvas size). Declaring the
  class as `Layout[MyLayoutConfig]` types `config` as `MyLayoutConfig`.
- `render(self, data: DashboardData) -> PIL.Image.Image` — a raw `"L"`-mode (8-bit grayscale) image.
  The pipeline post-processes it (grayscale, fit to `width`×`height`, quantize to the device gray
  levels) and writes it to the dashboard's `output_path`; drawing at the exact size makes the fit a
  no-op.

### One fetch, many dashboards — select what you draw

`gather()` runs once and feeds every dashboard from the same `source_data`, so a source configured
anywhere is available to *all* dashboards. A layout that wants each dashboard to show a different
slice takes that selection in its own config. The bundled `glanceable` does this two ways:

- `weather_location` (required) names which weather location to draw. Weather sources key their
  results by name (`data.locations["NYC"]`), and that name is also the join key across providers,
  so the layout reconciles Open-Meteo and NWS for the same place.
- `transit_boards` (optional) is an allowlist of station names; omit it to draw every board, or
  list names to keep only those.

Match on the *canonical* name a source produced (the config key / `board.name`), not a display
label, so renaming what's shown never breaks the selection. This is how sibling dashboards fed by
one fetch each render their own city and stations.

### Datetimes reach you as aware UTC

`generated_at` and every datetime inside `source_data` is timezone-aware **in UTC** (see
[sources.md](sources.md#datetimes-are-aware-utc)). A bare `strftime` therefore prints *UTC* clock
times, which is almost never what a dashboard should show. Take an IANA zone in your config and
convert before formatting:

```python
self.tz = config.timezone                       # a ZoneInfo, validated at config load
label = data.generated_at.astimezone(self.tz).strftime("%-I:%M %p")
```

Give the field no default: silently defaulting to UTC (or to the host's zone) produces a panel whose
clock is wrong in a way nobody notices until they read it. This is also what lets one run render a
New York and a Bay Area dashboard from a single fetch — each converts the same UTC instants to its
own zone. The bundled `glanceable` does exactly this at its three time-formatting sites.

`DashboardData` (`kindle_dash_gen.models`) carries `generated_at: datetime` and `source_data:
dict[type, Any]`, which maps each source's produced data class to its instance. Look up what you
need defensively — a source that failed or had no data is simply absent:

```python
from kindle_dash_gen.sources.builtins.mta.model import MtaData
from kindle_dash_gen.sources.builtins.nws.model import NwsData

weather = data.source_data.get(NwsData)                # NwsData | None
mta = data.source_data.get(MtaData)
boards = mta.boards if mta is not None else []          # list[StationBoard]
```

Each source owns the data type it produces, so a layout imports those types from the source
package. A layout that renders weather from more than one provider looks each up by its own type
(`data.source_data.get(NwsData)`, `data.source_data.get(OpenMeteoData)`) and reconciles them in its
own adapter — there is no shared weather model. Note that importing a provider's `.model` also
imports and registers that provider (its package `__init__` pulls in the source client and its
dependencies), so a layout that references a source type transitively loads that source's stack.

## The toolkit (`kindle_dash_gen.render.toolkit`)

The public surface for building layouts — everything the bundled `glanceable` uses:

- `Fonts` — resolves a system font family via fontconfig. `fonts.get(size, weight)` returns a
  Pillow font; weights: `Regular`, `Medium`, `SemiBold`, `Bold`, `Black`. A missing family fails
  fast with `LayoutError` rather than silently substituting.
- `DEFAULT_FONT` — the app-wide fallback font family, for the common case of resolving an
  unspecified (`None`) `font` to a single default: `Fonts(font or DEFAULT_FONT)`.
- `INK = 0`, `PAPER = 255` — the grayscale ink/paper values.
- `fit_font(fonts, text, weight, max_size, max_width)` — the largest face at which `text` fits
  `max_width`, stepping down from `max_size`.
- `load_asset_image(package, rel_path)` — load an image resource your plugin ships (e.g.
  `load_asset_image("kindle_dash_gen_nyc_plugins.my_layout", "assets/bg.png")`). Bundle assets
  inside your plugin package and reference them by your package name.
- Display formatters — `format_reading`, `format_apparent`, `format_temp`, `format_wind`,
  `format_aqi`, `format_eta`, and `weather_icon`, plus the `aqi_is_unhealthy` predicate (US AQI 151+,
  i.e. "Unhealthy" or worse — a layout can flag those readings; the "Unhealthy (Sensitive)" band
  below it is scoped to at-risk groups). **Render through these**; all data reaches a layout in SI, and
  these apply unit conversion + rounding at display time (the "SI internally, round at display"
  invariant). `format_reading`/`format_apparent` take anything exposing `.real`/`.feels_like`, so
  each provider's own temperature type works. `weather_icon(observed, conditions, raining)` takes
  plain strings (pass `observed=None` if your source has no station observation) and returns the
  icon name (`"sunny"`/`"cloudy"`/`"rain"`/`"snow"`) a layout can pair with its own `assets/icons/`.
- `Secret` — type a credential field in your layout's `Config` as `Secret` so the value can stay
  out of the config file (a literal, a command's stdout, or an environment variable), then read
  `.value` at use time. See [Secrets in config](sources.md#secrets-in-config-secret).
- `LayoutError` — raise for unrecoverable layout problems; the pipeline treats it as a render
  failure for that dashboard (isolated from the others).

## Notes

- Registering a name already taken raises `LayoutError` (two plugins claiming the same layout is a
  configuration error, so it fails fast).
- Discovery uses import side-effects, not entry points — the project runs in place
  (`package = false`), so there is no install step.
- The bundled `glanceable` at `render/builtins/glanceable/` is the worked reference: it depends only
  on this toolkit, owns its own `assets/icons/`, and registers itself — structurally identical to a
  private plugin. It is also the concrete example of multi-provider reconciliation: a private
  `_weather(data)` adapter normalizes whichever weather provider is present (preferring `OpenMeteoData`,
  falling back to `NwsData`) into a layout-local draw surface, resolving the icon per provider —
  `weather_icon()` for NWS, a local WMO-code→icon map for Open-Meteo.
