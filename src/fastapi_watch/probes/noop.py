from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe


class NoOpProbe(BaseProbe):
    """Always-passing probe. Useful for testing or as a no-op placeholder.

    Always returns ``HEALTHY`` with zero latency. Does not make any external
    calls. Commonly used as a base class for custom probes in tests.

    Example::

        # Placeholder while a real probe is being wired up
        registry.add(NoOpProbe(name="payments"))

        # As a base for a test stub
        class FailingProbe(NoOpProbe):
            async def check(self):
                result = await super().check()
                return result.model_copy(update={"status": ProbeStatus.UNHEALTHY})
    """

    def __init__(self, name: str = "noop", poll_interval_ms: int | None = None) -> None:
        self.name = name
        self.poll_interval_ms = poll_interval_ms

    async def check(self) -> ProbeResult:
        return ProbeResult(name=self.name, status=ProbeStatus.HEALTHY, latency_ms=0.0)
