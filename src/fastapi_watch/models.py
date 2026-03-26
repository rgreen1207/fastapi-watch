from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class ProbeStatus(str, Enum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


class ProbeResult(BaseModel):
    name: str
    status: ProbeStatus
    critical: bool = True
    latency_ms: float = 0.0
    error: Optional[str] = None
    details: Optional[dict[str, Any]] = None

    @property
    def is_healthy(self) -> bool:
        return self.status == ProbeStatus.HEALTHY


class HealthReport(BaseModel):
    status: ProbeStatus
    checked_at: Optional[datetime] = None
    probes: list[ProbeResult] = Field(default_factory=list)

    @classmethod
    def from_results(
        cls,
        results: list[ProbeResult],
        checked_at: Optional[datetime] = None,
    ) -> "HealthReport":
        critical_results = [r for r in results if r.critical]
        overall = (
            ProbeStatus.HEALTHY
            if all(r.is_healthy for r in critical_results)
            else ProbeStatus.UNHEALTHY
        )
        return cls(status=overall, checked_at=checked_at, probes=results)
