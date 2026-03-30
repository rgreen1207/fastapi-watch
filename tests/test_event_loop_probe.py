import pytest
from fastapi_watch.probes.event_loop import EventLoopProbe
from fastapi_watch.models import ProbeStatus


@pytest.mark.asyncio
async def test_event_loop_healthy_under_warn():
    probe = EventLoopProbe(warn_ms=1000.0, fail_ms=2000.0)
    result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.name == "event_loop"
    assert result.latency_ms >= 0
    assert result.details["lag_ms"] >= 0


@pytest.mark.asyncio
async def test_event_loop_degraded_at_warn(monkeypatch):
    import asyncio

    original_sleep = asyncio.sleep

    async def slow_sleep(delay):
        # Simulate real sleep then report high lag via monkeypatching loop.time
        await original_sleep(delay)

    probe = EventLoopProbe(warn_ms=0.001, fail_ms=1000.0)
    result = await probe.check()
    # With warn_ms=0.001 ms, any real asyncio.sleep(0) will exceed it
    assert result.status in (ProbeStatus.DEGRADED, ProbeStatus.UNHEALTHY)


@pytest.mark.asyncio
async def test_event_loop_unhealthy_at_fail(monkeypatch):
    probe = EventLoopProbe(warn_ms=0.0, fail_ms=0.0)
    result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY


@pytest.mark.asyncio
async def test_event_loop_custom_name():
    probe = EventLoopProbe(name="loop-lag")
    result = await probe.check()
    assert result.name == "loop-lag"


@pytest.mark.asyncio
async def test_event_loop_details_present():
    probe = EventLoopProbe(warn_ms=5.0, fail_ms=20.0)
    result = await probe.check()
    assert "lag_ms" in result.details
    assert "warn_ms" in result.details
    assert result.details["warn_ms"] == 5.0
    assert result.details["fail_ms"] == 20.0


@pytest.mark.asyncio
async def test_event_loop_default_poll_interval_none():
    probe = EventLoopProbe()
    assert probe.poll_interval_ms is None
