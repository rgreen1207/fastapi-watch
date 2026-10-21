import time
from typing import Any, Callable

from .._executor import SingleFlight, StillRunning
from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe, _failure_message

# Fields kept from each task dict returned by inspect.active() / reserved()
_TASK_FIELDS = ("id", "name", "time_start", "worker_pid")
# Task arguments are application data (emails, tokens, personal data), so they
# are only kept when the probe is created with include_task_args=True.
_TASK_ARG_FIELDS = ("args", "kwargs")
# Fields kept from each task dict returned by inspect.scheduled()
_SCHEDULED_FIELDS = ("eta", "priority", "request")
# Most tasks listed per worker for each of active / reserved / scheduled.  The
# history keeps many copies of every result, so details must stay bounded.
_MAX_TASKS_LISTED = 50

# Slack on top of the broadcast waits for broker connect/teardown.
_DEADLINE_MARGIN_S = 2.0


def _pluck(d: dict, keys: tuple) -> dict:
    return {k: d[k] for k in keys if k in d}


def _clean_task(task: dict, include_args: bool = False) -> dict:
    return _pluck(task, _TASK_FIELDS + _TASK_ARG_FIELDS if include_args else _TASK_FIELDS)


def _clean_scheduled(entry: dict, include_args: bool = False) -> dict:
    out = _pluck(entry, _SCHEDULED_FIELDS)
    if "request" in out:
        out["request"] = _clean_task(out["request"], include_args)
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

    By default only a ``ping`` broadcast is issued on each poll — one round-trip
    to confirm workers are alive. Set ``detailed=True`` to enable the full
    inspection (active, reserved, scheduled, registered tasks, pool stats, and
    queue bindings), which issues additional broadcasts and is best suited for
    development or test environments.

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
            A broadcast collects replies for this long, so the probe's own
            overall deadline (its ``timeout`` attribute, used by the registry)
            is set to cover every broadcast it issues plus a margin.
        min_workers: Minimum number of workers that must be online for the
            probe to be healthy. ``0`` (default) means workers are optional.
        detailed: When ``True``, collects per-worker active/reserved/scheduled
            tasks, registered task names, queue bindings, and pool stats.
            Disabled by default to minimise broadcast traffic in production.
            Each task list is capped at 50 entries; when a list is cut, the
            worker's ``truncated`` dict says how many entries were left out
            (the ``*_tasks`` counts are always the full totals).
        include_task_args: When ``True`` (and *detailed*), each listed task
            also carries its ``args`` and ``kwargs``.  Off by default: task
            arguments are application data — email addresses, tokens,
            personal data — and health endpoints are often public.

    The inspect calls are blocking, so they run in fastapi-watch's own small
    thread pool rather than the event loop's default executor.  If a broker
    hangs past the probe's deadline, the next check reports
    ``"previous check still running"`` instead of starting another call.

    Example::

        # Production — ping only
        registry.add(CeleryProbe(celery, min_workers=2))

        # Development / test — full worker inspection
        registry.add(CeleryProbe(celery, min_workers=1, detailed=True))
    """

    def __init__(
        self,
        app: Any,
        name: str = "celery",
        timeout: float = 1.0,
        min_workers: int = 0,
        detailed: bool = False,
        poll_interval_ms: int | None = None,
        include_task_args: bool = False,
    ) -> None:
        self.app = app
        self.name = name
        self.reply_timeout = timeout
        # Without a reply limit each broadcast waits the full reply_timeout, so
        # an overall deadline equal to it would expire before a healthy ping
        # returned.  Cover every broadcast (ping, plus six more when detailed).
        broadcasts = 7 if detailed else 1
        self.timeout = timeout * broadcasts + _DEADLINE_MARGIN_S
        self.min_workers = min_workers
        self.detailed = detailed
        self.include_task_args = include_task_args
        self.poll_interval_ms = poll_interval_ms
        self._inspect_call = SingleFlight()

    # ------------------------------------------------------------------
    # Synchronous inspect — run in fastapi-watch's thread pool
    # ------------------------------------------------------------------

    def _task_list(self, tasks: list, clean: Callable[[dict, bool], dict], truncated: dict[str, int], key: str) -> list[dict]:
        """Clean at most ``_MAX_TASKS_LISTED`` of *tasks*, noting any left out under *key*."""
        if len(tasks) > _MAX_TASKS_LISTED:
            truncated[key] = len(tasks) - _MAX_TASKS_LISTED
        return [clean(t, self.include_task_args) for t in tasks[:_MAX_TASKS_LISTED]]

    def _inspect(self) -> dict[str, Any]:
        inspector = self.app.control.inspect(timeout=self.reply_timeout)

        ping = inspector.ping() or {}
        workers = list(ping.keys())

        if not workers:
            return {"workers_online": 0, "workers": []}

        if not self.detailed:
            return {"workers_online": len(workers), "workers": workers}

        # detailed=True — full inspection (additional broadcasts per call)
        active = inspector.active() or {}
        reserved = inspector.reserved() or {}
        scheduled = inspector.scheduled() or {}
        registered = inspector.registered() or {}
        stats = inspector.stats() or {}
        active_queues = inspector.active_queues() or {}

        worker_details: dict[str, Any] = {}
        for worker in workers:
            worker_active = list(active.get(worker) or [])
            worker_reserved = list(reserved.get(worker) or [])
            worker_scheduled = list(scheduled.get(worker) or [])
            worker_registered = sorted(registered.get(worker) or [])
            worker_stats = stats.get(worker) or {}
            worker_queues = [q["name"] for q in (active_queues.get(worker) or [])]
            truncated: dict[str, int] = {}

            worker_details[worker] = {
                "status": "online",
                "queues": worker_queues,
                "active_tasks": len(worker_active),
                "reserved_tasks": len(worker_reserved),
                "scheduled_tasks": len(worker_scheduled),
                "pool": _pool_info(worker_stats),
                "total_tasks_executed": worker_stats.get("total", {}),
                "registered_tasks": worker_registered,
                "active": self._task_list(worker_active, _clean_task, truncated, "active"),
                "reserved": self._task_list(worker_reserved, _clean_task, truncated, "reserved"),
                "scheduled": self._task_list(worker_scheduled, _clean_scheduled, truncated, "scheduled"),
            }
            if truncated:
                worker_details[worker]["truncated"] = truncated

        return {"workers_online": len(workers), "workers": worker_details}

    # ------------------------------------------------------------------
    # Async check
    # ------------------------------------------------------------------

    async def check(self) -> ProbeResult:
        start = time.perf_counter()
        try:
            data = await self._inspect_call.run(self._inspect)
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

        except StillRunning:
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.UNHEALTHY,
                latency_ms=0.0,
                error="previous check still running",
            )
        except Exception as exc:
            latency = (time.perf_counter() - start) * 1000
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.UNHEALTHY,
                latency_ms=round(latency, 2),
                error=_failure_message(self.name, exc),
            )
