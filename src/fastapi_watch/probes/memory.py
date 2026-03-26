from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe


class MemoryProbe(BaseProbe):
    """Always-passing probe. Useful for testing or as a no-op placeholder."""

    def __init__(self, name: str = "memory") -> None:
        self.name = name

    async def check(self) -> ProbeResult:
        return ProbeResult(name=self.name, status=ProbeStatus.HEALTHY, latency_ms=0.0)
