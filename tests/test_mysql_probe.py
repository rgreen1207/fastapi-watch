"""Tests for MySQLProbe passive observation via watch decorator."""
import pytest
from fastapi_watch.models import ProbeStatus
from fastapi_watch.probes.mysql import MySQLProbe


@pytest.mark.asyncio
async def test_no_calls_has_no_data():
    probe = MySQLProbe(name="primary-db")
    result = await probe.check()
    assert result.status == ProbeStatus.NO_DATA
    assert result.details["message"] == "no calls observed yet"


@pytest.mark.asyncio
async def test_successful_call_recorded():
    probe = MySQLProbe(name="primary-db")

    @probe.watch
    async def get_product(pid: int):
        return {"id": pid, "name": "widget"}

    await get_product(1)
    result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.details["call_count"] == 1
    assert result.details["error_count"] == 0


@pytest.mark.asyncio
async def test_exception_recorded_as_error():
    probe = MySQLProbe()

    @probe.watch
    async def query():
        raise Exception("host unreachable")

    with pytest.raises(Exception):
        await query()

    result = await probe.check()
    assert result.details["error_count"] == 1
    assert result.details["consecutive_errors"] == 1


@pytest.mark.asyncio
async def test_error_rate_triggers_unhealthy():
    probe = MySQLProbe(max_error_rate=0.1)

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
    probe = MySQLProbe()

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
    probe = MySQLProbe()

    @probe.watch
    async def query():
        return [{"id": 1}, {"id": 2}]

    assert await query() == [{"id": 1}, {"id": 2}]


@pytest.mark.asyncio
async def test_exceptions_propagate():
    probe = MySQLProbe()

    @probe.watch
    async def query():
        raise RuntimeError("access denied")

    with pytest.raises(RuntimeError, match="access denied"):
        await query()


def test_default_name():
    assert MySQLProbe().name == "mysql"
