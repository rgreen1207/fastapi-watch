"""Tests for MongoProbe passive observation via watch decorator."""
import pytest
from fastapi_watch.models import ProbeStatus
from fastapi_watch.probes.mongo import MongoProbe


@pytest.mark.asyncio
async def test_no_calls_returns_healthy():
    probe = MongoProbe(name="mongodb")
    result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.details["message"] == "no calls observed yet"


@pytest.mark.asyncio
async def test_successful_call_recorded():
    probe = MongoProbe(name="mongodb")

    @probe.watch
    async def get_doc(doc_id: str):
        return {"_id": doc_id, "value": 42}

    await get_doc("abc")
    result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.details["call_count"] == 1
    assert result.details["error_count"] == 0


@pytest.mark.asyncio
async def test_exception_recorded_as_error():
    probe = MongoProbe()

    @probe.watch
    async def query():
        raise Exception("server selection timeout")

    with pytest.raises(Exception):
        await query()

    result = await probe.check()
    assert result.details["error_count"] == 1
    assert result.details["consecutive_errors"] == 1


@pytest.mark.asyncio
async def test_error_rate_triggers_unhealthy():
    probe = MongoProbe(max_error_rate=0.1)

    @probe.watch
    async def fail():
        raise RuntimeError()

    @probe.watch
    async def succeed():
        return "ok"

    for _ in range(9):
        with pytest.raises(RuntimeError):
            await fail()
    await succeed()

    result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY


@pytest.mark.asyncio
async def test_consecutive_errors_reset_on_success():
    probe = MongoProbe()

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
async def test_return_value_preserved():
    probe = MongoProbe()

    @probe.watch
    async def query():
        return {"_id": "abc", "status": "ok"}

    assert await query() == {"_id": "abc", "status": "ok"}


@pytest.mark.asyncio
async def test_exceptions_propagate():
    probe = MongoProbe()

    @probe.watch
    async def query():
        raise RuntimeError("write conflict")

    with pytest.raises(RuntimeError, match="write conflict"):
        await query()


def test_default_name():
    assert MongoProbe().name == "mongodb"
