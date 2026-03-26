import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import AsyncGenerator, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .models import HealthReport, ProbeResult, ProbeStatus
from .probes.base import BaseProbe

_MIN_POLL_INTERVAL_MS = 1000


def _normalize_interval(ms: int | None) -> int | None:
    """Validate and normalize a poll interval.

    - ``None`` or ``0``  → ``None``  (single-fetch mode)
    - ``1 – 999``        → ``1000``  (clamped to minimum)
    - ``>= 1000``        → used as-is
    """
    if ms is None or ms == 0:
        return None
    return max(ms, _MIN_POLL_INTERVAL_MS)


class HealthRegistry:
    """Register health probes and mount health endpoints on a FastAPI app.

    Mounted routes (all customisable via *prefix*):

    - ``GET /health/live``          — liveness; always 200
    - ``GET /health/ready``         — readiness; 200 if all probes pass, 503 otherwise
    - ``GET /health/status``        — full probe detail; 200 / 207 (Multi-Status)
    - ``GET /health/ready/stream``  — SSE stream of readiness; polls while connected
    - ``GET /health/status/stream`` — SSE stream of full probe detail; polls while connected

    The background poll loop only runs while at least one SSE client is connected.
    Regular ``GET`` endpoints serve from the last cached result when available,
    or run probes on demand if no stream has been active.

    Args:
        app: FastAPI application.
        prefix: URL prefix for health routes (default ``/health``).
        tags: OpenAPI tags applied to all routes.
        poll_interval_ms: How often (in milliseconds) to re-run all probes while
            a streaming client is connected.  Defaults to ``60000`` (60 s).
            Pass ``0`` or ``None`` to disable polling — each request will run
            probes on demand and streaming endpoints return a single result then
            close.  Values between 1 and 999 are clamped to 1000 ms.
        logger: Optional :class:`logging.Logger` to use for warnings and probe
            exception messages.  If ``None`` (default) no logging is emitted.

    Example::

        registry = HealthRegistry(app, logger=logging.getLogger(__name__))
        registry.add(RedisProbe(url="redis://localhost"))
        registry.add(HttpProbe(url="https://api.example.com/health"))

        # Change the interval at runtime:
        registry.set_poll_interval(30_000)   # every 30 s
        registry.set_poll_interval(0)        # back to single-fetch mode
    """

    def __init__(
        self,
        app: FastAPI,
        prefix: str = "/health",
        tags: list[str] | None = None,
        poll_interval_ms: int | None = 60_000,
        logger: logging.Logger | None = None,
    ) -> None:
        self.app = app
        self.prefix = prefix
        self._logger = logger
        self._probes: list[tuple[BaseProbe, bool]] = []  # (probe, critical)
        self._poll_interval_ms: int | None = self._set_interval(poll_interval_ms)
        self._cached_results: list[ProbeResult] | None = None
        self._last_checked_at: datetime | None = None
        self._poll_task: asyncio.Task | None = None
        self._active_connections: int = 0
        self._cache_lock = asyncio.Lock()

        self._register_routes(tags or ["health"])

    def add(self, probe: BaseProbe, critical: bool = True) -> "HealthRegistry":
        """Add a single probe to the registry. Returns ``self`` for chaining.

        Args:
            probe: The probe to register.
            critical: When ``True`` (default) a failing probe marks the overall
                status as unhealthy.  Non-critical probes appear in reports but
                do not affect ``/health/ready`` or the top-level ``status``.

        Silently skips the probe if it is already registered (identity check).
        """
        if not any(p is probe for p, _ in self._probes):
            self._probes.append((probe, critical))
        return self

    def add_probes(self, probes: list[BaseProbe], critical: bool = True) -> "HealthRegistry":
        """Add a list of probes to the registry. Returns ``self`` for chaining.

        Args:
            probes: Probes to register.
            critical: Applies to all probes in the list.

        Silently skips any probe that is already registered (identity check).
        """
        for probe in probes:
            self.add(probe, critical=critical)
        return self

    def set_poll_interval(self, ms: int | None) -> None:
        """Update the polling interval at runtime.

        Args:
            ms: New interval in milliseconds.  Pass ``0`` or ``None`` to switch
                to single-fetch mode.  Values between 1 and 999 are clamped to
                1000 ms.

        If streaming clients are connected the poll task is restarted immediately
        with the new interval.  If polling is disabled the task is cancelled and
        the cache is cleared.
        """
        self._poll_interval_ms = self._set_interval(ms)
        if self._poll_interval_ms is None:
            self._cached_results = None
            self._cancel_poll_task()
        elif self._active_connections > 0:
            self._cancel_poll_task()
            try:
                self._poll_task = asyncio.get_running_loop().create_task(self._poll_loop())
            except RuntimeError:
                pass

    async def run_all(self) -> list[ProbeResult]:
        """Run all probes concurrently and return their results.

        A probe that raises an unhandled exception is recorded as unhealthy
        rather than propagating the exception.
        """
        if not self._probes:
            return []

        async def _safe_check(probe: BaseProbe, critical: bool) -> ProbeResult:
            try:
                coro = probe.check()
                result = (
                    await asyncio.wait_for(coro, timeout=probe.timeout)
                    if probe.timeout is not None
                    else await coro
                )
                return result.model_copy(update={"critical": critical})
            except Exception as exc:
                if self._logger:
                    self._logger.exception("Probe %r raised an exception", probe.name)
                return ProbeResult(
                    name=probe.name,
                    status=ProbeStatus.UNHEALTHY,
                    critical=critical,
                    error=f"{type(exc).__name__}: {exc}",
                )

        results = list(await asyncio.gather(*(_safe_check(p, c) for p, c in self._probes)))
        self._last_checked_at = datetime.now(timezone.utc)
        return results

    def _set_interval(self, ms: int | None) -> int | None:
        """Normalize a poll interval and warn if it was clamped."""
        normalized = _normalize_interval(ms)
        if self._logger and ms is not None and ms != 0 and ms < _MIN_POLL_INTERVAL_MS:
            self._logger.warning(
                "poll_interval_ms %d is below the minimum of %d ms; clamping to %d ms",
                ms,
                _MIN_POLL_INTERVAL_MS,
                _MIN_POLL_INTERVAL_MS,
            )
        return normalized

    async def _on_connect(self) -> None:
        self._active_connections += 1
        if self._active_connections == 1 and self._poll_interval_ms is not None:
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def _on_disconnect(self) -> None:
        self._active_connections -= 1
        if self._active_connections == 0:
            self._cancel_poll_task()

    def _cancel_poll_task(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None

    async def _poll_loop(self) -> None:
        while True:
            self._cached_results = await self.run_all()
            await asyncio.sleep(self._poll_interval_ms / 1000)

    async def _get_results(self) -> list[ProbeResult]:
        """Serve cached results for regular GET endpoints, running fresh if needed."""
        if self._poll_interval_ms is None:
            return await self.run_all()
        if self._cached_results is not None:
            return self._cached_results
        async with self._cache_lock:
            if self._cached_results is None:
                self._cached_results = await self.run_all()
            return self._cached_results

    async def _wait_for_next_poll(self, request: Request) -> bool:
        """Sleep for poll_interval_ms, checking for client disconnect every 500 ms.

        Returns True if the client disconnected before the interval elapsed.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._poll_interval_ms / 1000
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.5, remaining))
            if await request.is_disconnected():
                return True

    async def _event_stream(
        self,
        request: Request,
        make_report: Callable[[list[ProbeResult]], dict],
    ) -> AsyncGenerator[str, None]:
        """Async generator that yields SSE events while the client is connected."""
        await self._on_connect()
        try:
            while True:
                results = self._cached_results or await self.run_all()
                yield f"data: {json.dumps(make_report(results))}\n\n"
                if self._poll_interval_ms is None:
                    return
                if await self._wait_for_next_poll(request):
                    return
        finally:
            await self._on_disconnect()

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
            report = HealthReport.from_results(results, checked_at=registry._last_checked_at)
            code = 200 if report.status == ProbeStatus.HEALTHY else 503
            return JSONResponse(report.model_dump(mode="json"), status_code=code)

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
            report = HealthReport.from_results(results, checked_at=registry._last_checked_at)
            code = 200 if report.status == ProbeStatus.HEALTHY else 207
            return JSONResponse(report.model_dump(mode="json"), status_code=code)

        def _make_sse_report(results: list[ProbeResult]) -> dict:
            return HealthReport.from_results(results, checked_at=registry._last_checked_at).model_dump(mode="json")

        sse_headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

        @self.app.get(
            f"{prefix}/ready/stream",
            tags=tags,
            summary="Readiness stream",
            description=(
                "SSE stream of readiness. Polls probes while connected; "
                "poll loop stops when the last client disconnects."
            ),
        )
        async def readiness_stream(request: Request) -> StreamingResponse:
            return StreamingResponse(
                registry._event_stream(request, _make_sse_report),
                media_type="text/event-stream",
                headers=sse_headers,
            )

        @self.app.get(
            f"{prefix}/status/stream",
            tags=tags,
            summary="Health status stream",
            description=(
                "SSE stream of full probe results. Polls probes while connected; "
                "poll loop stops when the last client disconnects."
            ),
        )
        async def status_stream(request: Request) -> StreamingResponse:
            return StreamingResponse(
                registry._event_stream(request, _make_sse_report),
                media_type="text/event-stream",
                headers=sse_headers,
            )
