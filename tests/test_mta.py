"""Tests for the MTA subway source."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from kindle_dash_gen.sources.builtins.mta.model import Direction, MtaData, StationBoard
from kindle_dash_gen.sources.builtins.mta.source import MtaClient, MtaError, Platform, Station

NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


def _as_nyct_gtfs_would(instant: datetime) -> datetime:
    """Render ``instant`` the way nyct-gtfs does: naive, in the *host's* local zone.

    Its arrival property is a bare ``datetime.fromtimestamp(epoch)``, so mirroring that here keeps
    the client's aware-UTC conversion under test rather than handing it an already-aware value.
    Building the fixture from the host zone is also what keeps these assertions independent of it.
    """
    return instant.astimezone().replace(tzinfo=None)


class FakeStop:
    def __init__(self, stop_id: str, arrival: datetime | None) -> None:
        self.stop_id = stop_id
        self.arrival = arrival


class FakeTrip:
    def __init__(self, route_id: str, direction: str, headsign: str, stops: list[FakeStop]) -> None:
        self.route_id = route_id
        self.direction = direction
        self.headsign_text = headsign
        self.underway = True
        self.stop_time_updates = stops

    def headed_to_stop(self, stop_id: str) -> bool:
        return any(s.stop_id == stop_id for s in self.stop_time_updates)


class FakeFeed:
    """Minimal stand-in replicating the filter_trips behaviour the client relies on."""

    def __init__(self, trips: list[FakeTrip]) -> None:
        self._trips = trips

    def filter_trips(self, line_id=None, headed_for_stop_id=None, underway=None):
        result = []
        for trip in self._trips:
            if line_id is not None and trip.route_id not in line_id:
                continue
            if underway is not None and trip.underway != underway:
                continue
            if headed_for_stop_id is not None and not any(
                trip.headed_to_stop(s) for s in headed_for_stop_id
            ):
                continue
            result.append(trip)
        return result


def _trip(route: str, direction: str, dest: str, stop_id: str, minutes: float) -> FakeTrip:
    arrival = _as_nyct_gtfs_would(NOW + timedelta(minutes=minutes))
    return FakeTrip(route, direction, dest, [FakeStop(stop_id, arrival)])


def _platform(**kw) -> Platform:
    defaults = dict(lines=["N", "Q", "R", "W"], stop_id="R20", direction="both")
    defaults.update(kw)
    return Platform(**defaults)


def _station(platforms: list[Platform] | None = None) -> Station:
    return Station(platforms=platforms or [_platform()])


def _loader_for(trips: list[FakeTrip]):
    calls: list[str] = []

    async def loader(url: str) -> FakeFeed:
        calls.append(url)
        return FakeFeed(trips)

    return loader, calls


def _minutes(arrivals) -> list[int]:
    return [round((a.arrival - NOW).total_seconds() / 60) for a in arrivals]


def _fetch(stations, loader):
    """Build the client and drive its async fetch to completion (tests stay sync)."""
    return asyncio.run(MtaClient(stations, feed_loader=loader).fetch(now=NOW))


def test_arrivals_grouped_and_sorted_per_direction() -> None:
    trips = [
        _trip("Q", "N", "96 St", "R20N", 7),
        _trip("N", "N", "Astoria", "R20N", 3),  # out of order on purpose
        _trip("R", "N", "Forest Hills", "R20N", 12),
        _trip("R", "S", "Bay Ridge", "R20S", 5),
    ]
    loader, _ = _loader_for(trips)
    boards = _fetch({"Union Sq": _station()}, loader)

    board = boards[0]
    assert board.name == "Union Sq"
    assert list(board.arrivals_by_direction.keys()) == [Direction.NORTH, Direction.SOUTH]
    # No truncation at fetch: every upcoming arrival is kept, sorted ascending per direction.
    assert _minutes(board.arrivals_by_direction["N"]) == [3, 7, 12]
    assert _minutes(board.arrivals_by_direction["S"]) == [5]


def test_display_name_carried_onto_board() -> None:
    # A station's optional display_name flows to the board's `label`; `name` stays canonical.
    loader, _ = _loader_for([])
    station = Station(display_name="59 St - CC", platforms=[_platform()])
    board = _fetch({"59 St-Columbus Circle": station}, loader)[0]
    assert board.name == "59 St-Columbus Circle"  # match key unchanged
    assert board.label == "59 St - CC"


def test_label_falls_back_to_name_without_display_name() -> None:
    loader, _ = _loader_for([])
    board = _fetch({"Union Sq": _station()}, loader)[0]
    assert board.display_name is None
    assert board.label == "Union Sq"


def test_platforms_merge_within_direction() -> None:
    trips = [
        _trip("Q", "N", "96 St", "R20N", 6),
        _trip("L", "N", "8 Av", "L03N", 2),  # different platform, same station name
    ]
    loader, calls = _loader_for(trips)
    platforms = [_platform(), _platform(lines=["L"], stop_id="L03")]
    boards = _fetch({"Union Sq": _station(platforms)}, loader)

    assert len(boards) == 1
    assert boards[0].name == "Union Sq"
    # Both platforms' northbound trains merge into one sorted "N" group.
    assert [a.route for a in boards[0].arrivals_by_direction["N"]] == ["L", "Q"]
    assert len(calls) == 2  # NQRW and L are distinct feeds


def test_platforms_merge_fully_without_truncation() -> None:
    # Two platforms, both northbound; all arrivals merge into one sorted group (no cap).
    trips = [
        _trip("Q", "N", "96 St", "R20N", 3),
        _trip("N", "N", "Astoria", "R20N", 7),
        _trip("L", "N", "8 Av", "L03N", 2),
        _trip("L", "N", "8 Av", "L03N", 5),
    ]
    loader, _ = _loader_for(trips)
    platforms = [_platform(), _platform(lines=["L"], stop_id="L03")]
    stations = {"Union Sq": _station(platforms)}
    boards = _fetch(stations, loader)
    # Merged, sorted N = [2, 3, 5, 7]; nothing dropped at fetch.
    assert _minutes(boards[0].arrivals_by_direction["N"]) == [2, 3, 5, 7]


def test_past_arrivals_excluded() -> None:
    trips = [
        _trip("N", "N", "Astoria", "R20N", -2),  # already departed
        _trip("Q", "N", "96 St", "R20N", 4),
    ]
    loader, _ = _loader_for(trips)
    boards = _fetch({"Union Sq": _station()}, loader)
    assert _minutes(boards[0].arrivals_by_direction["N"]) == [4]


def test_direction_north_only_targets_north_stop() -> None:
    trips = [
        _trip("N", "N", "Astoria", "R20N", 3),
        _trip("R", "S", "Bay Ridge", "R20S", 5),  # excluded: not headed to R20N
    ]
    loader, _ = _loader_for(trips)
    stations = {"Union Sq": _station([_platform(direction="north")])}
    boards = _fetch(stations, loader)
    assert list(boards[0].arrivals_by_direction.keys()) == [Direction.NORTH]


def test_each_feed_loaded_once() -> None:
    # N/Q/R/W all share one feed URL, so only one feed should be loaded.
    loader, calls = _loader_for([])
    _fetch({"Union Sq": _station()}, loader)
    assert len(calls) == 1


def test_unknown_line_raises() -> None:
    loader, _ = _loader_for([])
    stations = {"Nowhere": _station([_platform(lines=["ZZ"])])}
    with pytest.raises(MtaError):
        _fetch(stations, loader)


def test_mta_data_wraps_station_boards() -> None:
    # MtaData is the single value the MTA source contributes to DashboardData.source_data;
    # it just carries the per-station boards under one type key.
    board = StationBoard(name="Union Sq", arrivals_by_direction={})
    boards = MtaData(boards=[board])
    assert boards.boards == [board]


def test_arrivals_are_aware_utc_regardless_of_host_timezone(host_timezone) -> None:
    """nyct-gtfs hands back naive host-local times; the client must recover the true instant.

    The same feed rendered on a Los Angeles box and a Kolkata box has to yield identical arrival
    instants, otherwise a dashboard's clock would depend on where the generator happens to run.
    """
    instant = datetime(2026, 7, 1, 12, 5, 0, tzinfo=UTC)

    def arrivals_under(tz: str) -> list[datetime]:
        # Rebuild the fixture under this zone, exactly as nyct-gtfs would render it there.
        host_timezone(tz)
        trip = FakeTrip("N", "N", "Astoria", [FakeStop("R20N", _as_nyct_gtfs_would(instant))])
        loader, _ = _loader_for([trip])
        boards = _fetch({"Union Sq": _station()}, loader)
        return [a.arrival for a in boards[0].arrivals_by_direction[Direction.NORTH]]

    west, east = arrivals_under("America/Los_Angeles"), arrivals_under("Asia/Kolkata")
    assert west == east == [instant]
    assert all(a.tzinfo is not None and a.utcoffset().total_seconds() == 0 for a in west)
