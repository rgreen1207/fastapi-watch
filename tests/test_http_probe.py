"""Tests for HttpProbe passive observation via watch decorator."""
import pytest
from fastapi_watch.models import ProbeStatus
from fastapi_watch.probes.http import HttpProbe


@pytest.mark.asyncio
async def test_no_calls_returns_healthy():
    probe = HttpProbe(name="stripe")
    result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.details["message"] == "no calls observed yet"


@pytest.mark.asyncio
async def test_successful_call_recorded():
    probe = HttpProbe(name="stripe")

    @probe.watch
    async def call():
        return {"id": "ch_123"}

    await call()
    result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.details["call_count"] == 1
    assert result.details["error_count"] == 0
    assert result.details["error_rate"] == 0.0


@pytest.mark.asyncio
async def test_exception_recorded_as_error():
    probe = HttpProbe(name="stripe")

    @probe.watch
    async def call():
        raise ConnectionError("timeout")

    with pytest.raises(ConnectionError):
        await call()

    result = await probe.check()
    assert result.details["call_count"] == 1
    assert result.details["error_count"] == 1
    assert result.details["consecutive_errors"] == 1


@pytest.mark.asyncio
async def test_error_rate_triggers_unhealthy():
    probe = HttpProbe(name="stripe", max_error_rate=0.1)

    @probe.watch
    async def fail():
        raise RuntimeError("500")

    @probe.watch
    async def succeed():
        return "ok"

    for _ in range(9):
        with pytest.raises(RuntimeError):
            await fail()
    await succeed()

    result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY
    assert "error rate" in result.error


@pytest.mark.asyncio
async def test_consecutive_errors_reset_on_success():
    probe = HttpProbe(name="stripe")

    @probe.watch
    async def fail():
        raise RuntimeError()

    @probe.watch
    async def succeed():
        return "ok"

    for _ in range(3):
        with pytest.raises(RuntimeError):
            await fail()

    assert probe._consecutive_errors == 3
    await succeed()
    assert probe._consecutive_errors == 0


@pytest.mark.asyncio
async def test_latency_recorded():
    probe = HttpProbe(name="stripe")

    @probe.watch
    async def call():
        return "ok"

    await call()
    result = await probe.check()
    assert result.details["last_rtt_ms"] is not None
    assert result.details["avg_rtt_ms"] is not None


@pytest.mark.asyncio
async def test_avg_rtt_triggers_unhealthy():
    probe = HttpProbe(name="stripe", max_avg_rtt_ms=1.0)

    import asyncio

    @probe.watch
    async def slow_call():
        await asyncio.sleep(0.05)
        return "ok"

    await slow_call()
    result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY
    assert "avg RTT" in result.error


@pytest.mark.asyncio
async def test_sync_function_instrumented():
    probe = HttpProbe(name="external")

    @probe.watch
    def call():
        return "ok"

    call()
    result = await probe.check()
    assert result.details["call_count"] == 1


@pytest.mark.asyncio
async def test_exceptions_still_propagate():
    probe = HttpProbe(name="stripe")

    @probe.watch
    async def call():
        raise ValueError("bad response")

    with pytest.raises(ValueError, match="bad response"):
        await call()


@pytest.mark.asyncio
async def test_return_value_preserved():
    probe = HttpProbe(name="stripe")

    @probe.watch
    async def call():
        return {"id": "ch_123", "status": "succeeded"}

    result = await call()
    assert result == {"id": "ch_123", "status": "succeeded"}


# ---------------------------------------------------------------------------
# Percentiles (p50, p99)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p50_and_p99_rtt_present():
    probe = HttpProbe(name="stripe")

    @probe.watch
    async def call():
        return "ok"

    for _ in range(3):
        await call()

    result = await probe.check()
    assert result.details["p50_rtt_ms"] is not None
    assert result.details["p99_rtt_ms"] is not None


# ---------------------------------------------------------------------------
# Slow calls
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_slow_calls_counted():
    import asyncio

    probe = HttpProbe(name="stripe", slow_call_threshold_ms=1)

    @probe.watch
    async def slow_call():
        await asyncio.sleep(0.05)
        return "ok"

    await slow_call()
    result = await probe.check()
    assert result.details["slow_calls"] == 1


@pytest.mark.asyncio
async def test_slow_calls_absent_without_threshold():
    probe = HttpProbe(name="stripe")

    @probe.watch
    async def call():
        return "ok"

    await call()
    result = await probe.check()
    assert "slow_calls" not in result.details


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_error_types_tracked():
    probe = HttpProbe(name="stripe")

    @probe.watch
    async def call():
        raise ConnectionError("refused")

    with pytest.raises(ConnectionError):
        await call()

    result = await probe.check()
    assert result.details["error_types"].get("ConnectionError") == 1


# ---------------------------------------------------------------------------
# Cache hit / miss
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_hit_miss_recording():
    probe = HttpProbe(name="stripe")

    probe.record_cache_hit()
    probe.record_cache_miss()
    probe.record_cache_miss()

    @probe.watch
    async def call():
        return "ok"

    await call()
    result = await probe.check()
    assert result.details["cache_hits"] == 1
    assert result.details["cache_misses"] == 2


@pytest.mark.asyncio
async def test_auto_cache_tracking_lru_cache():
    """@probe.watch on an @lru_cache function auto-tracks hits/misses."""
    from functools import lru_cache
    probe = HttpProbe(name="stripe")

    @lru_cache(maxsize=128)
    def get_value(key: int) -> int:
        return key * 2

    watched = probe.watch(get_value)

    watched(1)  # miss
    watched(1)  # hit
    watched(2)  # miss

    result = await probe.check()
    assert result.details["cache_hits"] == 1
    assert result.details["cache_misses"] == 2
    assert result.details["cache_maxsize"] == 128
    assert result.details["cache_currsize"] == 2


@pytest.mark.asyncio
async def test_auto_cache_tracking_async_cache_info():
    """@probe.watch on an async function with cache_info() auto-tracks hits/misses."""
    probe = HttpProbe(name="stripe")
    _cache: dict = {}
    hits = 0
    misses = 0

    async def fetch(key: int) -> int:
        nonlocal hits, misses
        if key in _cache:
            hits += 1
        else:
            misses += 1
            _cache[key] = key * 2
        return _cache[key]

    from collections import namedtuple
    CacheInfo = namedtuple("CacheInfo", ["hits", "misses", "maxsize", "currsize"])
    fetch.cache_info = lambda: CacheInfo(hits, misses, 128, len(_cache))  # type: ignore[attr-defined]

    watched = probe.watch(fetch)

    await watched(1)  # miss
    await watched(1)  # hit
    await watched(2)  # miss

    result = await probe.check()
    assert result.details["cache_hits"] == 1
    assert result.details["cache_misses"] == 2


@pytest.mark.asyncio
async def test_cache_reporting_false_skips_tracking():
    """cache_reporting=False disables auto cache tracking even on @lru_cache functions."""
    from functools import lru_cache
    probe = HttpProbe(name="stripe", cache_reporting=False)

    @lru_cache(maxsize=64)
    def get_value(key: int) -> int:
        return key

    watched = probe.watch(get_value)
    watched(1)
    watched(1)

    result = await probe.check()
    assert "cache_hits" not in (result.details or {})


@pytest.mark.asyncio
async def test_cache_window_auto_sized_from_maxsize():
    """Window auto-sizes to cache maxsize when cache_window_size is not set."""
    from functools import lru_cache
    probe = HttpProbe(name="stripe")

    @lru_cache(maxsize=5)
    def get_value(key: int) -> int:
        return key

    watched = probe.watch(get_value)
    assert probe._cache_window_size == 5


@pytest.mark.asyncio
async def test_cache_time_window():
    """cache_time_window_s filters events older than T seconds."""
    import time as _time
    probe = HttpProbe(name="stripe", cache_time_window_s=60)

    # Inject an old event directly (>60s ago)
    probe._cache_timed_events.append((_time.monotonic() - 120, True))
    # And a recent hit/miss
    probe.record_cache_hit()
    probe.record_cache_miss()

    @probe.watch
    async def call():
        return "ok"

    await call()
    result = await probe.check()
    # Old event excluded; only the recent hit and miss count
    assert result.details["cache_hits"] == 1
    assert result.details["cache_misses"] == 1


# ---------------------------------------------------------------------------
# last_error_at / last_success_at
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_last_error_at_set_after_exception():
    probe = HttpProbe(name="stripe")

    @probe.watch
    async def fail():
        raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        await fail()

    result = await probe.check()
    assert "last_error_at" in result.details


@pytest.mark.asyncio
async def test_last_error_at_absent_before_any_error():
    probe = HttpProbe(name="stripe")

    @probe.watch
    async def call():
        return "ok"

    await call()
    result = await probe.check()
    assert "last_error_at" not in result.details


@pytest.mark.asyncio
async def test_last_success_at_absent_when_not_mostly_failing():
    probe = HttpProbe(name="stripe", window_size=10)

    @probe.watch
    async def call(fail: bool):
        if fail:
            raise RuntimeError("err")
        return "ok"

    for _ in range(5):
        await call(fail=False)
    for _ in range(5):
        with pytest.raises(RuntimeError):
            await call(fail=True)

    result = await probe.check()
    # 50% failure — below the 99% threshold
    assert "last_success_at" not in result.details


@pytest.mark.asyncio
async def test_last_success_at_shown_when_mostly_failing():
    probe = HttpProbe(name="stripe", window_size=10)

    @probe.watch
    async def call(fail: bool):
        if fail:
            raise RuntimeError("err")
        return "ok"

    await call(fail=False)  # records last_success_at
    for _ in range(10):
        with pytest.raises(RuntimeError):
            await call(fail=True)  # fills window with failures

    result = await probe.check()
    assert "last_success_at" in result.details
