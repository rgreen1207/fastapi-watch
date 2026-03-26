from __future__ import annotations

import time

from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe


class MongoProbe(BaseProbe):
    """Health probe for MongoDB using motor.

    Returns server version, uptime, connection pool stats, and memory usage.

    Install with: ``pip install fastapi-watch[mongo]``
    """

    def __init__(
        self,
        url: str = "mongodb://localhost:27017",
        name: str = "mongodb",
        server_selection_timeout_ms: int = 2000,
    ) -> None:
        self.url = url
        self.name = name
        self._timeout_ms = server_selection_timeout_ms

    async def check(self) -> ProbeResult:
        try:
            import motor.motor_asyncio as motor
        except ImportError as exc:
            raise ImportError(
                "Install fastapi-watch[mongo] to use MongoProbe."
            ) from exc

        start = time.perf_counter()
        client = None
        try:
            client = motor.AsyncIOMotorClient(
                self.url,
                serverSelectionTimeoutMS=self._timeout_ms,
            )
            status = await client.admin.command("serverStatus")
            latency = (time.perf_counter() - start) * 1000

            connections = status.get("connections", {})
            mem = status.get("mem", {})
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.HEALTHY,
                latency_ms=round(latency, 2),
                details={
                    "version": status.get("version"),
                    "uptime_seconds": status.get("uptime"),
                    "connections": {
                        "current": connections.get("current"),
                        "available": connections.get("available"),
                        "total_created": connections.get("totalCreated"),
                    },
                    "memory_mb": {
                        "resident": mem.get("resident"),
                        "virtual": mem.get("virtual"),
                    },
                    "storage_engine": status.get("storageEngine", {}).get("name"),
                },
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
                client.close()
