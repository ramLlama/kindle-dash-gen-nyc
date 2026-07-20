"""Shared test fixtures."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator

import pytest


@pytest.fixture
def host_timezone() -> Iterator[Callable[[str], None]]:
    """Set the *process* timezone; the original is restored when the test ends.

    Several invariants of the aware-UTC design are "the output must not depend on where this runs",
    which can only be tested by actually moving the host zone. ``TZ`` + ``tzset()`` is
    process-global state, so the restore matters: a leak would surface as an unrelated test failing
    depending on pytest's ordering. Yielding a setter rather than taking the zone as a param lets
    one test render under several zones and compare, which is the shape these tests need. Popping
    ``TZ`` (rather than setting it back to "") restores the original /etc/localtime behavior when
    it started unset.
    """
    saved = os.environ.get("TZ")

    def set_timezone(tz: str) -> None:
        os.environ["TZ"] = tz
        time.tzset()

    yield set_timezone

    # Teardown runs even when the test fails, so the zone cannot leak into the next test.
    if saved is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = saved
    time.tzset()
