import time

from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe


class MemcachedProbe(BaseProbe):
    """Health probe for Memcached using aiomcache.

    Install with: ``pip install fastapi-watch[memcached]``

    Args:
        host: Memcached host (default ``localhost``).
        port: Memcached port (default ``11211``).
        name: Probe name shown in health reports.
        pool_size: Connection pool size passed to aiomcache (default 1).
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 11211,
        name: str = "memcached",
        pool_size: int = 1,
        poll_interval_ms: int | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.name = name
        self.poll_interval_ms = poll_interval_ms
        self._pool_size = pool_size

    async def check(self) -> ProbeResult:
        try:
            import aiomcache
        except ImportError as exc:
            raise ImportError(
                "Install fastapi-watch[memcached] to use MemcachedProbe."
            ) from exc

        start = time.perf_counter()
        client = None
        try:
            client = aiomcache.Client(
                self.host,
                self.port,
                pool_size=self._pool_size,
            )
            # stats() returns server statistics — a reliable liveness signal
            # that works even on a completely empty cache.
            await client.stats()
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
        finally:
            if client is not None:
                await client.close()
