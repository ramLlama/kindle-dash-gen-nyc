---
name: sources-plugin-milestone
description: The "sources plugin architecture" milestone sequence (M2 infra, M3 cutover) and its intentional deferrals
type: project
---

Sources are being converted into a plugin system mirroring the existing render/layout plugin architecture.

**Why:** Symmetry with the render-layout plugin system (registry dict, plain `register_*` fn, Protocol interface, discovery via import side-effects, bundled + local roots).

**How to apply:**
- M2 (reviewed 2026-07-07) = infrastructure only: `sources/toolkit.py` (`SourceError`), `sources/registry.py` (`Source(Protocol[ConfigT_co])`, `register_source`, `build_sources`), empty `sources/builtins/`, second bundled root in `plugins.py`, rename `render/layouts/` → `render/builtins/`.
- The old `sources/weather.py` / `sources/mta.py` still exist and do NOT register yet — this is deliberate; they are converted to registered source plugins in M3. Do not flag them as dead/unwired.
- `docs/sources.md` is referenced in registry.py docstring but not yet created (expected in M3).
