"""The ``mta`` source client and config: real-time NYC subway arrivals via the nyct-gtfs feeds.

Each MTA feed covers a group of lines (e.g. one feed for N/Q/R/W). A station served by several line
groups needs several feeds, so this loads each distinct feed at most once per fetch. Platforms are
grouped under a station display name and merged into one board. This source owns its config schema
(``Platform``, ``Station``, ``MtaConfig``), so it is self-contained rather than reaching into
central config. The produced data type lives in :mod:`.model`.
"""

from __future__ import annotations

import asyncio
import csv
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from importlib.resources import files
from typing import Literal

import typer
from nyct_gtfs import NYCTFeed
from nyct_gtfs.trip import Trip
from pydantic import BaseModel, ConfigDict

from kindle_dash_gen.sources.registry import Source
from kindle_dash_gen.sources.toolkit import SourceError

from .model import Direction, MtaData, StationBoard, TrainArrival

# This source's own package (for bundled assets like the station lookup table).
_PACKAGE = "kindle_dash_gen.sources.builtins.mta"

# GTFS directions, in the order boards present them.
_DIRECTIONS = (Direction.NORTH, Direction.SOUTH)

# Line id -> feed URL, straight from nyct-gtfs (used to dedupe feeds across a station's lines).
_LINE_TO_URL: dict[str, str] = NYCTFeed._train_to_url

# Config direction -> the GTFS directions it targets.
_DIRECTION_SUFFIXES = {
    "north": (Direction.NORTH,),
    "south": (Direction.SOUTH,),
    "both": (Direction.NORTH, Direction.SOUTH),
}

# Loads one feed by URL. Async so a station's distinct feeds can be fetched concurrently; the
# default loader builds the feed without fetching, then awaits nyct-gtfs's async refresh.
FeedLoader = Callable[[str], Awaitable["NYCTFeed"]]


async def _default_feed_loader(url: str) -> NYCTFeed:
    """Build a feed without an immediate (blocking) fetch, then refresh it asynchronously."""
    feed = NYCTFeed(url, fetch_immediately=False)
    await feed.refresh_async()
    return feed


class Platform(BaseModel):
    """One physical platform: a GTFS stop id plus the lines that serve it."""

    model_config = ConfigDict(extra="forbid")

    lines: list[str]
    stop_id: str
    direction: Literal["north", "south", "both"] = "both"


class Station(BaseModel):
    """A display board: one or more platforms merged into per-direction arrival lists.

    Several platforms under one station are merged (e.g. the N/Q/R/W and the L platforms of
    "Union Sq"). Boards carry every upcoming arrival, sorted; how many to show is a render-time
    decision made by the layout (see docs/plugins.md), not a data-collection cap.
    """

    model_config = ConfigDict(extra="forbid")

    platforms: list[Platform]
    # Label a layout shows instead of the station's name (the config key). The key stays the
    # canonical name that plugins match on (e.g. home_mta_map), so renaming the display never
    # breaks that match. Unset means show the name as-is.
    display_name: str | None = None


class MtaError(SourceError):
    """Raised when subway data cannot be fetched."""


class MtaClient:
    """Loads the needed GTFS-realtime feeds and builds a merged board per station name."""

    def __init__(self, stations: dict[str, Station], feed_loader: FeedLoader | None = None) -> None:
        self._stations = stations
        self._feed_loader = feed_loader or _default_feed_loader

    async def fetch(self, now: datetime | None = None) -> list[StationBoard]:
        """Load every needed feed once and build a board for each station name."""
        now = now or datetime.now(UTC)
        feeds = await self._load_feeds()
        return [self._board(name, station, feeds, now) for name, station in self._stations.items()]

    async def _load_feeds(self) -> dict[str, NYCTFeed]:
        urls = [
            *{
                url
                for station in self._stations.values()
                for platform in station.platforms
                for url in _feed_urls(platform)
            }
        ]
        try:
            feeds = await asyncio.gather(*(self._feed_loader(url) for url in urls))
        except Exception as exc:  # network / protobuf-parse failures from nyct-gtfs
            raise MtaError("failed to load MTA feed") from exc
        return dict(zip(urls, feeds, strict=True))

    def _board(
        self, name: str, station: Station, feeds: dict[str, NYCTFeed], now: datetime
    ) -> StationBoard:
        # Merge every platform's arrivals, then group by direction and sort. No truncation here:
        # boards carry every upcoming arrival so a layout can pick what to show (e.g. next per
        # line, or the soonest few) at render time.
        by_direction: dict[Direction, list[TrainArrival]] = defaultdict(list)
        for platform in station.platforms:
            for arrival in _platform_arrivals(platform, feeds, now):
                by_direction[arrival.direction].append(arrival)
        # Canonical N-then-S order, each sorted ascending by arrival time.
        ordered = {
            d: sorted(by_direction[d], key=lambda a: a.arrival)
            for d in _DIRECTIONS
            if d in by_direction
        }
        return StationBoard(
            name=name, arrivals_by_direction=ordered, display_name=station.display_name
        )


def _platform_arrivals(
    platform: Platform, feeds: dict[str, NYCTFeed], now: datetime
) -> list[TrainArrival]:
    """Every upcoming arrival for one platform."""
    target_ids = _target_stop_ids(platform)
    arrivals: list[TrainArrival] = []
    for url in _feed_urls(platform):
        trips = feeds[url].filter_trips(
            line_id=platform.lines, headed_for_stop_id=target_ids, underway=True
        )
        for trip in trips:
            arrival = _arrival_at(trip, target_ids)
            if arrival is None or arrival < now:  # missing or already departed
                continue
            arrivals.append(
                TrainArrival(
                    route=trip.route_id,
                    direction=Direction(trip.direction),
                    destination=trip.headsign_text,
                    arrival=arrival,
                )
            )
    return arrivals


def _feed_urls(platform: Platform) -> set[str]:
    try:
        return {_LINE_TO_URL[line] for line in platform.lines}
    except KeyError as exc:
        raise MtaError(f"unknown subway line {exc}") from exc


def _target_stop_ids(platform: Platform) -> list[str]:
    return [platform.stop_id + suffix for suffix in _DIRECTION_SUFFIXES[platform.direction]]


def _arrival_at(trip: Trip, target_ids: list[str]) -> datetime | None:
    """The predicted arrival at the first matching target stop in the trip's path, as aware UTC.

    ``target_ids`` are the N/S variants of a single platform's stop, so a given trip (which
    runs one direction) matches at most one.

    nyct-gtfs builds its arrival with a bare ``datetime.fromtimestamp(epoch)``, i.e. the *host's*
    local wall clock with no tzinfo. Feeding that straight to ``astimezone`` recovers the original
    instant exactly, whatever the host zone happens to be: ``astimezone`` reads a naive value as
    host-local, which is precisely the zone ``fromtimestamp`` rendered it in, so the two cancel.
    (It stays exact across a DST fall-back too, since ``fromtimestamp`` sets ``fold`` on the
    ambiguous hour and ``astimezone`` honors it.) That keeps the app free of nyct-gtfs internals
    while still not depending on the host being set to America/New_York.
    """
    for stop in trip.stop_time_updates:
        if stop.stop_id in target_ids:
            arrival = stop.arrival
            return None if arrival is None else arrival.astimezone(UTC)
    return None


class MtaConfig(BaseModel):
    """Config for the ``[sources.mta]`` table."""

    model_config = ConfigDict(extra="forbid")

    stations: dict[str, Station]  # display name -> station board


class MtaSource(Source[MtaConfig]):
    """The ``mta`` source: fetches an :class:`MtaData` (one board per configured station)."""

    Config = MtaConfig

    def __init__(self, config: MtaConfig) -> None:
        self._client = MtaClient(config.stations)

    async def fetch(self, now: datetime) -> MtaData:
        return MtaData(boards=await self._client.fetch(now))

    @classmethod
    def cli(cls) -> typer.Typer:
        """Source-specific CLI verbs, mounted by the CLI under ``source mta``.

        An optional hook: a source may expose its own subcommands (this one ships a station-lookup
        helper). Sources without a ``cli`` just get the default ``source <name>`` fetch behavior.
        The CLI grafts the returned app's commands under ``source mta``; only plain commands are
        supported (no callback or sub-groups).
        """
        app = typer.Typer()

        @app.command("list-stations")
        def list_stations() -> None:
            """Dump every MTA station (stop id, routes, name) — grep it to fill in config."""
            data = files(_PACKAGE).joinpath("assets/stations.csv")
            with data.open() as f:
                rows = [
                    (r["stop_id"], ",".join(r["routes"].split()), r["name"])
                    for r in csv.DictReader(f)
                ]
            id_width = max(len(stop_id) for stop_id, _, _ in rows)
            routes_width = max(len(routes) for _, routes, _ in rows)
            for stop_id, routes, name in rows:
                typer.echo(f"{stop_id:<{id_width}}  {routes:<{routes_width}}  {name}")

        return app
