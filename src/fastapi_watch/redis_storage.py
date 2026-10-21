"""Redis storage backend with hard size bounds.

``RedisProbeStorage`` shares probe results and alerts across processes through
a Redis instance, usually the one the application already uses.  It is built
so that health monitoring never becomes a load on that Redis:

* no ``KEYS`` or ``SCAN`` — probe names are tracked in an index set;
* every write is one ``MULTI/EXEC`` round trip, so a failure part-way through
  can never leave a key without its expiry;
* every collection is capped by count, by age (per entry, not per key) and,
  for history, by bytes per entry.

The ``redis`` package is not imported here: pass in any ``redis.asyncio``
client (``pip install "fastapi-watch[redis]"``).
"""
from __future__ import annotations

import asyncio
import itertools
import math
import os
import time
from collections.abc import Iterable
from typing import Any

from pydantic_core import to_json

from .models import AlertRecord, ProbeResult

# Key-level PEXPIRE is a safety net on top of per-entry pruning: it removes a
# whole collection once nothing has been written to it for ttl + this margin.
_EXPIRY_MARGIN_MS = 60_000
_MIN_ENTRY_BYTES = 512
_TRUNCATED = "_truncated"


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, (bytes, bytearray)) else value


def _payload(member: Any) -> Any:
    """Strip the uniqueness tag (everything up to the first ``:``) from a sorted-set member."""
    if isinstance(member, (bytes, bytearray)):
        return member[member.index(b":") + 1:]
    return member[member.index(":") + 1:]


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
        max_entry_bytes: Upper bound on one stored history entry.  Larger
            entries lose their biggest ``details`` values first and are marked
            ``details["_truncated"] = True``.  Latest results are stored in full.

    When passed to :class:`~fastapi_watch.HealthRegistry`, the registry applies
    its own ``history_size``, ``result_ttl_seconds``, ``alert_ttl_seconds`` and
    ``max_alerts`` through :meth:`configure`.

    Keys (``P`` = *prefix*): ``P:probes`` (set of probe names), ``P:latest:<name>``
    (string), ``P:history:<name>`` (sorted set scored by ``checked_at``) and
    ``P:alerts`` (sorted set scored by ``timestamp``) and short-lived
    ``P:alert-claim:<key>`` markers written by :meth:`claim_alert`.

    **Size bound.**  Redis memory used by this backend is at most::

        N × (max_results × max_entry_bytes + L) + max_alerts × A + N × len(name) + C

    plus Redis' per-key overhead, where ``N`` is the number of probe names
    written within the last ``result_ttl_seconds`` (or since the last
    :meth:`prune` when that TTL is ``0``), ``L`` the size of one probe's
    serialized latest result and ``A`` the size of one serialized alert (about
    150 bytes plus the probe name) and ``C`` the alert claims live at once (one
    small key per claimed alert, each expiring after its claim TTL).  Each history entry is cut to
    ``max_entry_bytes`` (unless the probe name alone exceeds it); every write
    trims its collection by count and by age in the same transaction that adds
    to it; and once a collection stops being written it expires as a whole
    shortly after its TTL.
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
    ) -> None:
        if not prefix:
            raise ValueError("prefix must be a non-empty string")
        if max_entry_bytes < _MIN_ENTRY_BYTES:
            raise ValueError(f"max_entry_bytes must be at least {_MIN_ENTRY_BYTES}")
        self._redis = redis
        self._prefix = prefix
        self._max_entry_bytes = int(max_entry_bytes)
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
        if result_ttl_seconds < 0 or alert_ttl_seconds < 0:
            raise ValueError("TTLs must be >= 0 (0 disables time-based expiry)")
        self._max_results = max(1, int(max_results))
        self._max_alerts = max(1, int(max_alerts))
        self._result_ttl = float(result_ttl_seconds)
        self._alert_ttl = float(alert_ttl_seconds)

    # ------------------------------------------------------------------- keys

    @property
    def _index_key(self) -> str:
        return f"{self._prefix}:probes"

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

    async def _names(self) -> list[str]:
        return [_text(n) for n in await self._redis.smembers(self._index_key)]

    def _history_entry(self, result: ProbeResult) -> bytes:
        """Serialize *result* as a tagged sorted-set member of at most max_entry_bytes."""
        tag = self._tag()
        budget = self._max_entry_bytes - len(tag)
        raw = to_json(result)
        if len(raw) > budget:
            raw = self._shrink(result, budget)
        return tag + raw

    @staticmethod
    def _shrink(result: ProbeResult, budget: int) -> bytes:
        details = {k: v for k, v in (result.details or {}).items() if k != _TRUNCATED}
        sizes = {k: len(to_json(k)) + len(to_json(v)) + 2 for k, v in details.items()}
        excess = len(to_json(result)) + 20 - budget  # 20 ≈ the "_truncated":true marker
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
            return raw
        # Details alone were not the problem: shed free-text fields too.
        candidate = result.model_copy(
            update={"details": {_TRUNCATED: True}, "description": None, "tags": []}
        )
        raw = to_json(candidate)
        if len(raw) > budget and candidate.error:
            error = candidate.error.encode("utf-8")
            keep = max(0, len(error) - (len(raw) - budget) - 3)
            short = error[:keep].decode("utf-8", "ignore") + "..."
            raw = to_json(candidate.model_copy(update={"error": short}))
        return raw  # may exceed the budget only if the probe name alone does

    async def _reap(self, names: Iterable[str]) -> None:
        """Remove index members whose latest result and history are both gone.

        SREM and the existence check run in one transaction, and survivors are
        re-added, so a concurrent write can never be left out of the index.
        """
        names = list(names)
        if not names:
            return
        expiring = self._result_ttl > 0
        cutoff = time.time() - self._result_ttl
        async with self._redis.pipeline(transaction=True) as pipe:
            for name in names:
                if expiring:  # empties, and so deletes, fully expired history
                    pipe.zremrangebyscore(self._history_key(name), "-inf", f"({cutoff}")
                pipe.srem(self._index_key, name)
                pipe.exists(self._latest_key(name), self._history_key(name))
            replies = await pipe.execute()
        step = 3 if expiring else 2
        alive = [name for name, count in zip(names, replies[step - 1::step]) if count]
        if alive:
            await self._redis.sadd(self._index_key, *alive)

    # ----------------------------------------------------------------- latest

    def _fresh(self, result: ProbeResult, now: float) -> bool:
        return not (
            self._result_ttl > 0
            and result.checked_at is not None
            and result.checked_at.timestamp() < now - self._result_ttl
        )

    async def get_latest(self, name: str) -> ProbeResult | None:
        raw = await self._redis.get(self._latest_key(name))
        if raw is None:
            return None
        result = ProbeResult.model_validate_json(raw)
        return result if self._fresh(result, time.time()) else None

    async def get_all_latest(self) -> dict[str, ProbeResult]:
        names = await self._names()
        if not names:
            return {}
        values = await self._redis.mget([self._latest_key(n) for n in names])
        now = time.time()
        out: dict[str, ProbeResult] = {}
        missing: list[str] = []
        for name, raw in zip(names, values):
            if raw is None:
                missing.append(name)
                continue
            result = ProbeResult.model_validate_json(raw)
            if self._fresh(result, now):
                out[name] = result
        await self._reap(missing)
        return out

    async def set_latest(self, result: ProbeResult) -> None:
        ttl_ms = math.ceil(self._result_ttl * 1000)
        async with self._redis.pipeline(transaction=True) as pipe:
            if ttl_ms > 0:
                pipe.set(self._latest_key(result.name), to_json(result), px=ttl_ms)
            else:
                pipe.set(self._latest_key(result.name), to_json(result))
            pipe.sadd(self._index_key, result.name)
            if ttl_ms > 0:
                pipe.pexpire(self._index_key, ttl_ms + _EXPIRY_MARGIN_MS)
            await pipe.execute()

    def clear_latest(self) -> None:
        """Delete every latest result.  Sync per the protocol: inside a running
        loop the deletion is scheduled on it; otherwise it runs to completion."""

        async def _clear() -> None:
            names = await self._names()
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
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.zadd(key, {self._history_entry(result): _score(result.checked_at)})
            if ttl_ms > 0:
                pipe.zremrangebyscore(key, "-inf", f"({now - self._result_ttl}")
            pipe.zremrangebyrank(key, 0, -(self._max_results + 1))
            pipe.sadd(self._index_key, result.name)
            if ttl_ms > 0:
                pipe.pexpire(key, ttl_ms + _EXPIRY_MARGIN_MS)
                pipe.pexpire(self._index_key, ttl_ms + _EXPIRY_MARGIN_MS)
            await pipe.execute()

    async def get_history(self) -> dict[str, list[ProbeResult]]:
        names = await self._names()
        if not names:
            return {}
        cutoff = self._cutoff(self._result_ttl, time.time())
        async with self._redis.pipeline(transaction=False) as pipe:
            for name in names:
                pipe.zrevrangebyscore(self._history_key(name), "+inf", cutoff, start=0, num=self._max_results)
            replies = await pipe.execute()
        out: dict[str, list[ProbeResult]] = {}
        empty: list[str] = []
        for name, members in zip(names, replies):
            if members:
                out[name] = [ProbeResult.model_validate_json(_payload(m)) for m in reversed(members)]
            else:
                empty.append(name)
        await self._reap(empty)
        return out

    # ----------------------------------------------------------------- alerts

    async def append_alert(self, alert: AlertRecord) -> None:
        key = self._alerts_key
        ttl_ms = math.ceil(self._alert_ttl * 1000)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.zadd(key, {self._tag() + to_json(alert): _score(alert.timestamp)})
            if ttl_ms > 0:
                pipe.zremrangebyscore(key, "-inf", f"({time.time() - self._alert_ttl}")
            pipe.zremrangebyrank(key, 0, -(self._max_alerts + 1))
            if ttl_ms > 0:
                pipe.pexpire(key, ttl_ms + _EXPIRY_MARGIN_MS)
            await pipe.execute()

    async def get_alerts(self) -> list[AlertRecord]:
        cutoff = self._cutoff(self._alert_ttl, time.time())
        members = await self._redis.zrevrangebyscore(
            self._alerts_key, "+inf", cutoff, start=0, num=self._max_alerts
        )
        return [AlertRecord.model_validate_json(_payload(m)) for m in reversed(members)]

    async def claim_alert(self, key: str, ttl_seconds: float) -> bool:
        """Return True if this caller is first to claim *key* within *ttl_seconds*.

        One ``SET NX PX``, so exactly one of several replicas sharing the prefix
        wins.  The claim key always expires; ``ttl_seconds <= 0`` claims
        without storing anything.
        """
        ttl_ms = math.ceil(ttl_seconds * 1000)
        if ttl_ms <= 0:
            return True
        return bool(await self._redis.set(f"{self._prefix}:alert-claim:{key}", 1, nx=True, px=ttl_ms))

    # ------------------------------------------------------------------ prune

    async def prune(self, keep: set[str]) -> None:
        """Delete the latest result and history of every probe not in *keep*."""
        drop = [name for name in await self._names() if name not in keep]
        if not drop:
            return
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.delete(*(self._latest_key(n) for n in drop), *(self._history_key(n) for n in drop))
            pipe.srem(self._index_key, *drop)
            await pipe.execute()
