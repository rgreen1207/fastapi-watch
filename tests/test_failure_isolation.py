"""User callbacks and storage errors must not break health checks or polling."""
import asyncio
import signal

import httpx
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


# ---------------------------------------------------------------------------
# Early shutdown hook: signal handler defers task cancellation to the loop
# ---------------------------------------------------------------------------

def _capture_signal_handlers(monkeypatch):
    installed, chained_to = {}, []
    monkeypatch.setattr(signal, "getsignal", lambda sig: (lambda signum, frame: chained_to.append(signum)))
    monkeypatch.setattr(signal, "signal", lambda sig, handler: installed.__setitem__(sig, handler))
    return installed, chained_to


@pytest.mark.asyncio
async def test_signal_handler_defers_cancellation_to_the_loop(monkeypatch):
    class _RecordingTask:
        cancel_calls = 0

        def cancel(self):
            self.cancel_calls += 1

    installed, chained_to = _capture_signal_handlers(monkeypatch)
    registry = HealthRegistry(FastAPI())
    registry._install_early_shutdown_hook()
    stream_task = _RecordingTask()
    registry._sse_tasks.add(stream_task)

    installed[signal.SIGTERM](signal.SIGTERM, None)
    assert registry._shutting_down is True
    assert chained_to == [signal.SIGTERM]  # the previous handler still runs
    assert stream_task.cancel_calls == 0  # nothing cancelled from inside the handler

    await asyncio.sleep(0)  # let the loop run the scheduled cancellation
    assert stream_task.cancel_calls == 1


def test_signal_handler_without_a_running_loop_only_sets_the_flag(monkeypatch):
    installed, chained_to = _capture_signal_handlers(monkeypatch)
    registry = HealthRegistry(FastAPI())
    registry._install_early_shutdown_hook()  # called from sync code: no running loop

    installed[signal.SIGINT](signal.SIGINT, None)
    assert registry._shutting_down is True
    assert chained_to == [signal.SIGINT]


# ---------------------------------------------------------------------------
# Startup probes run through the same guarded path
# ---------------------------------------------------------------------------

class _RaisingProbe(BaseProbe):
    name = "boot"

    async def check(self):
        raise RuntimeError("init failed")


class _HangingStartupProbe(BaseProbe):
    name = "boot"

    async def check(self):
        await asyncio.sleep(3600)


def test_raising_startup_probe_answers_503_not_500():
    app = FastAPI()
    HealthRegistry(app, startup_probes=[_RaisingProbe()]).set_started()
    resp = TestClient(app, raise_server_exceptions=False).get("/health/startup")
    assert resp.status_code == 503
    assert resp.json()["failing"] == ["boot"]


@pytest.mark.asyncio
async def test_hung_startup_probe_is_bounded_by_probe_timeout():
    app = FastAPI()
    HealthRegistry(app, startup_probes=[_HangingStartupProbe()], probe_timeout=0.05).set_started()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await asyncio.wait_for(client.get("/health/startup"), 2)
    assert resp.status_code == 503
    assert resp.json()["probes"][0]["error"] == "probe timed out after 0.05 s"


# ---------------------------------------------------------------------------
# Custom-interval probes poll in the background without a stream client
# ---------------------------------------------------------------------------

class _Counting(BaseProbe):
    def __init__(self, name="counted", poll_interval_ms=None):
        self.name = name
        self.poll_interval_ms = poll_interval_ms
        self.calls = 0

    async def check(self):
        self.calls += 1
        return ProbeResult(name=self.name, status=ProbeStatus.HEALTHY)


@pytest.mark.asyncio
async def test_set_started_starts_background_polling_for_custom_intervals():
    registry = HealthRegistry(FastAPI(), poll_interval_ms=None)
    probe = _Counting(poll_interval_ms=1000)
    registry.add(probe)
    registry.set_started()
    try:
        assert registry._poll_task is not None and not registry._poll_task.done()
        await asyncio.sleep(1.2)  # no stream client, no requests
        assert probe.calls >= 2
    finally:
        await registry._shutdown()


@pytest.mark.asyncio
async def test_first_request_starts_polling_when_set_started_had_no_loop():
    registry = HealthRegistry(FastAPI(), poll_interval_ms=None)
    registry.add(_Counting(poll_interval_ms=1000))
    await asyncio.to_thread(registry.set_started)  # sync context: no running loop there
    assert registry._poll_task is None
    try:
        await registry.get_report()
        assert registry._poll_task is not None and not registry._poll_task.done()
    finally:
        await registry._shutdown()


@pytest.mark.asyncio
async def test_no_background_polling_without_custom_intervals():
    registry = HealthRegistry(FastAPI(), poll_interval_ms=60_000)
    registry.add(_Counting())
    registry.set_started()
    await registry.get_report()
    assert registry._poll_task is None


@pytest.mark.asyncio
async def test_background_polling_not_restarted_after_shutdown():
    registry = HealthRegistry(FastAPI(), poll_interval_ms=None)
    registry.add(_Counting(poll_interval_ms=1000))
    await registry._shutdown()
    registry.set_started()
    assert registry._poll_task is None


# ---------------------------------------------------------------------------
# A probe failing on its first check alerts
# ---------------------------------------------------------------------------

class _Fixed(BaseProbe):
    def __init__(self, status: ProbeStatus):
        self.name = "fixed"
        self.status = status

    async def check(self):
        return ProbeResult(name=self.name, status=self.status)


class _RecordingAlerter:
    def __init__(self):
        self.alerts = []

    async def notify(self, alert):
        self.alerts.append(alert)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [ProbeStatus.UNHEALTHY, ProbeStatus.DEGRADED])
async def test_first_non_healthy_result_alerts(status):
    alerter = _RecordingAlerter()
    registry = HealthRegistry(FastAPI(), poll_interval_ms=None, alerters=[alerter])
    registry.add(_Fixed(status))
    events = []
    registry.on_state_change(lambda *event: events.append(event))

    await registry.run_all()
    await asyncio.sleep(0.05)  # let the alert worker drain
    assert events == [("fixed", ProbeStatus.HEALTHY, status)]
    assert [(a.old_status, a.new_status) for a in alerter.alerts] == [(ProbeStatus.HEALTHY, status)]
    assert len(await registry._storage.get_alerts()) == 1
    await registry._shutdown()


@pytest.mark.asyncio
async def test_first_healthy_result_is_silent():
    registry = HealthRegistry(FastAPI(), poll_interval_ms=None)
    registry.add(_Fixed(ProbeStatus.HEALTHY))
    events = []
    registry.on_state_change(lambda *event: events.append(event))
    await registry.run_all()
    assert events == []
    assert await registry._storage.get_alerts() == []


@pytest.mark.asyncio
async def test_first_failure_during_maintenance_is_recorded_but_not_sent():
    alerter = _RecordingAlerter()
    registry = HealthRegistry(FastAPI(), poll_interval_ms=None, alerters=[alerter])
    registry.add(_Fixed(ProbeStatus.UNHEALTHY))
    registry.set_maintenance()
    await registry.run_all()
    await asyncio.sleep(0.05)
    assert alerter.alerts == []
    assert len(await registry._storage.get_alerts()) == 1
    await registry._shutdown()
