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
uv run python -m kindle_dash_gen_nyc --config config.toml dashboard render out/dashboard.png
uv run python -m kindle_dash_gen_nyc generate --config config.toml   # one-shot (M5)
uv run python -m kindle_dash_gen_nyc run --config config.toml        # loop every interval (M5)
```

`dashboard preview-prompt` fetches live weather/subway data and prints the prompt that would be
sent to the image model, without calling it (no spend). `dashboard render [output_file]` does
the same, then generates the image and writes it to `output_file` (or `[output].path` from the
config).

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
- `units` (`"us" | "si" | "both"`), `width`, `height` (from `[output]`)
- `aspect` (resolved aspect ratio, e.g. `"4:3"`), `now` (generation time, for ETA formatting)
- helper globals: `format_reading(temp, units)` (real, feels-like in brackets),
  `format_apparent(temp, units)` (feels-like only), `format_temp(celsius, units)`,
  `format_wind(kmh, direction, units)`, `format_eta(arrival, now)` — the same formatters the
  debug CLIs use, so display formatting has one source of truth

`[output].aspect_ratio` and `[output].resolution` optionally override the auto-selected
values; both must be one of the values the configured model supports (queried at runtime), or
the command fails with a clear error listing the valid values.

## Development

```sh
uv run pytest
uv run ruff check
```
