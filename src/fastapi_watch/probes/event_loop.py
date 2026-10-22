import asyncio

from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe


class EventLoopProbe(BaseProbe):
    """Measures asyncio event loop lag.

    High lag means the event loop is blocked — sync code running on the async
    path, a CPU-bound coroutine, or a slow coroutine that isn't yielding.
    FastAPI becomes unresponsive before any infrastructure probe would detect
    the problem.

    A single measurement taken at check time only runs once the loop is free
    again, so it cannot see a stall that happened between polls.  Instead, the
    first check starts a lightweight background sampler that repeatedly sleeps
    for ``sample_interval_ms`` and records how late it wakes up; each check
    reports the worst lag seen since the previous check (``lag_ms``) alongside
    a fresh immediate measurement (``current_lag_ms``).  :meth:`aclose` (called
    by the registry at shutdown) stops the sampler.

    Args:
        name: Probe name (default ``"event_loop"``).
        warn_ms: Lag threshold in ms above which the probe is DEGRADED
            (default ``5.0``).
        fail_ms: Lag threshold in ms above which the probe is UNHEALTHY
            (default ``20.0``).
        poll_interval_ms: Per-probe poll interval override.
        sample_interval_ms: How often the background sampler measures lag
            (default ``100``).  Lower values catch shorter stalls at slightly
            higher (still negligible) cost.  Must be at least ``1``: a
            shorter sleep would keep the loop spinning.

    Raises:
        ValueError: If ``sample_interval_ms`` is below ``1``.

    Example::

        registry.add(EventLoopProbe(warn_ms=2.0, fail_ms=10.0))
    """

    def __init__(
        self,
        name: str = "event_loop",
        warn_ms: float = 5.0,
        fail_ms: float = 20.0,
        poll_interval_ms: int | None = None,
        sample_interval_ms: float = 100.0,
    ) -> None:
        if not sample_interval_ms >= 1:
            raise ValueError(f"sample_interval_ms must be at least 1, got {sample_interval_ms!r}")
        self.name = name
        self.warn_ms = warn_ms
        self.fail_ms = fail_ms
        self.poll_interval_ms = poll_interval_ms
        self.sample_interval_ms = sample_interval_ms
        self._worst_lag_ms = 0.0
        self._sampler: asyncio.Task | None = None

    async def _sample(self) -> None:
        """Record how late each timed sleep wakes up, forever."""
        loop = asyncio.get_running_loop()
        interval_s = self.sample_interval_ms / 1000
        while True:
            start = loop.time()
            await asyncio.sleep(interval_s)
            lag_ms = (loop.time() - start - interval_s) * 1000
            if lag_ms > self._worst_lag_ms:
                self._worst_lag_ms = lag_ms

    def _ensure_sampler(self) -> None:
        """Start the sampler on the running loop if it is not already running there."""
        loop = asyncio.get_running_loop()
        sampler = self._sampler
        if sampler is None or sampler.done() or sampler.get_loop() is not loop:
            self._sampler = loop.create_task(self._sample(), name=f"fastapi-watch-{self.name}-sampler")

    async def aclose(self) -> None:
        """Stop the background sampler.  A later :meth:`check` starts a new one."""
        sampler, self._sampler = self._sampler, None
        if sampler is None or sampler.done():
            return
        sampler_loop = sampler.get_loop()
        if sampler_loop is asyncio.get_running_loop():
            sampler.cancel()
            # wait() rather than awaiting the task: the sampler's CancelledError
            # stays inside it, while a cancellation of aclose() itself still propagates.
            await asyncio.wait({sampler})
            return
        try:
            sampler_loop.call_soon_threadsafe(sampler.cancel)
        except RuntimeError:
            pass  # that loop is closed, and the sampler with it

    async def check(self) -> ProbeResult:
        self._ensure_sampler()
        loop = asyncio.get_running_loop()
        before = loop.time()
        await asyncio.sleep(0)
        current_lag_ms = round((loop.time() - before) * 1000, 3)

        # Worst lag since the previous check; the window restarts with each check.
        lag_ms = round(max(self._worst_lag_ms, current_lag_ms), 3)
        self._worst_lag_ms = 0.0

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
                "current_lag_ms": current_lag_ms,
                "warn_ms": self.warn_ms,
                "fail_ms": self.fail_ms,
            },
        )
