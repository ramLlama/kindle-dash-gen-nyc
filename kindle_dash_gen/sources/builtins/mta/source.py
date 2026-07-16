"""The ``mta`` source client and config: real-time NYC subway arrivals via the nyct-gtfs feeds.

Each MTA feed covers a group of lines (e.g. one feed for N/Q/R/W). A station served by several line
groups needs several feeds, so this loads each distinct feed at most once per fetch. Platforms are
grouped under a station display name and merged into one board. This source owns its config schema
(``Platform``, ``Station``, ``MtaConfig``), so it is self-contained rather than reaching into
central config. The produced data type lives in :mod:`.model`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from nyct_gtfs import NYCTFeed
from nyct_gtfs.trip import Trip
from pydantic import BaseModel, ConfigDict

from kindle_dash_gen.sources.registry import Source
from kindle_dash_gen.sources.toolkit import SourceError

from .model import Direction, MtaData, StationBoard, TrainArrival

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

FeedLoader = Callable[[str], "NYCTFeed"]


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
        self._feed_loader = feed_loader or (lambda url: NYCTFeed(url))

    def fetch(self, now: datetime | None = None) -> list[StationBoard]:
        """Load every needed feed once and build a board for each station name."""
        now = now or datetime.now()
        feeds = self._load_feeds()
        return [self._board(name, station, feeds, now) for name, station in self._stations.items()]

    def _load_feeds(self) -> dict[str, NYCTFeed]:
        urls = {
            url
            for station in self._stations.values()
            for platform in station.platforms
            for url in _feed_urls(platform)
        }
        try:
            return {url: self._feed_loader(url) for url in urls}
        except Exception as exc:  # network / protobuf-parse failures from nyct-gtfs
            raise MtaError("failed to load MTA feed") from exc

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
    """The predicted arrival at the first matching target stop in the trip's path.

    ``target_ids`` are the N/S variants of a single platform's stop, so a given trip (which
    runs one direction) matches at most one.
    """
    for stop in trip.stop_time_updates:
        if stop.stop_id in target_ids:
            return stop.arrival
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

    def fetch(self, now: datetime) -> MtaData:
        return MtaData(boards=self._client.fetch(now))
