"""Redis storage backend with hard size bounds.

``RedisProbeStorage`` shares probe results and alerts across processes through
a Redis instance, usually the one the application already uses.  It is built
so that health monitoring never becomes a load on that Redis:

* no ``KEYS`` or ``SCAN`` — probe names are tracked in an index;
* every write is one ``MULTI/EXEC`` round trip, so a failure part-way through
  can never leave a key without its expiry;
* every collection is capped by count, by age (per entry, not per key) and
  by bytes per entry;
* at most ``max_connections`` commands are in flight at once, so many probes
  checked together cannot drain a connection pool shared with the app.

The ``redis`` package is not imported here: pass in any ``redis.asyncio``
client (``pip install "fastapi-watch[redis]"``).
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import math
import os
import time
from collections.abc import AsyncIterator
from typing import Any

from pydantic_core import to_json

from .models import AlertRecord, ProbeResult
from .storage import _MIN_ENTRY_BYTES, _fit

# claim_alert: replace the value unless it is already this one, atomically.
_CLAIM_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
return 1
"""

# Key-level PEXPIRE is a safety net on top of per-entry pruning: it removes a
# whole collection once nothing has been written to it for ttl + this margin.
_EXPIRY_MARGIN_MS = 60_000


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, (bytes, bytearray)) else value


def _payload(member: Any) -> Any:
    """Strip the uniqueness tag (everything up to the first ``:``) from a sorted-set member."""
    if isinstance(member, (bytes, bytearray)):
        return member[member.index(b":") + 1:]
    return member[member.index(":") + 1:]


def _decode(model: Any, raw: Any, *, tagged: bool = False) -> Any:
    """Parse one stored value as *model*, or None if this version cannot read it.

    Other versions may share the prefix (a rolling deploy) and write values
    this one does not know, e.g. a new status; one such entry must not fail
    every read until it ages out.
    """
    try:
        return model.model_validate_json(_payload(raw) if tagged else raw)
    except ValueError:  # includes pydantic's ValidationError
        return None


def _score(moment: Any) -> float:
    return moment.timestamp() if moment is not None else time.time()


class RedisProbeStorage:
    """Store probe results and alerts in Redis without unbounded growth.

    Usage::

        import redis.asyncio as redis
        from fastapi_watch import HealthRegistry, RedisProbeStorage

        client = redis.from_url("redis://localhost:6379/0")
        registry = HealthRegistry(app, storage=RedisProbeStorage(client, prefix="myapp:health"))

    Works with clients created with or without ``decode_responses=True``.

    Args:
        redis: A ``redis.asyncio.Redis`` client.
        prefix: Namespace for every key.  Give each application sharing a Redis
            its own prefix.  On Redis Cluster wrap it in braces
            (``"{myapp:health}"``) so all keys share a hash slot.
        max_results: History entries kept per probe.
        result_ttl_seconds: Age after which latest results and history entries
            are dropped.  ``0`` disables time-based expiry.
        alert_ttl_seconds: Age after which alerts are dropped.  ``0`` disables
            time-based expiry.
        max_alerts: Alerts kept in total.
        max_entry_bytes: Upper bound on one stored latest result or history
            entry.  Larger ones lose their biggest ``details`` values first
            and are marked ``details["_truncated"] = True``.
        max_connections: Most commands this backend has in flight at once,
            i.e. connections it takes from the client's pool at once (default
            ``4``; ``None`` = no limit).  Probes checked together queue for a
            slot instead of each taking connections, so they cannot exhaust a
            pool the application shares.

    The retention arguments above apply unless :class:`~fastapi_watch.HealthRegistry`
    is given ``history_size``, ``result_ttl_seconds``, ``alert_ttl_seconds`` or
    ``max_alerts`` explicitly.  When at least one of those is passed, the
    registry calls :meth:`configure` with all four of its effective values
    (its defaults for the ones not passed); otherwise this backend keeps its
    constructor values.

    Applications and replicas may share a prefix safely: the registry only
    ever calls :meth:`forget` with names it wrote itself and no longer
    registers.  Two apps registering the same probe name on one prefix do
    share that probe's data, though, so a distinct prefix per app is still
    recommended.

    Keys (``P`` = *prefix*): ``P:names`` (sorted set of probe names scored by
    last write time), ``P:latest:<name>`` (string), ``P:history:<name>``
    (sorted set scored by ``checked_at``), ``P:alerts`` (sorted set scored by
    ``timestamp``) and short-lived ``P:alert-claim:<key>`` strings written by
    :meth:`claim_alert` (the registry uses one per probe, holding its last
    announced status).

    **Size bound.**  Redis memory used by this backend is at most::

        N × ((max_results + 1) × max_entry_bytes + len(name)) + max_alerts × A + C

    plus Redis' per-key overhead, where ``N`` is the number of probe names
    written within the last ``result_ttl_seconds`` (or, when that TTL is
    ``0``, ever written and not yet removed by :meth:`forget`), ``A`` the size
    of one serialized alert (about 150 bytes plus the probe name) and ``C``
    the alert claims live at once (one small key per probe, each expiring
    after its claim TTL).  Each latest result and history entry is
    cut to ``max_entry_bytes`` (unless the probe name alone exceeds it); every
    write trims its collection, and the index, by count and by age in the
    same transaction that adds to it; and once a collection stops being
    written it expires as a whole shortly after its TTL.
    """

    def __init__(
        self,
        redis: Any,
        *,
        prefix: str = "fastapi-watch",
        max_results: int = 120,
        result_ttl_seconds: float = 7200.0,
        alert_ttl_seconds: float = 259200.0,
        max_alerts: int = 120,
        max_entry_bytes: int = 8192,
        max_connections: int | None = 4,
    ) -> None:
        if not prefix:
            raise ValueError("prefix must be a non-empty string")
        if max_entry_bytes < _MIN_ENTRY_BYTES:
            raise ValueError(f"max_entry_bytes must be at least {_MIN_ENTRY_BYTES}")
        if max_connections is not None and max_connections < 1:
            raise ValueError("max_connections must be at least 1 or None")
        self._redis = redis
        self._prefix = prefix
        self._max_entry_bytes = int(max_entry_bytes)
        self._max_connections = max_connections
        # Created per event loop: an asyncio.Semaphore binds to the first loop it waits on.
        self._slots: asyncio.Semaphore | None = None
        self._slots_loop: asyncio.AbstractEventLoop | None = None
        # Earlier versions indexed names in a set at P:probes; each instance deletes it once.
        self._legacy_index_dropped = False
        self._pending: set[asyncio.Task] = set()  # strong refs to scheduled clear_latest tasks
        # Members are "<sequence><instance token>:<json>" so identical payloads
        # stay distinct and ties on score keep insertion order.
        self._seq = itertools.count()
        self._token = os.urandom(4).hex()
        self.configure(
            max_results=max_results,
            result_ttl_seconds=result_ttl_seconds,
            alert_ttl_seconds=alert_ttl_seconds,
            max_alerts=max_alerts,
        )

    # ------------------------------------------------------------------ setup

    def configure(
        self,
        *,
        max_results: int,
        result_ttl_seconds: float,
        alert_ttl_seconds: float,
        max_alerts: int,
    ) -> None:
        """Apply retention settings.  Reads honour them at once; each collection
        is trimmed to them on its next write."""
        for ttl in (result_ttl_seconds, alert_ttl_seconds):
            if not 0 <= ttl < math.inf:  # also rejects NaN, which PX cannot express
                raise ValueError("TTLs must be finite and >= 0 (0 disables time-based expiry)")
        self._max_results = max(1, int(max_results))
        self._max_alerts = max(1, int(max_alerts))
        self._result_ttl = float(result_ttl_seconds)
        self._alert_ttl = float(alert_ttl_seconds)

    # ------------------------------------------------------------------- keys

    @property
    def _index_key(self) -> str:
        return f"{self._prefix}:names"

    @property
    def _alerts_key(self) -> str:
        return f"{self._prefix}:alerts"

    def _latest_key(self, name: str) -> str:
        return f"{self._prefix}:latest:{name}"

    def _history_key(self, name: str) -> str:
        return f"{self._prefix}:history:{name}"

    # ---------------------------------------------------------------- helpers

    def _tag(self) -> bytes:
        return f"{next(self._seq):012x}{self._token}:".encode()

    @staticmethod
    def _cutoff(ttl: float, now: float) -> float | str:
        return now - ttl if ttl > 0 else "-inf"

    @contextlib.asynccontextmanager
    async def _slot(self) -> AsyncIterator[None]:
        """Hold one of the ``max_connections`` slots for the duration of a command."""
        if self._max_connections is None:
            yield
            return
        loop = asyncio.get_running_loop()
        if self._slots is None or self._slots_loop is not loop:
            self._slots, self._slots_loop = asyncio.Semaphore(self._max_connections), loop
        async with self._slots:
            yield

    async def _names(self, now: float) -> list[str]:
        """Probe names written within the result TTL (all indexed names for TTL 0)."""
        cutoff = self._cutoff(self._result_ttl, now)
        return [_text(n) for n in await self._redis.zrangebyscore(self._index_key, cutoff, "+inf")]

    def _index(self, pipe: Any, name: str, now: float, ttl_ms: int) -> None:
        """Queue on *pipe*: record a write of *name* in the index and trim the
        index to names written within the TTL, so it cannot outgrow them."""
        pipe.zadd(self._index_key, {name: now})
        if ttl_ms > 0:
            pipe.zremrangebyscore(self._index_key, "-inf", f"({now - self._result_ttl}")
            pipe.pexpire(self._index_key, ttl_ms + _EXPIRY_MARGIN_MS)
        else:
            pipe.persist(self._index_key)
        if not self._legacy_index_dropped:
            self._legacy_index_dropped = True
            pipe.delete(f"{self._prefix}:probes")

    def _history_entry(self, result: ProbeResult) -> bytes:
        """Serialize *result* as a tagged sorted-set member of at most max_entry_bytes."""
        tag = self._tag()
        return tag + _fit(result, self._max_entry_bytes - len(tag))[1]

    # ----------------------------------------------------------------- latest

    def _fresh(self, result: ProbeResult, now: float) -> bool:
        return not (
            self._result_ttl > 0
            and result.checked_at is not None
            and result.checked_at.timestamp() < now - self._result_ttl
        )

    async def get_latest(self, name: str) -> ProbeResult | None:
        async with self._slot():
            raw = await self._redis.get(self._latest_key(name))
        result = _decode(ProbeResult, raw) if raw is not None else None
        return result if result is not None and self._fresh(result, time.time()) else None

    async def get_all_latest(self) -> dict[str, ProbeResult]:
        now = time.time()
        async with self._slot():
            names = await self._names(now)
            if not names:
                return {}
            values = await self._redis.mget([self._latest_key(n) for n in names])
        out: dict[str, ProbeResult] = {}
        for name, raw in zip(names, values):
            result = _decode(ProbeResult, raw) if raw is not None else None
            if result is not None and self._fresh(result, now):
                out[name] = result
        return out

    async def set_latest(self, result: ProbeResult) -> None:
        now = time.time()
        ttl_ms = math.ceil(self._result_ttl * 1000)
        value = _fit(result, self._max_entry_bytes)[1]
        async with self._slot(), self._redis.pipeline(transaction=True) as pipe:
            if ttl_ms > 0:
                pipe.set(self._latest_key(result.name), value, px=ttl_ms)
            else:
                pipe.set(self._latest_key(result.name), value)
            self._index(pipe, result.name, now, ttl_ms)
            await pipe.execute()

    def clear_latest(self) -> None:
        """Delete every latest result.  Sync per the protocol: inside a running
        loop the deletion is scheduled on it; otherwise it runs to completion."""

        async def _clear() -> None:
            async with self._slot():
                names = await self._names(time.time())
                if names:
                    await self._redis.delete(*(self._latest_key(n) for n in names))

        async def _clear_and_release() -> None:
            # Connections opened on this temporary loop cannot be used from the
            # application's loop later, so close them before the loop ends.
            try:
                await _clear()
            finally:
                pool = getattr(self._redis, "connection_pool", None)
                if pool is not None:
                    await pool.disconnect()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(_clear_and_release())
            return
        task = loop.create_task(_clear())
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    # ---------------------------------------------------------------- history

    async def append_history(self, result: ProbeResult) -> None:
        key = self._history_key(result.name)
        now = time.time()
        ttl_ms = math.ceil(self._result_ttl * 1000)
        entry = self._history_entry(result)
        async with self._slot(), self._redis.pipeline(transaction=True) as pipe:
            pipe.zadd(key, {entry: _score(result.checked_at)})
            if ttl_ms > 0:
                pipe.zremrangebyscore(key, "-inf", f"({now - self._result_ttl}")
            pipe.zremrangebyrank(key, 0, -(self._max_results + 1))
            if ttl_ms > 0:
                pipe.pexpire(key, ttl_ms + _EXPIRY_MARGIN_MS)
            else:  # an expiry left by an earlier non-zero TTL must not delete it
                pipe.persist(key)
            self._index(pipe, result.name, now, ttl_ms)
            await pipe.execute()

    async def get_history(self) -> dict[str, list[ProbeResult]]:
        now = time.time()
        cutoff = self._cutoff(self._result_ttl, now)
        async with self._slot():
            names = await self._names(now)
            if not names:
                return {}
            async with self._redis.pipeline(transaction=False) as pipe:
                for name in names:
                    pipe.zrevrangebyscore(self._history_key(name), "+inf", cutoff, start=0, num=self._max_results)
                replies = await pipe.execute()
        history = {}
        for name, members in zip(names, replies):
            entries = [r for r in (_decode(ProbeResult, m, tagged=True) for m in reversed(members)) if r is not None]
            if entries:
                history[name] = entries
        return history

    # ----------------------------------------------------------------- alerts

    async def append_alert(self, alert: AlertRecord) -> None:
        key = self._alerts_key
        ttl_ms = math.ceil(self._alert_ttl * 1000)
        async with self._slot(), self._redis.pipeline(transaction=True) as pipe:
            pipe.zadd(key, {self._tag() + to_json(alert): _score(alert.timestamp)})
            if ttl_ms > 0:
                pipe.zremrangebyscore(key, "-inf", f"({time.time() - self._alert_ttl}")
            pipe.zremrangebyrank(key, 0, -(self._max_alerts + 1))
            if ttl_ms > 0:
                pipe.pexpire(key, ttl_ms + _EXPIRY_MARGIN_MS)
            else:
                pipe.persist(key)
            await pipe.execute()

    async def get_alerts(self) -> list[AlertRecord]:
        cutoff = self._cutoff(self._alert_ttl, time.time())
        async with self._slot():
            members = await self._redis.zrevrangebyscore(
                self._alerts_key, "+inf", cutoff, start=0, num=self._max_alerts
            )
        alerts = (_decode(AlertRecord, m, tagged=True) for m in reversed(members))
        return [a for a in alerts if a is not None]

    async def claim_alert(self, key: str, value: str, ttl_seconds: float) -> bool:
        """Store *value* under *key* for *ttl_seconds*; return False only if the
        key already held this *value*.

        One Lua script (``GET``, compare, ``SET PX``), so of several replicas
        sharing the prefix exactly one gets True for each value, and a new
        value (a probe changing status again) is always claimable.  The claim
        key always expires; ``ttl_seconds <= 0`` claims without storing anything.
        """
        ttl_ms = math.ceil(ttl_seconds * 1000)
        if ttl_ms <= 0:
            return True
        async with self._slot():
            claimed = await self._redis.eval(_CLAIM_SCRIPT, 1, f"{self._prefix}:alert-claim:{key}", value, ttl_ms)
        return bool(claimed)

    # ----------------------------------------------------------------- forget

    async def forget(self, names: set[str]) -> None:
        """Delete the latest result, history and index entry of exactly the probes in *names*.

        Nothing else under the prefix is touched, so apps and replicas sharing
        it keep their data.
        """
        drop = sorted(names)
        if not drop:
            return
        async with self._slot(), self._redis.pipeline(transaction=True) as pipe:
            pipe.delete(*(self._latest_key(n) for n in drop), *(self._history_key(n) for n in drop))
            pipe.zrem(self._index_key, *drop)
            await pipe.execute()
