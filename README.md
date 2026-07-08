# kindle-dash-gen

Generate a Kindle e-ink dashboard image with local weather (NWS) and real-time NYC subway
arrivals (MTA), drawn locally with Pillow and post-processed for a Kindle display.

The generator, every few minutes:

1. Pulls the local forecast from the [NWS API](https://www.weather.gov/documentation/services-web-api).
2. Pulls real-time subway arrivals via [`nyct-gtfs`](https://github.com/Andrew-Dickinson/nyct-gtfs).
3. Draws the whole dashboard from that data with a Pillow **layout** (free, offline, exact).
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
uv run python -m kindle_dash_gen --help
uv run python -m kindle_dash_gen version
uv run python -m kindle_dash_gen --config config.toml dashboard render out/raw.png
uv run python -m kindle_dash_gen --config config.toml dashboard post-process out/raw.png out/dashboard.png
uv run python -m kindle_dash_gen --config config.toml run --one-shot   # generate once and exit
uv run python -m kindle_dash_gen --config config.toml run              # loop every interval
```

`run` is the full pipeline: it gathers weather + subway data **once**, then renders every
configured dashboard, post-processes each for the Kindle, and writes it to that dashboard's
`[dashboards.<name>].output_path`. Configure one or more outputs as named tables (`[dashboards.main]`,
`[dashboards.landscape]`, …); a single fetch feeds them all. Without a flag it loops every
`[schedule].interval_minutes` (Ctrl-C exits cleanly, and a failed iteration is logged and retried
at the next interval); `--one-shot` runs a single iteration and exits non-zero if any dashboard
failed to render. Each source is isolated — if weather or subway is unavailable, that panel is
dropped and the render still proceeds; and each dashboard is isolated from the others.

The `dashboard` subcommands expose the individual pipeline steps for debugging. `dashboard render`
fetches once and writes every dashboard's raw, un-post-processed image to its path; restrict to a
subset with repeated `--name`, or pass an `[output_file]` to redirect a single dashboard. `dashboard
post-process INPUT OUTPUT` massages an existing PNG into a Kindle-ready frame: grayscale, fitted to
`width`×`height` via `post_process_method` (`resize`/`crop`/`pad`), and quantized to `gray_levels`.
`post-process` acts on one dashboard (the sole one, or a single `--name`).

## Configuration

See [`config.example.toml`](config.example.toml). Data sources and render layouts are both plugins;
see [`docs/sources.md`](docs/sources.md) and [`docs/plugins.md`](docs/plugins.md).

## Development

```sh
uv run pytest
uv run ruff check
```
