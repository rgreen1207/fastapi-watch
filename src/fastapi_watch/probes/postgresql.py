from __future__ import annotations

import time

from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe


class PostgreSQLProbe(BaseProbe):
    """Health probe for PostgreSQL using asyncpg (direct driver, no SQLAlchemy).

    Install with: ``pip install fastapi-watch[postgres]``

    Args:
        url: asyncpg DSN — ``postgresql://user:pass@host:port/db``.
        name: Probe name shown in health reports.
        query: Query used to verify connectivity (default ``SELECT 1``).
        timeout: Connection timeout in seconds (default 5.0).
    """

    def __init__(
        self,
        url: str,
        name: str = "postgresql",
        query: str = "SELECT 1",
        timeout: float = 5.0,
    ) -> None:
        self.url = url
        self.name = name
        self.query = query
        self.timeout = timeout

    async def check(self) -> ProbeResult:
        try:
            import asyncpg
        except ImportError as exc:
            raise ImportError(
                "Install fastapi-watch[postgres] to use PostgreSQLProbe."
            ) from exc

        start = time.perf_counter()
        conn = None
        try:
            conn = await asyncpg.connect(dsn=self.url, timeout=self.timeout)
            await conn.fetchval(self.query)
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
            if conn is not None:
                await conn.close()
