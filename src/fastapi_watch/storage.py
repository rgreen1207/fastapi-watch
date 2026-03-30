"""Storage backends for probe results and alert history.

:class:`HealthRegistry` writes to storage after every probe poll and reads
from it to serve the health endpoints.  Three collections are managed:

* **Latest** — the most recent :class:`~fastapi_watch.models.ProbeResult` per
  probe.  Served by ``GET /health/status`` and the live dashboard.
* **History** — a rolling window of past results per probe (capped at
  ``max_results`` entries).  Served by ``GET /health/history``.
* **Alerts** — a log of probe state-change events
  (:class:`~fastapi_watch.models.AlertRecord`), written whenever a probe
  transitions between HEALTHY / DEGRADED / UNHEALTHY.  Served by
  ``GET /health/alerts`` and consumed by alerters in ``alerts.py``.

Both the latest and history collections share the same ``result_ttl_seconds``
expiry.  Alerts have their own ``alert_ttl_seconds`` expiry.  Setting either
TTL to ``0`` disables time-based expiry for that collection.

:class:`ProbeStorage` is the protocol every backend must satisfy.
:class:`InMemoryProbeStorage` is the default in-process implementation.

Pass a custom backend to ``HealthRegistry(app, storage=my_storage)`` to
persist results and alerts in an external system (e.g. Redis, PostgreSQL)
so they survive process restarts or are shared across multiple instances.
"""
import time
from collections import deque
from typing import Protocol, runtime_checkable

from .models import AlertRecord, ProbeResult


@runtime_checkable
class ProbeStorage(Protocol):
    """Protocol for probe result and alert storage backends.

    Implement this to plug in a custom storage backend (e.g. Redis, Memcached).
    Pass your implementation to ``HealthRegistry(app, storage=my_storage)``.

    All methods except ``clear_latest`` are async to support I/O-bound backends.
    ``clear_latest`` is sync because it is called from a sync context.

    Example Redis implementation sketch::

        class RedisProbeStorage:
            def __init__(self, redis, result_ttl_seconds=7200, alert_ttl_seconds=259200):
                self._redis = redis
                self._result_ttl = int(result_ttl_seconds)
                self._alert_ttl = int(alert_ttl_seconds)

            async def get_latest(self, name):
                raw = await self._redis.get(f"fw:latest:{name}")
                return ProbeResult.model_validate_json(raw) if raw else None

            async def get_all_latest(self):
                keys = await self._redis.keys("fw:latest:*")
                if not keys:
                    return {}
                values = await self._redis.mget(*keys)
                return {
                    k.split(":", 2)[2]: ProbeResult.model_validate_json(v)
                    for k, v in zip(keys, values) if v
                }

            async def set_latest(self, result):
                await self._redis.setex(
                    f"fw:latest:{result.name}", self._result_ttl,
                    result.model_dump_json()
                )

            def clear_latest(self):
                import asyncio
                loop = asyncio.get_event_loop()
                keys = loop.run_until_complete(self._redis.keys("fw:latest:*"))
                if keys:
                    loop.run_until_complete(self._redis.delete(*keys))

            async def append_history(self, result):
                key = f"fw:history:{result.name}"
                await self._redis.rpush(key, result.model_dump_json())
                await self._redis.ltrim(key, -self._max_results, -1)
                await self._redis.expire(key, self._result_ttl)

            async def get_history(self):
                keys = await self._redis.keys("fw:history:*")
                out = {}
                for key in keys:
                    name = key.split(":", 2)[2]
                    raw_list = await self._redis.lrange(key, 0, -1)
                    out[name] = [ProbeResult.model_validate_json(r) for r in raw_list]
                return out

            async def append_alert(self, alert):
                await self._redis.rpush("fw:alerts", alert.model_dump_json())
                await self._redis.expire("fw:alerts", self._alert_ttl)

            async def get_alerts(self):
                raw_list = await self._redis.lrange("fw:alerts", 0, -1)
                return [AlertRecord.model_validate_json(r) for r in raw_list]
    """

    async def get_latest(self, name: str) -> "ProbeResult | None": ...
    async def get_all_latest(self) -> "dict[str, ProbeResult]": ...
    async def set_latest(self, result: "ProbeResult") -> None: ...
    def clear_latest(self) -> None: ...
    async def append_history(self, result: "ProbeResult") -> None: ...
    async def get_history(self) -> "dict[str, list[ProbeResult]]": ...
    async def append_alert(self, alert: "AlertRecord") -> None: ...
    async def get_alerts(self) -> "list[AlertRecord]": ...


class InMemoryProbeStorage:
    """Default in-process storage backend.  All data is held in Python dicts
    and deques — no external dependencies, no I/O.

    **Latest results** (one per probe): expire after *result_ttl_seconds*
    (default 2 hours).  A stale latest result is dropped on the next read so
    ``GET /health/status`` never returns data older than the TTL.

    **History** (rolling window per probe): capped at *max_results* entries
    (default 120 per probe); oldest entries are evicted when the cap is
    reached.  The same *result_ttl_seconds* TTL applies on read.

    **Alerts** (state-change log): capped at *max_alerts* entries (default
    120 total); oldest alerts are evicted when full.  Alerts older than
    *alert_ttl_seconds* (default 72 hours) are dropped on read.

    Set either TTL to ``0`` to disable time-based expiry for that collection.

    For multi-process deployments or persistence across restarts, replace this
    with a custom backend that implements :class:`ProbeStorage` and pass it to
    ``HealthRegistry(app, storage=my_storage)``.
    """

    def __init__(
        self,
        max_results: int = 120,
        result_ttl_seconds: float = 7200.0,
        alert_ttl_seconds: float = 259200.0,
        max_alerts: int = 120,
    ) -> None:
        self._max_results = max(1, max_results)
        self._result_ttl = result_ttl_seconds
        self._alert_ttl = alert_ttl_seconds
        self._max_alerts = max(1, max_alerts)

        # Latest result per probe
        self._cache: dict[str, ProbeResult] = {}
        self._cache_times: dict[str, float] = {}  # monotonic timestamps

        # Rolling history per probe (capped at max_results entries)
        self._history: dict[str, deque[ProbeResult]] = {}
        self._history_times: dict[str, deque[float]] = {}  # parallel monotonic timestamps

        # Alert log (capped at max_alerts; oldest dropped when full, TTL applied on read)
        self._alerts: deque[tuple[float, AlertRecord]] = deque(maxlen=self._max_alerts)

    def _result_expired(self, ts: float) -> bool:
        return self._result_ttl > 0 and (time.monotonic() - ts) > self._result_ttl

    async def get_latest(self, name: str) -> ProbeResult | None:
        result = self._cache.get(name)
        if result is None:
            return None
        if self._result_expired(self._cache_times.get(name, 0.0)):
            del self._cache[name]
            self._cache_times.pop(name, None)
            return None
        return result

    async def get_all_latest(self) -> dict[str, ProbeResult]:
        now = time.monotonic()
        if self._result_ttl > 0:
            expired = [
                name for name, ts in self._cache_times.items()
                if (now - ts) > self._result_ttl
            ]
            for name in expired:
                self._cache.pop(name, None)
                self._cache_times.pop(name, None)
        return dict(self._cache)

    async def set_latest(self, result: ProbeResult) -> None:
        self._cache[result.name] = result
        self._cache_times[result.name] = time.monotonic()

    def clear_latest(self) -> None:
        self._cache.clear()
        self._cache_times.clear()

    async def append_history(self, result: ProbeResult) -> None:
        name = result.name
        if name not in self._history:
            self._history[name] = deque(maxlen=self._max_results)
            self._history_times[name] = deque(maxlen=self._max_results)
        self._history[name].append(result)
        self._history_times[name].append(time.monotonic())

    async def get_history(self) -> dict[str, list[ProbeResult]]:
        now = time.monotonic()
        out: dict[str, list[ProbeResult]] = {}
        for name, entries in self._history.items():
            if self._result_ttl > 0:
                times = self._history_times.get(name, deque())
                valid = [r for r, ts in zip(entries, times) if (now - ts) <= self._result_ttl]
            else:
                valid = list(entries)
            if valid:
                out[name] = valid
        return out

    async def append_alert(self, alert: AlertRecord) -> None:
        self._alerts.append((time.monotonic(), alert))

    async def get_alerts(self) -> list[AlertRecord]:
        if self._alert_ttl > 0:
            now = time.monotonic()
            while self._alerts and (now - self._alerts[0][0]) > self._alert_ttl:
                self._alerts.popleft()
        return [alert for _, alert in self._alerts]
