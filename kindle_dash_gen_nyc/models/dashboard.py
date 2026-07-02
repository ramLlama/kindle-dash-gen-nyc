"""Aggregated dashboard data model, gathered from all sources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .mta import StationBoard
from .weather import WeatherReport


@dataclass(frozen=True, kw_only=True)
class DashboardData:
    """Everything gathered for one dashboard render (presentation config stays out of it)."""

    weather: WeatherReport | None  # None if the weather fetch failed
    boards: list[StationBoard]  # one per configured station
    generated_at: datetime  # also used as "now" for ETA formatting
