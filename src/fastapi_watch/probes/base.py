from abc import ABC, abstractmethod
from collections import deque

from ..models import ProbeResult


def _update_ema(current: float | None, value: float, alpha: float) -> float:
    """Return an updated exponential moving average."""
    return value if current is None else alpha * value + (1 - alpha) * current


def _calc_p95(window: deque) -> float | None:
    """Return the 95th-percentile value from a deque, or None if empty."""
    if not window:
        return None
    s = sorted(window)
    return round(s[max(0, int(len(s) * 0.95) - 1)], 2)


class BaseProbe(ABC):
    """Abstract base class for fastapi-watch health probes.

    Subclass this and implement :meth:`check` to create a custom probe.
    Set the :attr:`name` class or instance attribute to identify this probe
    in health reports.
    """

    name: str = "unnamed"
    timeout: float | None = None  # seconds; None means no timeout
    poll_interval_ms: int | None = None
    circuit_breaker_threshold: int | None = None
    circuit_breaker_cooldown_ms: int | None = None

    @abstractmethod
    async def check(self) -> ProbeResult:
        """Execute the health check and return a :class:`~fastapi_watch.models.ProbeResult`."""
