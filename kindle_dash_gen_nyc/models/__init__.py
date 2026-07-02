"""Domain data models produced by the sources and consumed by the renderer."""

from .dashboard import DashboardData
from .mta import Direction, StationBoard, TrainArrival
from .weather import HourlyForecast, Temperature, WeatherReport

__all__ = [
    "DashboardData",
    "Direction",
    "HourlyForecast",
    "StationBoard",
    "Temperature",
    "TrainArrival",
    "WeatherReport",
]
