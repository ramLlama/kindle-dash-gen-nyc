"""Data the ``mta`` source produces (owned by the source that produces it)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Direction(StrEnum):
    """GTFS travel direction (nominal for east-west lines like the L)."""

    NORTH = "N"  # uptown / Bronx / Queens-bound
    SOUTH = "S"  # downtown / Brooklyn-bound


@dataclass(frozen=True, kw_only=True)
class TrainArrival:
    """A predicted train arrival at a station."""

    route: str  # line/route id, e.g. "L", "6"
    direction: Direction
    destination: str  # headsign, e.g. "8 Av"
    arrival: datetime  # predicted arrival time (aware UTC; a layout converts for display)


@dataclass(frozen=True, kw_only=True)
class StationBoard:
    """Upcoming arrivals for one named station (may merge several platforms).

    Arrivals are grouped by direction and sorted within each group. ``name`` is the canonical
    station name (what plugins match on); ``display_name`` optionally overrides what a layout shows.
    """

    name: str
    arrivals_by_direction: dict[Direction, list[TrainArrival]]
    display_name: str | None = None

    @property
    def label(self) -> str:
        """Name a layout should show: ``display_name`` if set, else the canonical ``name``."""
        return self.display_name if self.display_name is not None else self.name


@dataclass(frozen=True, kw_only=True)
class MtaData:
    """All station boards from one MTA fetch, wrapped as a single value.

    The subway source contributes this to ``DashboardData.source_data`` under its own type key
    (a bare ``list[StationBoard]`` can't key that dict); consumers read ``.boards``.
    """

    boards: list[StationBoard]
