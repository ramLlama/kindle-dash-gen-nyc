"""Tests for the MTA subway source."""

from datetime import datetime, timedelta

import pytest

from kindle_dash_gen_nyc.config import Platform, Station
from kindle_dash_gen_nyc.models import Direction
from kindle_dash_gen_nyc.sources.mta import MtaClient, MtaError

NOW = datetime(2026, 7, 1, 12, 0, 0)


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
    return FakeTrip(route, direction, dest, [FakeStop(stop_id, NOW + timedelta(minutes=minutes))])


def _platform(**kw) -> Platform:
    defaults = dict(lines=["N", "Q", "R", "W"], stop_id="R20", direction="both")
    defaults.update(kw)
    return Platform(**defaults)


def _station(platforms: list[Platform] | None = None) -> Station:
    return Station(platforms=platforms or [_platform()])


def _loader_for(trips: list[FakeTrip]):
    calls: list[str] = []

    def loader(url: str) -> FakeFeed:
        calls.append(url)
        return FakeFeed(trips)

    return loader, calls


def _minutes(arrivals) -> list[int]:
    return [round((a.arrival - NOW).total_seconds() / 60) for a in arrivals]


def test_arrivals_grouped_and_sorted_per_direction() -> None:
    trips = [
        _trip("Q", "N", "96 St", "R20N", 7),
        _trip("N", "N", "Astoria", "R20N", 3),  # out of order on purpose
        _trip("R", "N", "Forest Hills", "R20N", 12),
        _trip("R", "S", "Bay Ridge", "R20S", 5),
    ]
    loader, _ = _loader_for(trips)
    boards = MtaClient({"Union Sq": _station()}, feed_loader=loader).fetch(now=NOW)

    board = boards[0]
    assert board.name == "Union Sq"
    assert list(board.arrivals_by_direction.keys()) == [Direction.NORTH, Direction.SOUTH]
    # No truncation at fetch: every upcoming arrival is kept, sorted ascending per direction.
    assert _minutes(board.arrivals_by_direction["N"]) == [3, 7, 12]
    assert _minutes(board.arrivals_by_direction["S"]) == [5]


def test_platforms_merge_within_direction() -> None:
    trips = [
        _trip("Q", "N", "96 St", "R20N", 6),
        _trip("L", "N", "8 Av", "L03N", 2),  # different platform, same station name
    ]
    loader, calls = _loader_for(trips)
    platforms = [_platform(), _platform(lines=["L"], stop_id="L03")]
    boards = MtaClient({"Union Sq": _station(platforms)}, feed_loader=loader).fetch(now=NOW)

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
    boards = MtaClient(stations, feed_loader=loader).fetch(now=NOW)
    # Merged, sorted N = [2, 3, 5, 7]; nothing dropped at fetch.
    assert _minutes(boards[0].arrivals_by_direction["N"]) == [2, 3, 5, 7]


def test_past_arrivals_excluded() -> None:
    trips = [
        _trip("N", "N", "Astoria", "R20N", -2),  # already departed
        _trip("Q", "N", "96 St", "R20N", 4),
    ]
    loader, _ = _loader_for(trips)
    boards = MtaClient({"Union Sq": _station()}, feed_loader=loader).fetch(now=NOW)
    assert _minutes(boards[0].arrivals_by_direction["N"]) == [4]


def test_direction_north_only_targets_north_stop() -> None:
    trips = [
        _trip("N", "N", "Astoria", "R20N", 3),
        _trip("R", "S", "Bay Ridge", "R20S", 5),  # excluded: not headed to R20N
    ]
    loader, _ = _loader_for(trips)
    stations = {"Union Sq": _station([_platform(direction="north")])}
    boards = MtaClient(stations, feed_loader=loader).fetch(now=NOW)
    assert list(boards[0].arrivals_by_direction.keys()) == [Direction.NORTH]


def test_each_feed_loaded_once() -> None:
    # N/Q/R/W all share one feed URL, so only one feed should be loaded.
    loader, calls = _loader_for([])
    MtaClient({"Union Sq": _station()}, feed_loader=loader).fetch(now=NOW)
    assert len(calls) == 1


def test_unknown_line_raises() -> None:
    loader, _ = _loader_for([])
    stations = {"Nowhere": _station([_platform(lines=["ZZ"])])}
    with pytest.raises(MtaError):
        MtaClient(stations, feed_loader=loader).fetch(now=NOW)
