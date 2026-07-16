"""Cross-source aggregate model consumed by the renderer.

Per-source data types (weather, subway, …) are owned by the source that produces them, under
``kindle_dash_gen.sources.builtins.<name>.model``. This package holds only the aggregate that
carries them into a render.
"""

from .dashboard_data import DashboardData

__all__ = ["DashboardData"]
