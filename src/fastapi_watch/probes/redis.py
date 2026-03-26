from __future__ import annotations

import time

from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe


class RedisProbe(BaseProbe):
    """Health probe for Redis using aioredis.

    Install with: ``pip install fastapi-watch[redis]``
    """

    def __init__(self, url: str = "redis://localhost", name: str = "redis") -> None:
        self.url = url
        self.name = name

    async def check(self) -> ProbeResult:
        try:
            import aioredis
        except ImportError as exc:
            raise ImportError(
                "Install fastapi-watch[redis] to use RedisProbe."
            ) from exc

        start = time.perf_counter()
        try:
            redis = await aioredis.from_url(self.url)
            await redis.ping()
            await redis.aclose()
            latency = (time.perf_counter() - start) * 1000
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.HEALTHY,
                latency_ms=round(latency, 2),
            )
        except Exception as exc:
            latency = (time.perf_counter() - start) * 1000
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.UNHEALTHY,
                latency_ms=round(latency, 2),
                error=str(exc),
            )
