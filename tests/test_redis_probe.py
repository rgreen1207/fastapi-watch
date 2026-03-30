"""Tests for RedisProbe passive observation via watch decorator."""
import pytest
from fastapi_watch.models import ProbeStatus
from fastapi_watch.probes.redis import RedisProbe


@pytest.mark.asyncio
async def test_no_calls_returns_healthy():
    probe = RedisProbe(name="session-cache")
    result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.details["message"] == "no calls observed yet"


@pytest.mark.asyncio
async def test_successful_call_recorded():
    probe = RedisProbe(name="session-cache")

    @probe.watch
    async def get_session(sid: str):
        return {"user_id": 1}

    await get_session("abc")
    result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.details["call_count"] == 1
    assert result.details["error_count"] == 0
    assert result.details["error_rate"] == 0.0


@pytest.mark.asyncio
async def test_exception_recorded_as_error():
    probe = RedisProbe(name="session-cache")

    @probe.watch
    async def get_session(sid: str):
        raise ConnectionError("redis unavailable")

    with pytest.raises(ConnectionError):
        await get_session("abc")

    result = await probe.check()
    assert result.details["call_count"] == 1
    assert result.details["error_count"] == 1
    assert result.details["consecutive_errors"] == 1


@pytest.mark.asyncio
async def test_error_rate_triggers_unhealthy():
    probe = RedisProbe(name="cache", max_error_rate=0.1)

    @probe.watch
    async def fail():
        raise RuntimeError("error")

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
    probe = RedisProbe()

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
    probe = RedisProbe()

    @probe.watch
    async def get():
        return "val"

    await get()
    result = await probe.check()
    assert result.details["last_rtt_ms"] is not None
    assert result.details["avg_rtt_ms"] is not None


@pytest.mark.asyncio
async def test_sync_function_instrumented():
    probe = RedisProbe()

    @probe.watch
    def get():
        return "val"

    get()
    result = await probe.check()
    assert result.details["call_count"] == 1


@pytest.mark.asyncio
async def test_return_value_preserved():
    probe = RedisProbe()

    @probe.watch
    async def get():
        return {"user_id": 42}

    assert await get() == {"user_id": 42}


@pytest.mark.asyncio
async def test_exceptions_propagate():
    probe = RedisProbe()

    @probe.watch
    async def get():
        raise ValueError("key error")

    with pytest.raises(ValueError, match="key error"):
        await get()


def test_default_name():
    assert RedisProbe().name == "redis"
