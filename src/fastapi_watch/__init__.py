from .registry import HealthRegistry
from .probe_router import ProbeRouter
from .probes.route import RouteProbe
from .probes.websocket import WebSocketProbe
from .dashboard import render_dashboard

__all__ = ["HealthRegistry", "ProbeRouter", "RouteProbe", "WebSocketProbe", "render_dashboard"]
__version__ = "1.1.0"
