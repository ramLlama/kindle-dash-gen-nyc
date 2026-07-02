# Style Guide

Match the existing code first. This documents the conventions already in play; enforced rules
live in `pyproject.toml` (`[tool.ruff]`).

## Tooling & Enforcement

- **Line length:** 100 (`tool.ruff.line-length`).
- **Ruff lint select:** `E`, `F`, `I` (isort), `UP` (pyupgrade), `B` (bugbear). Run
  `uv run ruff check .` — it is a required gate alongside `uv run pytest`.
- Target Python **3.14+**. Use modern syntax freely: `X | None` unions, `StrEnum`, `tomllib`,
  built-in generics (`list[str]`, `dict[str, Station]`).
- Every module starts with `from __future__ import annotations`.

## Naming

- Modules and functions: `snake_case`. Classes: `PascalCase`. Constants: `UPPER_SNAKE`.
- **Module-private helpers are prefixed with `_`** (e.g. `_atomic_write`, `_high_low`,
  `_quantize_lut`, `_merge_supported_parameters`). Public API is the un-prefixed surface.
- Domain error classes are named `<Source>Error` and subclass `RuntimeError`
  (`WeatherError`, `MtaError`, `OpenRouterError`).
- Module-level constants for external vocab / literals: `NWS_API`, `API`, `_LINE_TO_URL`,
  `_RAIN_KEYWORDS`, `_DIRECTION_SUFFIXES`.

## Types & Models

- **Domain models are frozen dataclasses**, keyword-only where they have many fields
  (`@dataclass(frozen=True, kw_only=True)`). They carry data only — no methods, no formatting.
- **Config models are pydantic** `BaseModel` with `model_config = ConfigDict(extra="forbid")`
  on every class. Provide sensible defaults inline; document non-obvious fields with a trailing
  `#` comment (see `config.py`).
- Prefer precise unions and `Literal[...]` for closed sets (`Literal["us", "si", "both"]`,
  `PostProcessMethod = Literal["resize", "crop", "pad"]`).

## Functions & Docstrings

- Non-trivial functions and all public functions have a one-line (or short) docstring stating
  intent, not mechanics. Module docstrings explain the module's role and any non-obvious
  invariant (e.g. "all data is SI; callers round for display").
- Keyword-only params (`*,`) for multi-arg render/pipeline functions to keep call sites
  self-documenting (`post_process(png, *, width, height, gray_levels, method)`).
- Inject collaborators for testability with a defaulted optional param
  (`session: niquests.Session | None = None`, `feed_loader: FeedLoader | None = None`).

## Comments

- Comment the *why* and non-obvious *what*, not the line-by-line obvious. Good examples in the
  codebase: the protobuf-override note in `pyproject.toml`, the "NWS rejects >4 decimal places"
  note, the rollover-hour rationale, the atomic-write explanation.
- Do not leave commented-out code or `TODO` placeholders.

## Python-specific conventions (from global user prefs, honored here)

- **Explicit checks over truthiness** for containers/values: `if len(x) == 0`, `if x is None`,
  `if x is not None` — not `if not x`. This is used consistently across the codebase.
- **Minimal visibility**: don't export/widen something until it's used outside its module.
- **Centralize cross-cutting logic**: display formatting lives only in `format.py`; feed-URL
  mapping and direction suffixes are single named constants.
- **Fail fast** on things that should succeed (bad explicit override, missing capability);
  **degrade gracefully** only for expected external outages (source fetch failures).

## Imports & Structure

- isort-ordered (ruff `I`): stdlib, third-party, first-party (`from . / from ..`).
- `models/__init__.py` re-exports the domain models with an explicit `__all__`; import domain
  types from `kindle_dash_gen_nyc.models`, not the submodules.
- Bundled assets are loaded via `importlib.resources.files("kindle_dash_gen_nyc")`, never with
  hardcoded filesystem paths — keeps them resolvable regardless of CWD.

## Commits

Conventional Commits (`feat`, `fix`, `refactor`, `docs`, `test`, `build`, `ci`, `perf`) — no
`chore`. One milestone/feature per commit. Include a `Co-Authored-By: Claude` trailer.
