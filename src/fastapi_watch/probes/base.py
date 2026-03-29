from abc import ABC, abstractmethod

from ..models import ProbeResult


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
