import asyncio
import shutil
import time

from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe


class DiskProbe(BaseProbe):
    """Checks available disk space using :func:`shutil.disk_usage` (stdlib only).

    Returns DEGRADED when used space exceeds *warn_percent* and UNHEALTHY when
    it exceeds *fail_percent*.  A full disk silently breaks writes, logs, and
    temporary files in ways that are hard to diagnose after the fact.

    Args:
        path: Filesystem path to check (default ``"/"``).
        name: Probe name (default ``"disk"``).
        warn_percent: Percent used above which the probe is DEGRADED
            (default ``80.0``).
        fail_percent: Percent used above which the probe is UNHEALTHY
            (default ``90.0``).
        poll_interval_ms: Per-probe poll interval override.

    Example::

        registry.add(DiskProbe(path="/data", warn_percent=75.0, fail_percent=90.0))
    """

    def __init__(
        self,
        path: str = "/",
        name: str = "disk",
        warn_percent: float = 80.0,
        fail_percent: float = 90.0,
        poll_interval_ms: int | None = None,
    ) -> None:
        self.path = path
        self.name = name
        self.warn_percent = warn_percent
        self.fail_percent = fail_percent
        self.poll_interval_ms = poll_interval_ms

    async def check(self) -> ProbeResult:
        loop = asyncio.get_running_loop()
        start = time.perf_counter()
        try:
            usage = await loop.run_in_executor(None, shutil.disk_usage, self.path)
            latency = round((time.perf_counter() - start) * 1000, 2)

            total_gb = round(usage.total / 1024 ** 3, 2)
            used_gb = round(usage.used / 1024 ** 3, 2)
            free_gb = round(usage.free / 1024 ** 3, 2)
            percent_used = round((usage.used / usage.total) * 100, 2)

            if percent_used >= self.fail_percent:
                status = ProbeStatus.UNHEALTHY
            elif percent_used >= self.warn_percent:
                status = ProbeStatus.DEGRADED
            else:
                status = ProbeStatus.HEALTHY

            return ProbeResult(
                name=self.name,
                status=status,
                latency_ms=latency,
                details={
                    "path": self.path,
                    "total_gb": total_gb,
                    "used_gb": used_gb,
                    "free_gb": free_gb,
                    "percent_used": percent_used,
                    "warn_percent": self.warn_percent,
                    "fail_percent": self.fail_percent,
                },
            )
        except Exception as exc:
            latency = round((time.perf_counter() - start) * 1000, 2)
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.UNHEALTHY,
                latency_ms=latency,
                error=f"{type(exc).__name__}: {exc}",
                details={"path": self.path},
            )
