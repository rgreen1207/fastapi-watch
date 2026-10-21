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


# ---------------------------------------------------------------------------
# Background sampling: stalls between checks are seen
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stall_between_checks_is_detected():
    import asyncio
    import time

    probe = EventLoopProbe(warn_ms=5.0, fail_ms=20.0, sample_interval_ms=10)
    await probe.check()  # starts the sampler
    await asyncio.sleep(0.03)
    time.sleep(0.08)  # block the loop between checks
    await asyncio.sleep(0.05)

    result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY
    assert result.details["lag_ms"] >= 40
    assert result.details["current_lag_ms"] < 20  # the loop is free again by now


@pytest.mark.asyncio
async def test_lag_window_restarts_after_each_check():
    import asyncio
    import time

    probe = EventLoopProbe(warn_ms=5.0, fail_ms=20.0, sample_interval_ms=10)
    await probe.check()
    time.sleep(0.05)
    await asyncio.sleep(0.03)
    assert (await probe.check()).status == ProbeStatus.UNHEALTHY

    await asyncio.sleep(0.05)  # quiet period
    assert (await probe.check()).details["lag_ms"] < 20


@pytest.mark.asyncio
async def test_one_sampler_is_reused_across_checks():
    probe = EventLoopProbe()
    await probe.check()
    sampler = probe._sampler
    await probe.check()
    assert probe._sampler is sampler and not sampler.done()
    sampler.cancel()
