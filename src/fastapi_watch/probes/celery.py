import asyncio
import time
from typing import Any

from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe

# Fields kept from each task dict returned by inspect.active() / reserved()
_TASK_FIELDS = ("id", "name", "args", "kwargs", "time_start", "worker_pid")
# Fields kept from each task dict returned by inspect.scheduled()
_SCHEDULED_FIELDS = ("eta", "priority", "request")


def _pluck(d: dict, keys: tuple) -> dict:
    return {k: d[k] for k in keys if k in d}


def _clean_task(task: dict) -> dict:
    return _pluck(task, _TASK_FIELDS)


def _clean_scheduled(entry: dict) -> dict:
    out = _pluck(entry, _SCHEDULED_FIELDS)
    if "request" in out:
        out["request"] = _pluck(out["request"], _TASK_FIELDS)
    return out


def _pool_info(stats: dict) -> dict:
    pool = stats.get("pool", {})
    return {
        "implementation": pool.get("implementation"),
        "max_concurrency": pool.get("max-concurrency"),
        "processes": pool.get("processes", []),
    }


class CeleryProbe(BaseProbe):
    """Health probe that inspects live Celery workers via the control broadcast API.

    Reports per-worker active, reserved, and scheduled tasks, registered task
    names, queue bindings, pool configuration, and lifetime task counts.

    When no workers are found and *min_workers* is ``0`` (the default), the
    probe is **healthy** — this covers scale-to-zero deployments where workers
    are only started when there is work to do.  Set *min_workers* ≥ 1 to
    require at least that many workers to be online.

    Args:
        app: Celery application instance.
        name: Probe name (default ``"celery"``).
        timeout: Seconds to wait for each inspector broadcast reply.
            Workers that are alive respond well within 1 s; the timeout
            only matters when workers are unreachable (default ``1.0``).
        min_workers: Minimum number of workers that must be online for the
            probe to be healthy.  ``0`` (default) means workers are optional.

    Example::

        from celery_app import celery
        registry.add(CeleryProbe(celery))
        registry.add(CeleryProbe(celery, min_workers=2, timeout=2.0))
    """

    def __init__(
        self,
        app: Any,
        name: str = "celery",
        timeout: float = 1.0,
        min_workers: int = 0,
    ) -> None:
        self.app = app
        self.name = name
        self.timeout = timeout
        self.min_workers = min_workers

    # ------------------------------------------------------------------
    # Synchronous inspect — called via run_in_executor
    # ------------------------------------------------------------------

    def _inspect(self) -> dict[str, Any]:
        i = self.app.control.inspect(timeout=self.timeout)

        # Ping first: only one broadcast roundtrip if no workers are online.
        ping = i.ping() or {}
        workers = list(ping.keys())

        if not workers:
            return {"workers_online": 0, "workers": {}}

        # Workers are alive — fetch detailed state (each call is one broadcast).
        active = i.active() or {}
        reserved = i.reserved() or {}
        scheduled = i.scheduled() or {}
        registered = i.registered() or {}
        stats = i.stats() or {}
        active_queues = i.active_queues() or {}

        worker_details: dict[str, Any] = {}
        for worker in workers:
            worker_active = [_clean_task(t) for t in (active.get(worker) or [])]
            worker_reserved = [_clean_task(t) for t in (reserved.get(worker) or [])]
            worker_scheduled = [_clean_scheduled(e) for e in (scheduled.get(worker) or [])]
            worker_registered = sorted(registered.get(worker) or [])
            worker_stats = stats.get(worker) or {}
            worker_queues = [q["name"] for q in (active_queues.get(worker) or [])]

            worker_details[worker] = {
                "status": "online",
                "queues": worker_queues,
                "active_tasks": len(worker_active),
                "reserved_tasks": len(worker_reserved),
                "scheduled_tasks": len(worker_scheduled),
                "pool": _pool_info(worker_stats),
                "total_tasks_executed": worker_stats.get("total", {}),
                "registered_tasks": worker_registered,
                "active": worker_active,
                "reserved": worker_reserved,
                "scheduled": worker_scheduled,
            }

        return {"workers_online": len(workers), "workers": worker_details}

    # ------------------------------------------------------------------
    # Async check
    # ------------------------------------------------------------------

    async def check(self) -> ProbeResult:
        start = time.perf_counter()
        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, self._inspect)
            latency = (time.perf_counter() - start) * 1000

            workers_online: int = data["workers_online"]

            if workers_online == 0:
                if self.min_workers == 0:
                    return ProbeResult(
                        name=self.name,
                        status=ProbeStatus.HEALTHY,
                        latency_ms=round(latency, 2),
                        details={
                            "workers_online": 0,
                            "reason": (
                                "no workers online — they may be scaled down "
                                "because there are no pending tasks"
                            ),
                        },
                    )
                return ProbeResult(
                    name=self.name,
                    status=ProbeStatus.UNHEALTHY,
                    latency_ms=round(latency, 2),
                    error=f"no workers online; expected at least {self.min_workers}",
                    details={"workers_online": 0},
                )

            if workers_online < self.min_workers:
                return ProbeResult(
                    name=self.name,
                    status=ProbeStatus.UNHEALTHY,
                    latency_ms=round(latency, 2),
                    error=(
                        f"{workers_online} worker(s) online; "
                        f"expected at least {self.min_workers}"
                    ),
                    details=data,
                )

            return ProbeResult(
                name=self.name,
                status=ProbeStatus.HEALTHY,
                latency_ms=round(latency, 2),
                details=data,
            )

        except Exception as exc:
            latency = (time.perf_counter() - start) * 1000
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.UNHEALTHY,
                latency_ms=round(latency, 2),
                error=str(exc),
            )
