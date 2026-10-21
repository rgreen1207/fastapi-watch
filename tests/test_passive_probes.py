"""Shared passive-probe behaviour: defaults, cache attribution, status parsing."""
import asyncio
import functools
import pathlib
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from fastapi_watch import FastAPIRouteProbe, FastAPIWebSocketProbe, HealthRegistry, PassiveProbe
from fastapi_watch.models import ProbeStatus
from fastapi_watch.probes import HttpProbe, NoOpProbe, RedisProbe, TCPProbe
from fastapi_watch.probes import base as base_module


# ---------------------------------------------------------------------------
# One implementation
# ---------------------------------------------------------------------------

def test_route_probe_shares_the_passive_implementation():
    assert issubclass(FastAPIRouteProbe, PassiveProbe)


@pytest.mark.parametrize("cls", [PassiveProbe, HttpProbe, RedisProbe])
def test_passive_probes_accept_poll_interval_and_timeout(cls):
    probe = cls("p", poll_interval_ms=5_000, timeout=2.0)
    assert (probe.poll_interval_ms, probe.timeout) == (5_000, 2.0)


@pytest.mark.asyncio
async def test_passive_probe_reports_requests_per_minute():
    probe = PassiveProbe("p")

    @probe.watch
    async def call():
        await asyncio.sleep(0.01)

    await call()
    await call()
    assert (await probe.check()).details["requests_per_minute"] > 0


def test_no_deprecated_coroutine_check_in_source():
    """asyncio.iscoroutinefunction warns on 3.14+ and is removed in 3.16."""
    src = pathlib.Path(__file__).resolve().parent.parent / "src"
    offenders = [p.name for p in src.rglob("*.py") if "asyncio.iscoroutinefunction" in p.read_text(encoding="utf-8")]
    assert offenders == []


# ---------------------------------------------------------------------------
# Circuit breaker defaults
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("make", [
    lambda: PassiveProbe("p"),
    lambda: HttpProbe("h"),
    lambda: RedisProbe("r"),
    lambda: FastAPIRouteProbe("route"),
    lambda: FastAPIWebSocketProbe("ws"),
])
def test_passive_probes_default_to_breaker_off(make):
    assert make().circuit_breaker_enabled is False


@pytest.mark.parametrize("make", [
    lambda: PassiveProbe("p", circuit_breaker_enabled=True),
    lambda: FastAPIRouteProbe("route", circuit_breaker_enabled=True),
    lambda: FastAPIWebSocketProbe("ws", circuit_breaker_enabled=True),
])
def test_passive_probes_can_opt_in(make):
    assert make().circuit_breaker_enabled is True


@pytest.mark.parametrize("probe", [NoOpProbe(name="n"), TCPProbe("example.invalid", 1)])
def test_active_probes_keep_breaker_on(probe):
    assert probe.circuit_breaker_enabled is True


@pytest.mark.asyncio
async def test_failing_passive_probe_is_not_suspended_and_recovers():
    registry = HealthRegistry(FastAPI(), poll_interval_ms=None, circuit_breaker_threshold=2)
    probe = HttpProbe("h", window_size=5)
    registry.add(probe)

    @probe.watch
    async def call(fail: bool):
        if fail:
            raise ConnectionError

    for _ in range(5):
        with pytest.raises(ConnectionError):
            await call(True)
    for _ in range(3):  # well past the breaker threshold
        [result] = await registry.run_all()
        assert result.status == ProbeStatus.UNHEALTHY
    for _ in range(5):
        await call(False)
    [result] = await registry.run_all()
    assert result.status == ProbeStatus.HEALTHY  # live stats, not a suspended stale result


# ---------------------------------------------------------------------------
# Cache attribution
# ---------------------------------------------------------------------------

def test_sequential_cache_hits_and_misses_are_counted():
    probe = PassiveProbe("p")

    @probe.watch
    @functools.lru_cache(maxsize=8)
    def lookup(key):
        return key

    lookup(1)
    lookup(1)
    lookup(2)
    events = [hit for _, hit in probe._cache_timed_events]
    assert events == [False, True, False]


class _SharedCache:
    """An async 'cached' function whose cache_info() all callers share."""

    def __init__(self):
        self.info = SimpleNamespace(hits=0, misses=0, maxsize=8, currsize=0)
        self.gate = asyncio.Event()

    async def __call__(self, hit: bool):
        if hit:
            self.info.hits += 1
        else:
            self.info.misses += 1
        await self.gate.wait()

    def cache_info(self):
        return SimpleNamespace(**vars(self.info))


@pytest.mark.asyncio
async def test_interleaved_calls_are_not_misattributed():
    cached = _SharedCache()

    async def fn(hit: bool):
        return await cached(hit)

    fn.cache_info = cached.cache_info
    probe = PassiveProbe("p")
    watched = probe.watch(fn)

    # Two calls overlap: one miss, one hit, sharing one cache_info().
    tasks = [asyncio.create_task(watched(False)), asyncio.create_task(watched(True))]
    await asyncio.sleep(0)
    cached.gate.set()
    await asyncio.gather(*tasks)

    # The miss's window saw both counters move, so it is skipped; the hit's
    # window saw only its own hit, so it is recorded.  The old before/after
    # hits check counted both calls as hits.
    assert [hit for _, hit in probe._cache_timed_events] == [True]
    assert probe._call_count == 2


# ---------------------------------------------------------------------------
# Route status parsing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["ok", None, 3.5, True])
async def test_non_int_status_code_on_result_still_records(value):
    probe = FastAPIRouteProbe("r")

    @probe.watch
    async def handler():
        return SimpleNamespace(status_code=value)

    await handler()
    result = await probe.check()
    assert result.details["request_count"] == 1
    assert result.details["last_status_code"] == 200


@pytest.mark.asyncio
async def test_non_int_status_code_on_exception_counts_as_500():
    probe = FastAPIRouteProbe("r")

    class Weird(Exception):
        status_code = "teapot"

    @probe.watch
    async def handler():
        raise Weird

    with pytest.raises(Weird):
        await handler()
    details = (await probe.check()).details
    assert details["last_status_code"] == 500
    assert details["error_count"] == 1


# ---------------------------------------------------------------------------
# check() reads a consistent snapshot under the lock
# ---------------------------------------------------------------------------

class _TrackingLock:
    """A lock that records whether it is held, to see what check() does under it."""

    def __init__(self):
        self._lock = threading.Lock()
        self.held = False
        self.acquisitions = 0

    def __enter__(self):
        self._lock.acquire()
        self.held = True
        self.acquisitions += 1
        return self

    def __exit__(self, *exc):
        self.held = False
        self._lock.release()


async def test_check_copies_state_under_the_lock_and_sorts_outside_it(monkeypatch):
    probe = PassiveProbe("p", slow_call_threshold_ms=0.0)

    @probe.watch
    def call():
        return 1

    for _ in range(5):
        call()
    lock = probe._lock = _TrackingLock()
    sorted_under_lock = []
    real = base_module._calc_percentiles
    monkeypatch.setattr(
        base_module, "_calc_percentiles",
        lambda window, *ps: sorted_under_lock.append(lock.held) or real(window, *ps),
    )

    result = await probe.check()

    assert lock.acquisitions == 1
    assert sorted_under_lock == [False]
    assert result.details["call_count"] == 5


async def test_concurrent_recording_from_threads_gives_consistent_checks():
    probe = PassiveProbe("p", window_size=50, max_error_rate=1.0)

    @probe.watch
    def call(i):
        if i % 4 == 0:
            raise ValueError
        return i

    def worker():
        for i in range(2000):
            try:
                call(i)
            except ValueError:
                pass

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        details = (await probe.check()).details
        assert details["error_count"] <= details["call_count"]
        assert sum(details.get("error_types", {}).values()) <= 50
    for t in threads:
        t.join()

    details = (await probe.check()).details
    assert details["call_count"] == 8000
    assert details["error_count"] == 2000
