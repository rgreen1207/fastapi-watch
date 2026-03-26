import pytest
from fastapi_watch.probes.memory import MemoryProbe
from fastapi_watch.models import ProbeStatus


@pytest.mark.asyncio
async def test_memory_probe_always_healthy():
    probe = MemoryProbe()
    result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.name == "memory"
    assert result.error is None


@pytest.mark.asyncio
async def test_memory_probe_custom_name():
    probe = MemoryProbe(name="test-svc")
    result = await probe.check()
    assert result.name == "test-svc"
    assert result.status == ProbeStatus.HEALTHY


@pytest.mark.asyncio
async def test_memory_probe_zero_latency():
    probe = MemoryProbe()
    result = await probe.check()
    assert result.latency_ms == 0.0


@pytest.mark.asyncio
async def test_memory_probe_is_healthy():
    probe = MemoryProbe()
    result = await probe.check()
    assert result.is_healthy is True
