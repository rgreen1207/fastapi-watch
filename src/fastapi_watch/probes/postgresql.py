import asyncio
import time

from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe


class PostgreSQLProbe(BaseProbe):
    """Health probe for PostgreSQL using asyncpg (direct driver, no SQLAlchemy).

    Returns server version, active connection count, max connections,
    and the human-readable size of the current database.

    Install with: ``pip install fastapi-watch[postgres]``

    Args:
        url: asyncpg DSN — ``postgresql://user:pass@host:port/db``.
        name: Probe name shown in health reports.
        timeout: Connection timeout in seconds (default 5.0).
    """

    def __init__(
        self,
        url: str,
        name: str = "postgresql",
        timeout: float = 5.0,
    ) -> None:
        self.url = url
        self.name = name
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

            version, active_connections, max_connections, db_size = await asyncio.gather(
                conn.fetchval("SELECT version()"),
                conn.fetchval(
                    "SELECT count(*) FROM pg_stat_activity WHERE state = 'active'"
                ),
                conn.fetchval(
                    "SELECT setting::int FROM pg_settings WHERE name = 'max_connections'"
                ),
                conn.fetchval(
                    "SELECT pg_size_pretty(pg_database_size(current_database()))"
                ),
            )

            latency = (time.perf_counter() - start) * 1000
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.HEALTHY,
                latency_ms=round(latency, 2),
                details={
                    "version": version,
                    "active_connections": active_connections,
                    "max_connections": max_connections,
                    "database_size": db_size,
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
            if conn is not None:
                await conn.close()
