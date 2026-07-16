"""Aggregated dashboard data model, gathered from all sources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, kw_only=True)
class DashboardData:
    """Everything gathered for one dashboard render (presentation config stays out of it).

    ``source_data`` maps each source's produced data class to its instance (e.g. ``NwsData``
    -> an ``NwsData``, ``MtaData`` -> an ``MtaData``). A source that failed or had no data is simply
    absent, so a consumer looks its data up defensively: ``data.source_data.get(NwsData)``.
    """

    generated_at: datetime  # also used as "now" for ETA formatting
    source_data: dict[type, Any]
