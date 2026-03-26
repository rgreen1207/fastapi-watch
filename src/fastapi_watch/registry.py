from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .models import HealthReport, ProbeResult, ProbeStatus
from .probes.base import BaseProbe

logger = logging.getLogger(__name__)


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

    Example::

        registry = HealthRegistry(app)
        registry.add(RedisProbe(url="redis://localhost"))
        registry.add(HttpProbe(url="https://api.example.com/health"))
    """

    def __init__(
        self,
        app: FastAPI,
        prefix: str = "/health",
        tags: list[str] | None = None,
    ) -> None:
        self.app = app
        self.prefix = prefix
        self._probes: list[BaseProbe] = []
        self._register_routes(tags or ["health"])

    def add(self, probe: BaseProbe) -> "HealthRegistry":
        """Add *probe* to the registry. Returns ``self`` for chaining."""
        self._probes.append(probe)
        return self

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
            results = await registry.run_all()
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
            results = await registry.run_all()
            report = HealthReport.from_results(results)
            code = 200 if report.status == ProbeStatus.HEALTHY else 207
            return JSONResponse(report.model_dump(), status_code=code)
