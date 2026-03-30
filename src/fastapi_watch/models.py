from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ProbeStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class ProbeResult(BaseModel):
    name: str
    status: ProbeStatus
    critical: bool = True
    latency_ms: float = 0.0
    error: str | None = None
    details: dict[str, Any] | None = None

    @property
    def is_healthy(self) -> bool:
        """True only when status is HEALTHY (strict check)."""
        return self.status == ProbeStatus.HEALTHY

    @property
    def is_degraded(self) -> bool:
        """True when status is DEGRADED."""
        return self.status == ProbeStatus.DEGRADED

    @property
    def is_passing(self) -> bool:
        """True when status is HEALTHY or DEGRADED (not UNHEALTHY).

        Used by the circuit breaker to determine whether to reset the failure
        counter — a DEGRADED probe is still answering, just under stress.
        """
        return self.status != ProbeStatus.UNHEALTHY


class HealthReport(BaseModel):
    status: ProbeStatus
    checked_at: datetime | None = None
    timezone: str | None = None
    probes: list[ProbeResult] = Field(default_factory=list)

    @classmethod
    def from_results(
        cls,
        results: list[ProbeResult],
        checked_at: datetime | None = None,
        timezone: str | None = None,
    ) -> "HealthReport":
        critical = [r for r in results if r.critical]
        if any(r.status == ProbeStatus.UNHEALTHY for r in critical):
            overall = ProbeStatus.UNHEALTHY
        elif any(r.status == ProbeStatus.DEGRADED for r in critical):
            overall = ProbeStatus.DEGRADED
        else:
            overall = ProbeStatus.HEALTHY
        return cls(status=overall, checked_at=checked_at, timezone=timezone, probes=results)
