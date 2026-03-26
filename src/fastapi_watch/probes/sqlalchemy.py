from __future__ import annotations

import time
from typing import TYPE_CHECKING

from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


class SqlAlchemyProbe(BaseProbe):
    """Health probe for a SQLAlchemy 2.x async engine.

    Install with: ``pip install fastapi-watch[sqlalchemy]``

    Args:
        engine: An ``AsyncEngine`` instance.
        name: Probe name shown in health reports.
        query: SQL used to verify connectivity (default ``SELECT 1``).
    """

    def __init__(
        self,
        engine: "AsyncEngine",
        name: str = "database",
        query: str = "SELECT 1",
    ) -> None:
        self.engine = engine
        self.name = name
        self.query = query

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
                await conn.execute(text(self.query))
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
