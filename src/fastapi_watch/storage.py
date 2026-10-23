"""Storage backends for probe results and alert history.

:class:`HealthRegistry` writes to storage after every probe check.  The
readiness, status, metrics and stream endpoints are served from the
registry's in-process copy of its own results and never read storage, so a
slow or shared backend cannot slow or skew them.  Three collections are
managed:

* **Latest** — the most recent :class:`~fastapi_watch.models.ProbeResult` per
  probe.  The registry reads it only to show a probe's last result while its
  circuit is open and this process holds none.
* **History** — a rolling window of past results per probe (capped at
  ``max_results`` entries).  Served by ``GET /health/history`` and embedded
  in the dashboard's graphs.
* **Alerts** — a log of probe state-change events
  (:class:`~fastapi_watch.models.AlertRecord`), written whenever a probe
  transitions between HEALTHY / DEGRADED / UNHEALTHY.  Served by
  ``GET /health/alerts``.  Alerters receive each record from the registry
  directly, not from storage.

Both the latest and history collections share the same ``result_ttl_seconds``
expiry.  Alerts have their own ``alert_ttl_seconds`` expiry.  Setting either
TTL to ``0`` disables time-based expiry for that collection.

:class:`ProbeStorage` is the protocol every backend must satisfy.
:class:`InMemoryProbeStorage` is the default in-process implementation.

Pass a backend to ``HealthRegistry(app, storage=my_storage)`` to persist
results and alerts outside the process so they survive restarts or are shared
across instances.  :class:`~fastapi_watch.RedisProbeStorage` (in
``redis_storage.py``) is the bundled Redis backend.
"""
import math
import time
from collections import deque
from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from pydantic_core import to_json

from .models import AlertRecord, ProbeResult

_MIN_ENTRY_BYTES = 512
_TRUNCATED = "_truncated"


def _check_ttls(*ttls: float) -> None:
    """Raise ``ValueError`` unless every TTL is finite and >= 0.

    A negative or NaN TTL compared false against every age, silently turning
    time-based expiry off.
    """
    for ttl in ttls:
        if not 0 <= ttl < math.inf:  # also rejects NaN
            raise ValueError("TTLs must be finite and >= 0 (0 disables time-based expiry)")


def _fit(result: ProbeResult, budget: int) -> "tuple[ProbeResult, bytes]":
    """Return *result*, cut to serialize in at most *budget* bytes, and its JSON.

    An oversized result loses its largest ``details`` values first and is
    marked ``details["_truncated"] = True``; if that is not enough, its
    ``description`` and ``tags`` go and ``error`` is shortened.  Only a probe
    name longer than *budget* can leave it over.
    """
    raw = to_json(result)
    if len(raw) <= budget:
        return result, raw
    details = {k: v for k, v in (result.details or {}).items() if k != _TRUNCATED}
    sizes = {k: len(to_json(k)) + len(to_json(v)) + 2 for k, v in details.items()}
    excess = len(raw) + 20 - budget  # 20 ≈ the "_truncated":true marker
    droppable = sorted(details, key=sizes.__getitem__, reverse=True)
    while droppable and excess > 0:  # drop the largest values until the estimate fits
        key = droppable.pop(0)
        del details[key]
        excess -= sizes[key]
    candidate = result.model_copy(update={"details": {**details, _TRUNCATED: True}})
    raw = to_json(candidate)
    while len(raw) > budget and droppable:  # estimate was short: keep dropping
        del details[droppable.pop(0)]
        candidate = result.model_copy(update={"details": {**details, _TRUNCATED: True}})
        raw = to_json(candidate)
    if len(raw) <= budget:
        return candidate, raw
    # Details alone were not the problem: shed free-text fields too.
    candidate = result.model_copy(update={"details": {_TRUNCATED: True}, "description": None, "tags": []})
    raw = to_json(candidate)
    if len(raw) > budget and candidate.error:
        error = candidate.error.encode("utf-8")
        keep = max(0, len(error) - (len(raw) - budget) - 3)
        candidate = candidate.model_copy(update={"error": error[:keep].decode("utf-8", "ignore") + "..."})
        raw = to_json(candidate)
    return candidate, raw


@runtime_checkable
class ProbeStorage(Protocol):
    """Protocol for probe result and alert storage backends.

    Implement this to plug in a custom storage backend (e.g. Redis, Memcached).
    Pass your implementation to ``HealthRegistry(app, storage=my_storage)``.

    All methods except ``clear_latest`` are async to support I/O-bound backends.
    ``clear_latest`` is sync so it can be called from a sync context.  The
    registry no longer calls it — it would also clear the latest results of
    other apps and replicas sharing the backend — but it remains part of the
    protocol so existing backends keep satisfying it.

    Optional methods, called by the registry only when a backend defines them
    (backends without them keep working)::

        # Apply the registry's history_size / TTL / max_alerts settings.
        # Called only when HealthRegistry is given at least one of them
        # explicitly; otherwise the backend keeps its own settings.
        def configure(self, *, max_results: int, result_ttl_seconds: float,
                      alert_ttl_seconds: float, max_alerts: int) -> None: ...

        # Delete the latest result, history and any index entry of exactly the
        # probes in names, and nothing else.  The registry passes only names
        # it wrote itself and no longer registers, so apps and replicas
        # sharing one namespace never delete each other's data.
        async def forget(self, names: set[str]) -> None: ...

        # Atomically store value under key for ttl_seconds.  Return True if
        # key was absent, expired or held a different value; False if it
        # already held this value (another caller announced it).  The registry
        # passes (probe name, new status, window), so only one replica sends
        # each alert, and a probe going back to a status inside the window is
        # announced again.  ttl_seconds <= 0: always True, nothing stored.
        async def claim_alert(self, key: str, value: str, ttl_seconds: float) -> bool: ...

    ``get_history`` and ``get_alerts`` may also accept a ``names`` argument
    (an iterable of probe names, as the first positional parameter, e.g.
    ``get_history(self, names=None)``): the registry then passes the probes it
    registers and the backend reads only theirs.  A backend without it
    returns everything, and the registry drops other apps' probes itself.

    For Redis use the bundled :class:`~fastapi_watch.RedisProbeStorage`.  A
    custom backend outline::

        class MyStorage:
            async def get_latest(self, name): ...       # ProbeResult | None
            async def get_all_latest(self): ...         # {name: ProbeResult}
            async def set_latest(self, result): ...
            def clear_latest(self): ...                 # sync: schedule I/O on the running loop
            async def append_history(self, result): ... # cap by count AND by age, per entry
            async def get_history(self): ...            # {name: [ProbeResult, ...]}, oldest first
            async def append_alert(self, alert): ...    # cap by count AND by age
            async def get_alerts(self): ...             # [AlertRecord, ...], oldest first

    Keep every collection bounded by the configured count and TTL: health
    monitoring must never grow the store without limit.
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
    reached.  The same *result_ttl_seconds* TTL applies on read.  Entries
    larger than *max_entry_bytes* (as JSON) lose their biggest ``details``
    values first and are marked ``details["_truncated"] = True``, so one
    probe with a huge payload cannot hold *max_results* copies of it.

    **Alerts** (state-change log): capped at *max_alerts* entries (default
    120 total); oldest alerts are evicted when full.  Alerts older than
    *alert_ttl_seconds* (default 72 hours) are dropped on read.

    Set either TTL to ``0`` to disable time-based expiry for that collection.

    For multi-process deployments or persistence across restarts, pass
    :class:`~fastapi_watch.RedisProbeStorage` or another :class:`ProbeStorage`
    implementation as ``HealthRegistry(app, storage=my_storage)``.
    """

    def __init__(
        self,
        max_results: int = 120,
        result_ttl_seconds: float = 7200.0,
        alert_ttl_seconds: float = 259200.0,
        max_alerts: int = 120,
        max_entry_bytes: int = 8192,
    ) -> None:
        if max_entry_bytes < _MIN_ENTRY_BYTES:
            raise ValueError(f"max_entry_bytes must be at least {_MIN_ENTRY_BYTES}")
        _check_ttls(result_ttl_seconds, alert_ttl_seconds)
        self._max_entry_bytes = int(max_entry_bytes)
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

        # Alert claims (claim_alert): key -> (value, monotonic expiry)
        self._claims: dict[str, tuple[str, float]] = {}

    def configure(
        self,
        *,
        max_results: int,
        result_ttl_seconds: float,
        alert_ttl_seconds: float,
        max_alerts: int,
    ) -> None:
        """Apply new retention settings, keeping the newest entries that still fit."""
        _check_ttls(result_ttl_seconds, alert_ttl_seconds)
        self._max_results = max(1, max_results)
        self._result_ttl = result_ttl_seconds
        self._alert_ttl = alert_ttl_seconds
        self._max_alerts = max(1, max_alerts)
        for name in self._history:
            self._history[name] = deque(self._history[name], maxlen=self._max_results)
            self._history_times[name] = deque(self._history_times[name], maxlen=self._max_results)
        self._alerts = deque(self._alerts, maxlen=self._max_alerts)

    async def forget(self, names: set[str]) -> None:
        """Drop the latest result and history of exactly the probes in *names*."""
        for name in names:
            self._cache.pop(name, None)
            self._cache_times.pop(name, None)
            self._history.pop(name, None)
            self._history_times.pop(name, None)

    async def claim_alert(self, key: str, value: str, ttl_seconds: float) -> bool:
        """Store *value* under *key* for *ttl_seconds*; return False only if the
        key already held this *value*.  Process-local; expired claims are
        purged on each call."""
        now = time.monotonic()
        for stale in [k for k, (_, expires) in self._claims.items() if expires <= now]:
            del self._claims[stale]
        if ttl_seconds <= 0:
            return True
        held = self._claims.get(key)
        if held is not None and held[0] == value:
            return False
        self._claims[key] = (value, now + ttl_seconds)
        return True

    def _result_expired(self, recorded_at: float) -> bool:
        return self._result_ttl > 0 and (time.monotonic() - recorded_at) > self._result_ttl

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
                name for name, recorded_at in self._cache_times.items()
                if (now - recorded_at) > self._result_ttl
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
        result, _ = _fit(result, self._max_entry_bytes)
        name = result.name
        if name not in self._history:
            self._history[name] = deque(maxlen=self._max_results)
            self._history_times[name] = deque(maxlen=self._max_results)
        self._history[name].append(result)
        self._history_times[name].append(time.monotonic())

    async def get_history(self, names: Iterable[str] | None = None) -> dict[str, list[ProbeResult]]:
        """History per probe, oldest first; only the probes in *names* when given."""
        now = time.monotonic()
        out: dict[str, list[ProbeResult]] = {}
        selected = self._history if names is None else {n: self._history[n] for n in names if n in self._history}
        for name, entries in selected.items():
            if self._result_ttl > 0:
                times = self._history_times.get(name, deque())
                valid = [result for result, recorded_at in zip(entries, times) if (now - recorded_at) <= self._result_ttl]
            else:
                valid = list(entries)
            if valid:
                out[name] = valid
        return out

    async def append_alert(self, alert: AlertRecord) -> None:
        self._alerts.append((time.monotonic(), alert))

    async def get_alerts(self, names: Iterable[str] | None = None) -> list[AlertRecord]:
        """Alerts, oldest first; only those of probes in *names* when given."""
        if self._alert_ttl > 0:
            now = time.monotonic()
            while self._alerts and (now - self._alerts[0][0]) > self._alert_ttl:
                self._alerts.popleft()
        wanted = None if names is None else set(names)
        return [alert for _, alert in self._alerts if wanted is None or alert.probe in wanted]
