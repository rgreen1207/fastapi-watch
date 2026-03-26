import asyncio
import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .models import HealthReport, ProbeResult, ProbeStatus
from .probes.base import BaseProbe

logger = logging.getLogger(__name__)

_MIN_POLL_INTERVAL_MS = 1000


def _normalize_interval(ms: int | None) -> int | None:
    """Validate and normalize a poll interval.

    - ``None`` or ``0``  → ``None``  (single-fetch mode)
    - ``1 – 999``        → ``1000``  (clamped to minimum)
    - ``>= 1000``        → used as-is
    """
    if ms is None or ms == 0:
        return None
    if ms < _MIN_POLL_INTERVAL_MS:
        logger.warning(
            "poll_interval_ms %d is below the minimum of %d ms; clamping to %d ms",
            ms,
            _MIN_POLL_INTERVAL_MS,
            _MIN_POLL_INTERVAL_MS,
        )
        return _MIN_POLL_INTERVAL_MS
    return ms


class HealthRegistry:
    """Register health probes and mount health endpoints on a FastAPI app.

    Mounted routes (all customisable via *prefix*):

    - ``GET /health/live``   — liveness; always 200
    - ``GET /health/ready``  — readiness; 200 if all probes pass, 503 otherwise
    - ``GET /health/status`` — full probe detail; 200 / 207 (Multi-Status)

    Args:
        app: FastAPI application.
        prefix: URL prefix for health routes (default ``/health``).
        tags: OpenAPI tags applied to all three routes.
        poll_interval_ms: How often (in milliseconds) to re-run all probes in
            the background and cache the results.  Defaults to ``60000`` (60 s).
            Pass ``0`` or ``None`` to disable polling — each HTTP request will
            run the probes on demand.  Values between 1 and 999 are clamped to
            1000 ms.

    Example::

        registry = HealthRegistry(app)
        registry.add(RedisProbe(url="redis://localhost"))
        registry.add(HttpProbe(url="https://api.example.com/health"))

        # Change the interval at runtime (e.g. from a config endpoint):
        registry.set_poll_interval(30_000)   # every 30 s
        registry.set_poll_interval(0)        # back to single-fetch mode
    """

    def __init__(
        self,
        app: FastAPI,
        prefix: str = "/health",
        tags: list[str] | None = None,
        poll_interval_ms: int | None = 60_000,
    ) -> None:
        self.app = app
        self.prefix = prefix
        self._probes: list[BaseProbe] = []
        self._poll_interval_ms: int | None = _normalize_interval(poll_interval_ms)
        self._cached_results: list[ProbeResult] | None = None
        self._poll_task: asyncio.Task | None = None
        self._cache_lock = asyncio.Lock()

        app.router.on_startup.append(self._start_polling)
        app.router.on_shutdown.append(self._stop_polling)

        self._register_routes(tags or ["health"])

    def add(self, probes: BaseProbe | list[BaseProbe]) -> "HealthRegistry":
        """Add a probe or list of probes to the registry. Returns ``self`` for chaining.

        Silently skips any probe that is already registered (identity check).
        """
        for probe in (probes if isinstance(probes, list) else [probes]):
            if probe not in self._probes:
                self._probes.append(probe)
        return self

    def set_poll_interval(self, ms: int | None) -> None:
        """Update the polling interval at runtime.

        Args:
            ms: New interval in milliseconds.  Pass ``0`` or ``None`` to switch
                to single-fetch mode (probes run on each HTTP request).  Values
                between 1 and 999 are clamped to 1000 ms.

        If the application event loop is already running the change takes effect
        immediately: the old polling task is cancelled and a new one is started
        (or not, in single-fetch mode).
        """
        self._poll_interval_ms = _normalize_interval(ms)
        if self._poll_interval_ms is None:
            self._cached_results = None
        self._cancel_poll_task()
        if self._poll_interval_ms is not None:
            try:
                loop = asyncio.get_running_loop()
                self._poll_task = loop.create_task(self._poll_loop())
            except RuntimeError:
                pass  # _start_polling will create the task on app startup.

    async def run_all(self) -> list[ProbeResult]:
        """Run all probes concurrently and return their results.

        A probe that raises an unhandled exception is recorded as unhealthy
        rather than propagating the exception.
        """
        if not self._probes:
            return []

        async def _safe_check(probe: BaseProbe) -> ProbeResult:
            try:
                return await probe.check()
            except Exception as exc:
                logger.exception("Probe %r raised an exception", probe.name)
                return ProbeResult(
                    name=probe.name,
                    status=ProbeStatus.UNHEALTHY,
                    error=f"{type(exc).__name__}: {exc}",
                )

        return list(await asyncio.gather(*(_safe_check(p) for p in self._probes)))

    async def _start_polling(self) -> None:
        if self._poll_interval_ms is not None:
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def _stop_polling(self) -> None:
        task = self._poll_task
        self._cancel_poll_task()
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _cancel_poll_task(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None

    async def _poll_loop(self) -> None:
        while True:
            self._cached_results = await self.run_all()
            await asyncio.sleep(self._poll_interval_ms / 1000)

    async def _get_results(self) -> list[ProbeResult]:
        if self._poll_interval_ms is None:
            return await self.run_all()
        async with self._cache_lock:
            if self._cached_results is None:
                self._cached_results = await self.run_all()
            return self._cached_results

    def _register_routes(self, tags: list[str]) -> None:
        registry = self
        prefix = self.prefix

        @self.app.get(
            f"{prefix}/live",
            tags=tags,
            summary="Liveness check",
            description="Returns 200 while the process is running.",
        )
        async def liveness() -> JSONResponse:
            return JSONResponse({"status": "ok"})

        @self.app.get(
            f"{prefix}/ready",
            tags=tags,
            summary="Readiness check",
            description="Returns 200 if all probes pass, 503 if any fail.",
        )
        async def readiness() -> JSONResponse:
            results = await registry._get_results()
            report = HealthReport.from_results(results)
            code = 200 if report.status == ProbeStatus.HEALTHY else 503
            return JSONResponse(report.model_dump(), status_code=code)

        @self.app.get(
            f"{prefix}/status",
            tags=tags,
            summary="Detailed health status",
            description=(
                "Returns full probe results. "
                "200 when all healthy, 207 Multi-Status when any probe fails."
            ),
        )
        async def health_status() -> JSONResponse:
            results = await registry._get_results()
            report = HealthReport.from_results(results)
            code = 200 if report.status == ProbeStatus.HEALTHY else 207
            return JSONResponse(report.model_dump(), status_code=code)
