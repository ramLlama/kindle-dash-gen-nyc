# Render layout plugins

The pillow render backend draws the dashboard with a named **layout**. Layouts are plugins: the
registry starts empty and every layout — including the bundled `glanceable` — registers itself the
same way. There is no privileged builtin. You can add your own layout locally (e.g. a home-specific
design) without touching the app, and a private plugin has access to the exact same API the bundled
one uses, so `glanceable` could be recreated 1:1 as a private plugin.

## The two plugin directories

Both are discovered by identical logic (`kindle_dash_gen/plugins.py`):

1. **Bundled** — `kindle_dash_gen/render/layouts/`, shipped with the app. Always loaded.
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

A plugin is a **subpackage** of a plugin directory that, on import, calls `register_layout`:

```python
# <plugins_dir>/my_layout/__init__.py
from kindle_dash_gen.render.layout import register_layout
from kindle_dash_gen.render.toolkit import DEFAULT_FONT, INK, PAPER, Fonts, fit_font, load_asset_image
from PIL import Image, ImageDraw


class MyLayout:
    # Constructed with the panel size, the configured font family (or None), and display units.
    def __init__(self, width: int, height: int, font: str | None, units: str) -> None:
        # font is None when the dashboard didn't set one — pick your own default for that case.
        self.fonts = Fonts(font if font is not None else DEFAULT_FONT)
        self.img = Image.new("L", (width, height), PAPER)
        self.d = ImageDraw.Draw(self.img)

    # Draw DashboardData and return an "L"-mode image of the panel size.
    def render(self, data) -> Image.Image:
        self.d.text((20, 20), "hello", font=..., fill=INK)
        return self.img


register_layout("my_layout", MyLayout)
```

Then select it per dashboard:

```toml
[dashboards.main]
backend = "pillow"
layout  = "my_layout"
```

## The `Layout` protocol

A layout class satisfies `kindle_dash_gen.render.layout.Layout`:

- `__init__(self, width: int, height: int, font: str | None, units: str)` — `font` is the
  dashboard's configured font family, or `None` when unspecified. Resolve it into `Fonts` yourself,
  supplying your own default for the `None` case (e.g. `Fonts(font or DEFAULT_FONT)`, or per-role
  defaults like the bundled `home_mta_map`, which uses Futura for line letters and Helvetica Neue
  for everything else).
- `render(self, data: DashboardData) -> PIL.Image.Image` — an `"L"`-mode (8-bit grayscale) image
  of exactly `width` × `height`. It is post-processed (quantized to the device gray levels) by the
  pipeline; a pillow layout is already the exact size, so the fit step is a no-op.

`DashboardData` (`kindle_dash_gen.models`) carries `weather: WeatherReport | None`, `boards:
list[StationBoard]`, and `generated_at: datetime`. Both `weather` and `boards` can be absent
(sources degrade independently), so guard them.

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
  `format_eta`, and `weather_icon`. **Render through these**; all data reaches a layout in SI, and
  these apply unit conversion + rounding at display time (the "SI internally, round at display"
  invariant). `weather_icon(report)` returns the icon name (`"sunny"`/`"cloudy"`/`"rain"`/`"snow"`)
  a layout can pair with its own `assets/icons/`.
- `LayoutError` — raise for unrecoverable layout problems; the pipeline treats it as a render
  failure for that dashboard (isolated from the others).

## Notes

- Registering a name already taken raises `LayoutError` (two plugins claiming the same layout is a
  configuration error, so it fails fast).
- Discovery uses import side-effects, not entry points — the project runs in place
  (`package = false`), so there is no install step.
- The bundled `glanceable` at `render/layouts/glanceable/` is the worked reference: it depends only
  on this toolkit, owns its own `assets/icons/`, and registers itself — structurally identical to a
  private plugin.
