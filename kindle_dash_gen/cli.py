"""Command-line interface for the Kindle dashboard generator."""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from PIL import Image
from rich.console import Console

from . import __version__, pipeline, plugins
from .config import Config, Dashboard, load_config
from .render.layout import validate_layout
from .render.postprocess import post_process
from .sources.registry import (
    Source,
    SourceError,
    build_sources,
    registered_sources,
    source_class,
)

app = typer.Typer(
    help="Generate a Kindle e-ink dashboard with NYC weather and subway info.",
    no_args_is_help=True,
)
source_app = typer.Typer(
    help="Inspect a data source in isolation: `source <name>` fetches it; `source list` to list.",
    no_args_is_help=True,
)
app.add_typer(source_app, name="source")
dashboard_app = typer.Typer(help="Render the dashboard image.", no_args_is_help=True)
app.add_typer(dashboard_app, name="dashboard")

# Each source is mounted as a `source <name>` subcommand ahead of parsing, since typer has no native
# dynamic subcommands: the bundled sources at import (see the _wire call near the bottom), local
# plugins_path ones in run() after it sniffs --config. Re-mounting a source is a harmless no-op
# (typer overwrites by name), so `_wired_sources` is just to avoid rebuilding the same sub-typer.
_wired_sources: set[str] = set()

# Source names the `source` group can't use: they'd shadow its own static commands (`source list`).
_RESERVED_SOURCE_NAMES = frozenset({"list"})

ConfigOption = Annotated[
    Path,
    typer.Option("--config", "-c", help="Path to the TOML config file."),
]


@app.callback()
def main(ctx: typer.Context, config: ConfigOption = Path("config.toml")) -> None:
    """Store the config path for subcommands to load on demand."""
    ctx.obj = config


def _config(ctx: typer.Context) -> Config:
    """Load the config, discover plugins, and eagerly validate every source (fail fast)."""
    cfg = load_config(ctx.obj)
    plugins.load_plugins(cfg.plugins_path)
    build_sources(cfg.sources)  # validate each [sources.<name>] against its plugin now, not mid-run
    for dash in cfg.dashboards.values():  # and each dashboard's layout_config against its layout
        validate_layout(dash.layout, dash.layout_config)
    return cfg


NameOption = Annotated[
    list[str] | None,
    typer.Option("--name", "-n", help="Dashboard name(s) to act on; repeatable. Default: all."),
]


def _selected_dashboards(cfg: Config, names: list[str] | None) -> dict[str, Dashboard]:
    """Every dashboard, or just the named subset (error on any unknown name)."""
    if names is None or len(names) == 0:
        return cfg.dashboards
    unknown = [n for n in names if n not in cfg.dashboards]
    if len(unknown) > 0:
        raise typer.BadParameter(f"unknown dashboard(s) {unknown}; have: {sorted(cfg.dashboards)}")
    return {n: cfg.dashboards[n] for n in names}


def _one_dashboard(cfg: Config, names: list[str] | None) -> tuple[str, Dashboard]:
    """Exactly one dashboard: the sole configured one, or a single --name; error otherwise."""
    selected = _selected_dashboards(cfg, names)
    if len(selected) != 1:
        raise typer.BadParameter(
            f"this command acts on one dashboard; pass a single --name (have {sorted(selected)})"
        )
    return next(iter(selected.items()))


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command(name="run")
def run_dashboard(
    ctx: typer.Context,
    one_shot: Annotated[
        bool,
        typer.Option("--one-shot", help="Run a single iteration and exit instead of looping."),
    ] = False,
) -> None:
    """Generate the Kindle dashboard(s) on the configured interval (or once with ``--one-shot``).

    Each run gathers weather + subway data once, then renders every configured dashboard,
    post-processes each for the Kindle, and writes it to that dashboard's path in your config.
    """
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
    )
    cfg = _config(ctx)
    if one_shot:
        # Surface render failures to the exit code so a cron/systemd one-shot doesn't report
        # success when a dashboard silently failed. "All sources down" is a legitimate skip (no
        # failures), so it still exits 0. The loop, by contrast, just retries next interval.
        result = pipeline.run_once(cfg)
        if len(result.failed) > 0:
            raise typer.Exit(code=1)
    else:
        pipeline.run(cfg)


def _discover_sources(ctx: typer.Context) -> Config:
    """Load the config and discover plugins for the ``source`` debug commands.

    Unlike :func:`_config`, this skips the eager whole-config validation (every source slice and
    every dashboard's ``layout_config``): inspecting one source shouldn't fail because an unrelated
    dashboard, or a different source, is misconfigured.
    """
    cfg = load_config(ctx.obj)
    plugins.load_plugins(cfg.plugins_path)
    return cfg


@source_app.command("list")
def source_list(ctx: typer.Context) -> None:
    """List the data sources available to run, marking those configured in the current config."""
    cfg = _discover_sources(ctx)
    configured = set(cfg.sources)
    console = Console()
    for name in registered_sources():
        console.print(f"{name}{'  (configured)' if name in configured else ''}")


def _build_source_typer(name: str, cls: type[Source[Any]]) -> typer.Typer:
    """A per-source subcommand app: fetch + print by default, plus the source's own ``cli()`` verbs.

    ``source <name>`` with no verb fetches the source (the default), so the callback runs the fetch
    only when no subcommand was invoked. A source's optional ``cli()`` verbs mount alongside it.
    """
    sub = typer.Typer(
        no_args_is_help=False,
        help=f"Run the {name!r} source (fetch + print), or one of its subcommands.",
    )

    @sub.callback(invoke_without_command=True)
    def _default(ctx: typer.Context) -> None:
        if ctx.invoked_subcommand is not None:
            return  # a source verb (e.g. list-stations) was invoked; don't also fetch
        _print_source_fetch(ctx, name)

    verbs = getattr(cls, "cli", None)
    if verbs is not None:
        src_cli = verbs()
        # Only plain commands are grafted under `source <name>`; a callback or sub-groups would be
        # dropped silently, so reject them loudly rather than surprise the source author.
        if src_cli.registered_callback is not None or len(src_cli.registered_groups) > 0:
            raise RuntimeError(
                f"source {name!r} cli() may only define plain commands, "
                "not a callback or sub-groups"
            )
        sub.registered_commands.extend(src_cli.registered_commands)
    return sub


def _print_source_fetch(ctx: typer.Context, name: str) -> None:
    """Fetch one source in isolation and pretty-print (rich) the raw data object it produces.

    Validates only the target source's config slice (not the whole config), then prints its produced
    data at full precision (SI, no display formatting). Only reached for a registered source; a
    registered-but-unconfigured source errors clearly.
    """
    cfg = _discover_sources(ctx)
    if name not in cfg.sources:
        raise typer.BadParameter(
            f"source {name!r} is registered but has no [sources.{name}] section; "
            f"configured: {sorted(cfg.sources)}"
        )
    source_cls, source_cfg = build_sources({name: cfg.sources[name]})[name]
    try:
        result = source_cls(source_cfg).fetch(datetime.now())
    except SourceError as exc:
        # A fetch failure is expected (source unavailable); report it cleanly, not as a traceback.
        raise typer.BadParameter(f"source {name!r} failed: {exc}") from exc
    console = Console()
    if result is None:
        console.print(f"source {name!r} returned no data")
        return
    console.print(result)


@dashboard_app.command("render")
def dashboard_render(
    ctx: typer.Context,
    names: NameOption = None,
    output_file: Annotated[
        Path | None,
        typer.Argument(help="Write a single dashboard's PNG here (needs one --name if several)."),
    ] = None,
) -> None:
    """Fetch live data once and render every dashboard's PNG via its pillow layout.

    Writes the raw rendered image (before Kindle post-processing) to each dashboard's output_path;
    run ``dashboard post-process`` to massage it for the device. Restrict to a subset with repeated
    ``--name``, and pass an output path to redirect a single dashboard elsewhere.
    """
    cfg = _config(ctx)
    selected = _selected_dashboards(cfg, names)
    if output_file is not None and len(selected) > 1:
        raise typer.BadParameter("output_file writes one dashboard; pass a single --name")
    data = pipeline.gather(cfg)  # one fetch, shared across all rendered dashboards
    for dash in selected.values():
        image = pipeline.render_raw(cfg, data, dash)
        path = output_file or dash.output_path
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path, format="PNG")


@dashboard_app.command("post-process")
def dashboard_post_process(
    ctx: typer.Context,
    input_file: Annotated[Path, typer.Argument(help="Existing PNG to massage for the Kindle.")],
    output_file: Annotated[Path, typer.Argument(help="Where to write the processed PNG.")],
    names: NameOption = None,
) -> None:
    """Fit, grayscale, and quantize an existing PNG into a Kindle-ready image (per dashboard)."""
    cfg = _config(ctx)
    _, dash = _one_dashboard(cfg, names)
    with Image.open(input_file) as image:
        png = post_process(
            image,
            width=dash.width,
            height=dash.height,
            gray_levels=dash.gray_levels,
            method=dash.post_process_method,
            rotate=dash.rotate,
        )
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_bytes(png)


def _config_path_from_argv(argv: list[str]) -> Path:
    """Scan argv for the global ``--config``/``-c`` value before typer parses it.

    Sources are mounted ahead of parsing (typer has no native dynamic subcommands), so we need the
    config path early to discover a config's ``plugins_path`` sources. Best-effort: unrecognized
    forms fall back to the default, and typer still does the real, authoritative parse afterward.
    """
    for i, arg in enumerate(argv):
        if arg in ("--config", "-c") and i + 1 < len(argv):
            return Path(argv[i + 1])
        if arg.startswith("--config="):
            return Path(arg.split("=", 1)[1])
        if arg.startswith("-c="):
            return Path(arg.split("=", 1)[1])
        if arg.startswith("-c") and len(arg) > 2:  # click's attached short form, e.g. -cconfig.toml
            return Path(arg[2:])
    return Path("config.toml")


def _invoked_command(argv: list[str]) -> str | None:
    """The top-level subcommand in ``argv`` (the first positional), skipping the global option.

    Best-effort, used only to decide whether to preload local plugin sources for a ``source``
    invocation; typer still does the authoritative parse.
    """
    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg in ("--config", "-c"):
            skip_next = True  # its value is the next token
            continue
        if arg.startswith("-"):
            continue
        return arg
    return None


def _wire_source_commands() -> None:
    """Mount a ``source <name>`` subcommand for every registered source not already mounted."""
    for name in registered_sources():
        if name in _wired_sources:
            continue
        if name in _RESERVED_SOURCE_NAMES:
            raise SourceError(
                f"source name {name!r} is reserved by the CLI; it would shadow `source {name}`"
            )
        cls = source_class(name)
        assert cls is not None  # registered_sources() just returned it, so it is in the registry
        source_app.add_typer(_build_source_typer(name, cls), name=name)
        _wired_sources.add(name)


# Mount the bundled sources at import so they're reachable through the app in tests and normal use;
# local (plugins_path) sources are mounted in run() (only for a `source` invocation).
_wire_source_commands()


def run() -> None:
    """Console-script entry point.

    Bundled sources are mounted at import. Local (``plugins_path``) sources are mounted here, but
    only for a ``source`` invocation: this both avoids coupling ``version``/``run``/etc. to the
    plugin dir and lets a broken plugin fail loud (a resolved ``--config`` whose plugins can't
    import raises here) rather than hiding as "no such command". A bad/missing config is swallowed;
    the source command reports it cleanly when it runs.
    """
    if _invoked_command(sys.argv[1:]) == "source":
        try:
            cfg = load_config(_config_path_from_argv(sys.argv[1:]))
        except Exception:
            cfg = None
        if cfg is not None:
            plugins.load_plugins(cfg.plugins_path)  # a broken plugin propagates (fail fast)
            _wire_source_commands()
    app()
