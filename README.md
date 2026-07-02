# kindle-dash-gen-nyc

Generate a Kindle e-ink dashboard image with local weather (NWS) and real-time NYC subway
arrivals (MTA), rendered by an OpenRouter image model and post-processed for a Kindle
display.

The generator, every few minutes:

1. Pulls the local forecast from the [NWS API](https://www.weather.gov/documentation/services-web-api).
2. Pulls real-time subway arrivals via [`nyct-gtfs`](https://github.com/Andrew-Dickinson/nyct-gtfs).
3. Asks an OpenRouter image model to render the whole dashboard from that data.
4. Post-processes the image (grayscale, exact resolution, reduced bit depth) for the Kindle.
5. Writes the PNG to a configured path for syncing to the device.

## Requirements

- Python 3.14+
- [`uv`](https://docs.astral.sh/uv/)

## Setup

```sh
uv sync
cp config.example.toml config.toml   # then edit config.toml
```

## Usage

Run in place from the clone (no install step):

```sh
uv run python -m kindle_dash_gen_nyc --help
uv run python -m kindle_dash_gen_nyc version
uv run python -m kindle_dash_gen_nyc --config config.toml dashboard preview-prompt  # debug, no spend
uv run python -m kindle_dash_gen_nyc --config config.toml dashboard render out/raw.png
uv run python -m kindle_dash_gen_nyc --config config.toml dashboard post-process out/raw.png out/dashboard.png
uv run python -m kindle_dash_gen_nyc --config config.toml run --one-shot   # generate once and exit
uv run python -m kindle_dash_gen_nyc --config config.toml run              # loop every interval
```

`run` is the full pipeline: it gathers weather + subway data, renders the image via
OpenRouter, post-processes it for the Kindle, and writes the result to `[dashboard].path`.
Without a flag it loops every `[schedule].interval_minutes` (Ctrl-C exits cleanly, and a failed
iteration is logged and retried at the next interval); `--one-shot` runs a single iteration and
exits. Each source is isolated — if weather or subway is unavailable, that panel is dropped and
the render still proceeds.

The `dashboard` subcommands expose the individual pipeline steps for debugging. `dashboard
preview-prompt` fetches live data and prints the prompt without calling the image model (no
spend). `dashboard render [output_file]` generates and writes the raw, un-post-processed image
(to `output_file` or `[dashboard].path`). `dashboard post-process INPUT OUTPUT` massages an
existing PNG into a Kindle-ready frame: grayscale, fitted to `width`×`height` via
`post_process_method` (`resize`/`crop`/`pad`), and quantized to `gray_levels`.

## Configuration

See [`config.example.toml`](config.example.toml). The OpenRouter API key is never read from
an environment variable — supply it inline (`{ value = "..." }`) or as a command whose
stdout is the key (`{ value_from_cmd = "pass show openrouter/key" }`).

### Prompt templates

`[openrouter].prompt_template` selects the Jinja2 template describing the dashboard to the
image model: either a bundled name (currently `"dense"`, an info-dense layout — see
`kindle_dash_gen_nyc/assets/dashboard_prompts/dense.j2`) or a filesystem path to your own
`.j2` file. Custom templates render with this context:

- `weather` (`WeatherReport | None`), `boards` (`list[StationBoard]`)
- `units` (`"us" | "si" | "both"`), `width`, `height` (from `[dashboard]`)
- `aspect` (resolved aspect ratio, e.g. `"4:3"`), `now` (generation time, for ETA formatting)
- helper globals: `format_reading(temp, units)` (real, feels-like in brackets),
  `format_apparent(temp, units)` (feels-like only), `format_temp(celsius, units)`,
  `format_wind(kmh, direction, units)`, `format_eta(arrival, now)` — the same formatters the
  debug CLIs use, so display formatting has one source of truth

`[dashboard].aspect_ratio` and `[dashboard].resolution` optionally override the auto-selected
values; both must be one of the values the configured model supports (queried at runtime), or
the command fails with a clear error listing the valid values.

## Development

```sh
uv run pytest
uv run ruff check
```
