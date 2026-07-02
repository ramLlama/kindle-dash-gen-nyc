# Testing

Run with `uv run pytest` (config in `pyproject.toml`: `testpaths = ["tests"]`,
`pythonpath = ["."]`). One test file per source module (`tests/test_<module>.py`).

## Conventions

- **Tests are offline.** No test hits a real network. HTTP-backed clients are tested with
  `niquests-mock` (imported as `import niquests_mock as nm`); collaborators that aren't HTTP
  (the MTA feed, the render clients in pipeline tests) are stubbed via injected fakes or
  `monkeypatch`.
- **Dependency injection over patching where the seam exists.** `NwsClient` and
  `OpenRouterClient` take an optional `session`; `MtaClient` takes an optional `feed_loader`.
  Prefer passing a fake through the constructor; use `monkeypatch.setattr(pipeline, "MtaClient",
  Fake)` only for wiring at the pipeline level.
- **Config in tests** is built with `Config.model_validate(CONFIG_DICT)` from an inline dict
  (see `tests/test_pipeline.py`), not by loading a TOML file.
- **Real image assertions.** Pillow-touching tests assert against actual decoded output
  (`Image.open(...).size`, `.mode == "L"`), not mocks — verify the bytes are a real
  Kindle-ready PNG.
- Follow the global preference: `with pytest.raises(ExnType):` without matching on the message.

## What each test file covers

- `test_pipeline.py` — source isolation (weather-down / subway-down still renders), the
  skip-render-when-all-sources-empty short-circuit (must not overwrite the last image or spend a
  generation), one-shot writes a grayscale image at the configured size, and the run loop
  (loops until `KeyboardInterrupt`, survives an iteration failure and retries).
- `test_weather.py` — NWS multi-step fetch parsing, high/low rollover, apparent-temp series,
  observation/raining derivation.
- `test_mta.py` — feed dedup/reuse, platform merging, per-direction cap and sort, error paths.
- `test_openrouter.py` — capability merging across endpoints, nearest aspect ratio, override
  validation, base64 decode, error handling. Uses `niquests_mock`.
- `test_postprocess.py` — grayscale/fit/quantize, each `method`, gray-level LUT.
- `test_prompt.py` — template resolution (bundled vs path) and context contract.
- `test_config.py` — pydantic validation, `Secret` one-of rule, `extra="forbid"`.
- `test_format.py` — unit conversion and display formatting across `us`/`si`/`both`.
- `test_cli.py` — typer command wiring.

## Adding tests

- New source or render module → add `tests/test_<module>.py` mirroring the existing style.
- When adding a network call, mock it with `niquests-mock`; do not introduce a live dependency.
- When changing the template context contract (`render/prompt.py`), update `test_prompt.py` and
  the contract docs in `prompt.py`, `config.example.toml`, and
  [architecture.md](../architecture.md) together — it is a public API.
