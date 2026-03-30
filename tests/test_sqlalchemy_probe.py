"""Tests for SqlAlchemyProbe passive observation via watch decorator."""
import pytest
from fastapi_watch.models import ProbeStatus
from fastapi_watch.probes.sqlalchemy import SqlAlchemyProbe


@pytest.mark.asyncio
async def test_no_calls_returns_healthy():
    probe = SqlAlchemyProbe(name="postgres")
    result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.details["message"] == "no calls observed yet"


@pytest.mark.asyncio
async def test_successful_call_recorded():
    probe = SqlAlchemyProbe(name="postgres")

    @probe.watch
    async def get_user(uid: int):
        return {"id": uid}

    await get_user(1)
    result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.details["call_count"] == 1
    assert result.details["error_count"] == 0


@pytest.mark.asyncio
async def test_exception_recorded_as_error():
    probe = SqlAlchemyProbe()

    @probe.watch
    async def query():
        raise Exception("connection pool exhausted")

    with pytest.raises(Exception):
        await query()

    result = await probe.check()
    assert result.details["error_count"] == 1
    assert result.details["consecutive_errors"] == 1


@pytest.mark.asyncio
async def test_error_rate_triggers_unhealthy():
    probe = SqlAlchemyProbe(max_error_rate=0.1)

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
    probe = SqlAlchemyProbe()

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
    probe = SqlAlchemyProbe()

    @probe.watch
    async def query():
        return [1, 2, 3]

    assert await query() == [1, 2, 3]


@pytest.mark.asyncio
async def test_exceptions_propagate():
    probe = SqlAlchemyProbe()

    @probe.watch
    async def query():
        raise RuntimeError("deadlock detected")

    with pytest.raises(RuntimeError, match="deadlock detected"):
        await query()


def test_default_name():
    assert SqlAlchemyProbe().name == "database"
