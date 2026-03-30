import pytest
from fastapi_watch.probes.noop import NoOpProbe
from fastapi_watch.models import ProbeStatus


@pytest.mark.asyncio
async def test_noop_probe_always_healthy():
    probe = NoOpProbe()
    result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.name == "noop"
    assert result.error is None


@pytest.mark.asyncio
async def test_noop_probe_custom_name():
    probe = NoOpProbe(name="test-svc")
    result = await probe.check()
    assert result.name == "test-svc"
    assert result.status == ProbeStatus.HEALTHY


@pytest.mark.asyncio
async def test_noop_probe_zero_latency():
    probe = NoOpProbe()
    result = await probe.check()
    assert result.latency_ms == 0.0


@pytest.mark.asyncio
async def test_noop_probe_is_healthy():
    probe = NoOpProbe()
    result = await probe.check()
    assert result.is_healthy is True
