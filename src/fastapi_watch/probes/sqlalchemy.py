import time
from typing import TYPE_CHECKING

from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


class SqlAlchemyProbe(BaseProbe):
    """Health probe for a SQLAlchemy 2.x async engine.

    Returns the dialect name, driver, and raw server version string.

    Install with: ``pip install fastapi-watch[sqlalchemy]``

    Args:
        engine: An ``AsyncEngine`` instance.
        name: Probe name shown in health reports.
    """

    def __init__(
        self,
        engine: "AsyncEngine",
        name: str = "database",
        poll_interval_ms: int | None = None,
    ) -> None:
        self.engine = engine
        self.name = name
        self.poll_interval_ms = poll_interval_ms

    async def check(self) -> ProbeResult:
        try:
            from sqlalchemy import text
        except ImportError as exc:
            raise ImportError(
                "Install fastapi-watch[sqlalchemy] to use SqlAlchemyProbe."
            ) from exc

        start = time.perf_counter()
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
                server_version = conn.dialect.server_version_info
            latency = (time.perf_counter() - start) * 1000
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.HEALTHY,
                latency_ms=round(latency, 2),
                details={
                    "dialect": self.engine.dialect.name,
                    "driver": self.engine.dialect.driver,
                    "server_version": (
                        ".".join(str(v) for v in server_version)
                        if server_version
                        else None
                    ),
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
