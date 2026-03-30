"""Tests for SMTPProbe passive observation via watch decorator."""
import pytest
from fastapi_watch.models import ProbeStatus
from fastapi_watch.probes.smtp import SMTPProbe


@pytest.mark.asyncio
async def test_no_sends_returns_healthy():
    probe = SMTPProbe(name="sendgrid")
    result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.details["message"] == "no sends observed yet"


@pytest.mark.asyncio
async def test_successful_send_recorded():
    probe = SMTPProbe(name="sendgrid")

    @probe.watch
    async def send():
        return None

    await send()
    result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.details["call_count"] == 1
    assert result.details["error_count"] == 0
    assert result.details["error_rate"] == 0.0


@pytest.mark.asyncio
async def test_exception_recorded_as_error():
    probe = SMTPProbe(name="sendgrid")

    @probe.watch
    async def send():
        raise ConnectionError("auth failed")

    with pytest.raises(ConnectionError):
        await send()

    result = await probe.check()
    assert result.details["call_count"] == 1
    assert result.details["error_count"] == 1
    assert result.details["consecutive_errors"] == 1


@pytest.mark.asyncio
async def test_error_rate_triggers_unhealthy():
    probe = SMTPProbe(name="sendgrid", max_error_rate=0.1)

    @probe.watch
    async def fail():
        raise RuntimeError("smtp error")

    @probe.watch
    async def succeed():
        return None

    for _ in range(9):
        with pytest.raises(RuntimeError):
            await fail()
    await succeed()

    result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY
    assert "error rate" in result.error


@pytest.mark.asyncio
async def test_consecutive_errors_reset_on_success():
    probe = SMTPProbe(name="sendgrid")

    @probe.watch
    async def fail():
        raise RuntimeError()

    @probe.watch
    async def succeed():
        return None

    for _ in range(3):
        with pytest.raises(RuntimeError):
            await fail()

    assert probe._consecutive_errors == 3
    await succeed()
    assert probe._consecutive_errors == 0


@pytest.mark.asyncio
async def test_latency_recorded():
    probe = SMTPProbe(name="sendgrid")

    @probe.watch
    async def send():
        return None

    await send()
    result = await probe.check()
    assert result.details["last_rtt_ms"] is not None
    assert result.details["avg_rtt_ms"] is not None


@pytest.mark.asyncio
async def test_sync_function_instrumented():
    probe = SMTPProbe(name="sendgrid")

    @probe.watch
    def send():
        return None

    send()
    result = await probe.check()
    assert result.details["call_count"] == 1


@pytest.mark.asyncio
async def test_exceptions_still_propagate():
    probe = SMTPProbe(name="sendgrid")

    @probe.watch
    async def send():
        raise ValueError("bad credentials")

    with pytest.raises(ValueError, match="bad credentials"):
        await send()


@pytest.mark.asyncio
async def test_return_value_preserved():
    probe = SMTPProbe(name="sendgrid")

    @probe.watch
    async def send():
        return {"message_id": "abc123"}

    result = await send()
    assert result == {"message_id": "abc123"}


@pytest.mark.asyncio
async def test_avg_rtt_triggers_unhealthy():
    probe = SMTPProbe(name="sendgrid", max_avg_rtt_ms=1.0)

    import asyncio

    @probe.watch
    async def slow_send():
        await asyncio.sleep(0.05)

    await slow_send()
    result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY
    assert "avg RTT" in result.error
