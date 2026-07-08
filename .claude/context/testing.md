# Testing

Run with `uv run pytest` (config in `pyproject.toml`: `testpaths = ["tests"]`,
`pythonpath = ["."]`). One test file per source module (`tests/test_<module>.py`).

## Conventions

- **Tests are offline.** No test hits a real network. HTTP-backed clients are tested with
  `niquests-mock` (imported as `import niquests_mock as nm`); collaborators that aren't HTTP
  (the MTA feed, the layout in pipeline tests) are stubbed via injected fakes or `monkeypatch`.
- **Dependency injection over patching where the seam exists.** `NwsClient` takes an optional
  `session`; `MtaClient` takes an optional `feed_loader`. Prefer passing a fake through the
  constructor; use `monkeypatch.setattr(pipeline, "MtaClient", Fake)` only for wiring at the
  pipeline level.
- **Config in tests** is built with `Config.model_validate(CONFIG_DICT)` from an inline dict
  (see `tests/test_pipeline.py`), not by loading a TOML file.
- **Real image assertions.** Pillow-touching tests assert against actual decoded output
  (`Image.open(...).size`, `.mode == "L"`), not mocks — verify the bytes are a real
  Kindle-ready PNG.
- Follow the global preference: `with pytest.raises(ExnType):` without matching on the message.

## What each test file covers

- `test_pipeline.py` — source isolation (a `SourceError` from one source still renders the rest),
  the skip-render-when-all-sources-empty short-circuit (must not overwrite the last image or spend a
  generation), one-shot writes a grayscale image at the configured size, and the run loop
  (loops until `KeyboardInterrupt`, survives an iteration failure and retries).
- `test_plugins.py` — layout + source registry APIs, discovery of the bundled plugins, and loading
  a local `plugins_path` directory (the mechanism serving both layouts and sources).
- `test_sources.py` — `build_sources`: per-plugin `Config` validation of each `[sources.<name>]`
  slice, and fail-fast on an unknown source name or an invalid slice.
- `test_layout.py` — the layout registry: `validate_layout`/`build_layout` config validation,
  dispatch, `Fonts` resolution, and `LayoutError`.
- `test_weather.py` — NWS (`nws` source) multi-step fetch parsing, high/low rollover, apparent-temp
  series, observation/raining derivation.
- `test_mta.py` — MTA (`mta` source) feed dedup/reuse, platform merging, per-direction sort, error
  paths.
- `test_postprocess.py` — grayscale/fit/quantize, each `method`, gray-level LUT.
- `test_config.py` — pydantic validation, `extra="forbid"`, the reshaped `Dashboard` (output spec
  + raw `layout_config`).
- `test_format.py` — unit conversion and display formatting across `us`/`si`/`both`.
- `test_cli.py` — typer command wiring.

## Adding tests

- New source or render module → add `tests/test_<module>.py` mirroring the existing style.
- When adding a network call, mock it with `niquests-mock`; do not introduce a live dependency.
- A new layout is a plugin: add a test exercising its `Config` validation and that `render` returns
  an `Image` at the panel size (see `test_layout.py`). When changing the `Layout` protocol or the
  `layout_config` contract, update `docs/plugins.md`, `config.example.toml`, and
  [architecture.md](../architecture.md) together — it is the public plugin API.
