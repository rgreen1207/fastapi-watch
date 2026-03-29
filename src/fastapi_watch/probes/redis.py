import time

from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe


class RedisProbe(BaseProbe):
    """Health probe for Redis using redis[asyncio].

    Returns server version, uptime, memory usage, connected clients,
    total key count, and a per-prefix cluster breakdown with key counts
    and sampled TTLs.

    Install with: ``pip install fastapi-watch[redis]``
    """

    def __init__(self, url: str = "redis://localhost", name: str = "redis") -> None:
        self.url = url
        self.name = name

    async def check(self) -> ProbeResult:
        try:
            from redis.asyncio import from_url
        except ImportError as exc:
            raise ImportError(
                "Install fastapi-watch[redis] to use RedisProbe."
            ) from exc

        start = time.perf_counter()
        redis = None
        try:
            redis = from_url(self.url, decode_responses=True)
            await redis.ping()
            latency = (time.perf_counter() - start) * 1000

            details: dict = {}
            try:
                info = await redis.info()
                details["version"] = info.get("redis_version")
                details["uptime_seconds"] = info.get("uptime_in_seconds")
                details["used_memory_human"] = info.get("used_memory_human")
                details["connected_clients"] = info.get("connected_clients")
                details["role"] = info.get("role")

                db_size = await redis.dbsize()
                details["total_keys"] = db_size

                # Per-prefix cluster breakdown — capped to avoid scanning
                # the entire keyspace on large instances.
                _MAX_SCAN = 1000
                clusters: dict[str, int] = {}
                scanned = 0
                async for key in redis.scan_iter(match="*", count=200):
                    prefix = key.split(":")[0] if ":" in key else key
                    clusters[prefix] = clusters.get(prefix, 0) + 1
                    scanned += 1
                    if scanned >= _MAX_SCAN:
                        break

                if clusters:
                    details["clusters"] = clusters
                    if scanned >= _MAX_SCAN:
                        details["clusters_truncated"] = True
            except Exception:
                pass  # details are best-effort; connectivity is what matters

            return ProbeResult(
                name=self.name,
                status=ProbeStatus.HEALTHY,
                latency_ms=round(latency, 2),
                details=details,
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
            if redis is not None:
                await redis.aclose()
