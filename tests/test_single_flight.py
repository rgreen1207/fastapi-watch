"""Concurrent callers share one in-flight run per probe."""
import asyncio

import httpx
import pytest
from fastapi import FastAPI

from fastapi_watch import HealthRegistry
from fastapi_watch.models import ProbeResult, ProbeStatus
from fastapi_watch.probes.base import BaseProbe
from fastapi_watch.storage import InMemoryProbeStorage


class _Slow(BaseProbe):
    def __init__(self, name: str = "slow", delay: float = 0.1):
        self.name = name
        self.delay = delay
        self.calls = 0

    async def check(self):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return ProbeResult(name=self.name, status=ProbeStatus.HEALTHY)


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


@pytest.mark.asyncio
async def test_concurrent_requests_run_a_due_probe_once():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=60_000)
    probe = _Slow()
    registry.add(probe)
    async with _client(app) as client:
        responses = await asyncio.gather(*(client.get("/health/ready") for _ in range(20)))

    assert {r.status_code for r in responses} == {200}
    assert probe.calls == 1
    assert len((await registry._storage.get_history())["slow"]) == 1


@pytest.mark.asyncio
async def test_concurrent_run_all_share_results():
    registry = HealthRegistry(FastAPI(), poll_interval_ms=None)
    probe = _Slow()
    registry.add(probe)
    first, second = await asyncio.gather(registry.run_all(), registry.run_all())

    assert probe.calls == 1
    assert first == second


@pytest.mark.asyncio
async def test_finished_run_is_not_reused():
    registry = HealthRegistry(FastAPI(), poll_interval_ms=None)
    probe = _Slow(delay=0)
    registry.add(probe)
    await registry.run_all()
    await registry.run_all()

    assert probe.calls == 2
    assert registry._inflight == {}


@pytest.mark.asyncio
async def test_cancelled_caller_does_not_cancel_shared_run():
    registry = HealthRegistry(FastAPI(), poll_interval_ms=None)
    probe = _Slow(delay=0.1)
    registry.add(probe)
    impatient = asyncio.create_task(registry.run_all())
    patient = asyncio.create_task(registry.run_all())
    await asyncio.sleep(0.01)
    impatient.cancel()

    [result] = await patient
    assert result.status == ProbeStatus.HEALTHY
    assert probe.calls == 1


@pytest.mark.asyncio
async def test_distinct_probes_are_never_coalesced():
    registry = HealthRegistry(FastAPI(), poll_interval_ms=None)
    a, b = _Slow("a"), _Slow("b")
    registry.add(a).add(b)
    await registry.run_all()

    assert (a.calls, b.calls) == (1, 1)


class _BrokenStorage(InMemoryProbeStorage):
    async def set_latest(self, result):
        raise ConnectionError("storage down")


@pytest.mark.asyncio
async def test_failed_run_reaches_every_caller_and_is_forgotten(monkeypatch):
    registry = HealthRegistry(FastAPI(), poll_interval_ms=None)
    registry.add(_Slow(delay=0.05))

    async def _fails(probe, critical):
        await asyncio.sleep(0.05)
        raise ConnectionError("unexpected")

    monkeypatch.setattr(registry, "_check_or_cached", _fails)
    outcomes = await asyncio.gather(registry.run_all(), registry.run_all(), return_exceptions=True)

    assert all(isinstance(o, ConnectionError) for o in outcomes)
    assert registry._inflight == {}


@pytest.mark.asyncio
async def test_storage_failure_does_not_fail_the_shared_run():
    registry = HealthRegistry(FastAPI(), poll_interval_ms=None, storage=_BrokenStorage())
    registry.add(_Slow(delay=0.05))
    outcomes = await asyncio.gather(registry.run_all(), registry.run_all())

    assert [[r.status for r in o] for o in outcomes] == [[ProbeStatus.HEALTHY]] * 2
    assert registry._inflight == {}
