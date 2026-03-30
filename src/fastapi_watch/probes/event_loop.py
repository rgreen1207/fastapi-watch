import asyncio

from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe


class EventLoopProbe(BaseProbe):
    """Measures asyncio event loop lag.

    Schedules an immediate wakeup via ``await asyncio.sleep(0)`` and measures
    how long it actually takes.  High lag means the event loop is blocked —
    sync code running on the async path, a CPU-bound coroutine, or a
    slow coroutine that isn't yielding.  FastAPI becomes unresponsive before
    any infrastructure probe would detect the problem.

    Args:
        name: Probe name (default ``"event_loop"``).
        warn_ms: Lag threshold in ms above which the probe is DEGRADED
            (default ``5.0``).
        fail_ms: Lag threshold in ms above which the probe is UNHEALTHY
            (default ``20.0``).
        poll_interval_ms: Per-probe poll interval override.

    Example::

        registry.add(EventLoopProbe(warn_ms=2.0, fail_ms=10.0))
    """

    def __init__(
        self,
        name: str = "event_loop",
        warn_ms: float = 5.0,
        fail_ms: float = 20.0,
        poll_interval_ms: int | None = None,
    ) -> None:
        self.name = name
        self.warn_ms = warn_ms
        self.fail_ms = fail_ms
        self.poll_interval_ms = poll_interval_ms

    async def check(self) -> ProbeResult:
        loop = asyncio.get_running_loop()
        before = loop.time()
        await asyncio.sleep(0)
        lag_ms = round((loop.time() - before) * 1000, 3)

        if lag_ms >= self.fail_ms:
            status = ProbeStatus.UNHEALTHY
        elif lag_ms >= self.warn_ms:
            status = ProbeStatus.DEGRADED
        else:
            status = ProbeStatus.HEALTHY

        return ProbeResult(
            name=self.name,
            status=status,
            latency_ms=lag_ms,
            details={
                "lag_ms": lag_ms,
                "warn_ms": self.warn_ms,
                "fail_ms": self.fail_ms,
            },
        )
