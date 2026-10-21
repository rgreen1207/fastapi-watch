"""User callbacks and storage errors must not break health checks or polling."""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi_watch import HealthRegistry
from fastapi_watch.models import ProbeResult, ProbeStatus
from fastapi_watch.probes.base import BaseProbe
from fastapi_watch.storage import InMemoryProbeStorage


class _Flip(BaseProbe):
    """HEALTHY on the first check, UNHEALTHY afterwards — one state change."""

    name = "flip"

    def __init__(self):
        self.calls = 0

    async def check(self):
        self.calls += 1
        status = ProbeStatus.HEALTHY if self.calls == 1 else ProbeStatus.UNHEALTHY
        return ProbeResult(name=self.name, status=status)


def _boom(*_):
    raise RuntimeError("callback bug")


async def _async_boom(*_):
    raise RuntimeError("async callback bug")


@pytest.mark.parametrize("bad_callback", [_boom, _async_boom])
def test_raising_callback_does_not_fail_ready(bad_callback, caplog):
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(_Flip())
    seen = []
    registry.on_state_change(bad_callback)
    registry.on_state_change(lambda name, old, new: seen.append((name, old, new)))
    client = TestClient(app)

    client.get("/health/ready")
    with caplog.at_level("ERROR", logger="fastapi_watch"):
        resp = client.get("/health/ready")

    assert resp.status_code == 503  # the probe's real verdict, not a 500
    assert seen == [("flip", ProbeStatus.HEALTHY, ProbeStatus.UNHEALTHY)]  # later callbacks still run
    assert any("on_state_change callback" in r.getMessage() for r in caplog.records)
    assert len(client.get("/health/alerts").json()["alerts"]) == 1


class _FailOnceStorage(InMemoryProbeStorage):
    def __init__(self):
        super().__init__()
        self.failures_left = 1

    async def set_latest(self, result):
        if self.failures_left:
            self.failures_left -= 1
            raise ConnectionError("storage down")
        await super().set_latest(result)


@pytest.mark.asyncio
async def test_poll_loop_survives_a_failing_iteration(caplog):
    registry = HealthRegistry(FastAPI(), poll_interval_ms=1000, storage=_FailOnceStorage())
    probe = _Flip()
    registry.add(probe)

    with caplog.at_level("ERROR", logger="fastapi_watch"):
        task = asyncio.create_task(registry._poll_loop())
        await asyncio.sleep(0.05)
    try:
        assert probe.calls == 1
        assert not task.done()  # still alive after the storage error
        assert any("poll iteration failed" in r.getMessage() for r in caplog.records)
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_on_connect_restarts_a_dead_poll_task():
    registry = HealthRegistry(FastAPI(), poll_interval_ms=1000)

    async def _died():
        return None

    dead = asyncio.create_task(_died())
    await dead
    registry._poll_task = dead
    await registry._on_connect()
    try:
        assert registry._poll_task is not dead
        assert not registry._poll_task.done()
    finally:
        registry._cancel_poll_task()
        await registry._on_disconnect()
