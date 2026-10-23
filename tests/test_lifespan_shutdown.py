"""The registry shuts down with the application, whether or not it has a ``lifespan=``.

Starlette runs ``on_shutdown`` handlers only for apps without a lifespan, and
the documented setup always passes one: queued alerts, pending storage writes
and probe ``aclose()`` were all skipped at shutdown.
"""
import asyncio
import contextlib
import time

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient

from fastapi_watch import HealthRegistry, InMemoryProbeStorage
from fastapi_watch.models import ProbeResult, ProbeStatus
from fastapi_watch.probes.base import BaseProbe


class _Closable(BaseProbe):
    def __init__(self, name: str = "closable"):
        self.name = name
        self.closed = 0

    async def check(self):
        return ProbeResult(name=self.name, status=ProbeStatus.HEALTHY)

    async def aclose(self):
        self.closed += 1


def _lifespan_app(events: list) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app):
        events.append("startup")
        yield {"answer": 42}
        events.append("teardown")

    return FastAPI(lifespan=lifespan)


def test_lifespan_app_shuts_the_registry_down():
    events: list = []
    app = _lifespan_app(events)
    registry = HealthRegistry(app, poll_interval_ms=None)
    probe = _Closable()
    registry.add(probe)
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200
        assert probe.closed == 0
    assert registry._shut_down is True
    assert probe.closed == 1
    assert events == ["startup", "teardown"]


def test_registry_shuts_down_before_the_app_teardown():
    """Alerters and storage may use clients the app's own teardown closes."""
    events: list = []
    app = _lifespan_app(events)
    registry = HealthRegistry(app, poll_interval_ms=None)

    class _Recording(_Closable):
        async def aclose(self):
            events.append("registry")

    registry.add(_Recording())
    with TestClient(app):
        pass
    assert events == ["startup", "registry", "teardown"]


def test_lifespan_state_still_reaches_requests():
    events: list = []
    app = _lifespan_app(events)
    HealthRegistry(app, poll_interval_ms=None)

    @app.get("/answer")
    async def answer(request: Request):
        return {"answer": request.state.answer}

    with TestClient(app) as client:
        assert client.get("/answer").json() == {"answer": 42}


def test_app_without_lifespan_shuts_down_exactly_once():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    probe = _Closable()
    registry.add(probe)
    with TestClient(app):
        pass
    # Both the lifespan wrapper and the on_shutdown entry reach _shutdown.
    assert probe.closed == 1


def test_router_lifespan_merged_after_the_registry_still_shuts_it_down():
    """include_router() wraps the app's lifespan context, ours included."""
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    probe = _Closable()
    registry.add(probe)
    seen: list = []

    @contextlib.asynccontextmanager
    async def router_lifespan(app):
        seen.append("router")
        yield

    router = APIRouter(lifespan=router_lifespan)
    try:
        app.include_router(router)
    except TypeError:
        pytest.skip("this FastAPI has no router lifespans")
    with TestClient(app):
        pass
    assert probe.closed == 1


def test_two_registries_on_one_lifespan_app_both_shut_down():
    app = _lifespan_app([])
    first = HealthRegistry(app, prefix="/a", poll_interval_ms=None)
    second = HealthRegistry(app, prefix="/b", poll_interval_ms=None)
    probes = [_Closable("one"), _Closable("two")]
    first.add(probes[0])
    second.add(probes[1])
    with TestClient(app):
        pass
    assert [p.closed for p in probes] == [1, 1]


@pytest.mark.parametrize("with_lifespan", [True, False])
def test_each_lifespan_starts_the_registry_afresh(with_lifespan):
    """After one shutdown, streams of a second run returned nothing and alerts stopped."""
    app = _lifespan_app([]) if with_lifespan else FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    probe = _Closable()
    registry.add(probe)
    for cycle in (1, 2):
        with TestClient(app) as client:
            resp = client.get("/health/status/stream")
            assert resp.text.startswith("retry: "), f"cycle {cycle}: {resp.text!r}"
            assert registry._shutting_down is False
        assert probe.closed == cycle


@pytest.mark.asyncio
async def test_alerts_flow_again_after_a_restart():
    from fastapi_watch.alerts import BaseAlerter

    received = []

    class _Recording(BaseAlerter):
        async def notify(self, alert):
            received.append(alert)

    app = _lifespan_app([])
    registry = HealthRegistry(app, poll_interval_ms=None, alerters=[_Recording()])
    async with app.router.lifespan_context(app):
        pass
    async with app.router.lifespan_context(app):
        await registry._fire_state_changes([ProbeResult(name="db", status=ProbeStatus.UNHEALTHY)])
    assert [a.probe for a in received] == ["db"]


@pytest.mark.asyncio
async def test_shutdown_is_idempotent():
    registry = HealthRegistry(FastAPI(), poll_interval_ms=None)
    probe = _Closable()
    registry.add(probe)
    await registry._shutdown()
    await registry._shutdown()
    assert probe.closed == 1


class _Healthy(BaseProbe):
    name = "healthy"

    async def check(self) -> ProbeResult:
        return ProbeResult(name=self.name, status=ProbeStatus.HEALTHY)


class _SlowStorage(InMemoryProbeStorage):
    """A non-default backend, so writes run in the background."""

    def __init__(self) -> None:
        super().__init__()
        self.written: list[str] = []

    async def append_history(self, result: ProbeResult) -> None:
        await asyncio.sleep(0.2)
        self.written.append(result.name)
        await super().append_history(result)


def test_pending_writes_flushed_with_a_lifespan_app():
    app = _lifespan_app([])
    storage = _SlowStorage()
    HealthRegistry(app, storage=storage).add(_Healthy())
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200
        assert storage.written == []  # still in flight when the app stops
    assert storage.written == ["healthy"]


@pytest.mark.parametrize("with_lifespan", [True, False])
def test_queued_alerts_sent_at_shutdown(with_lifespan):
    sent = []

    class _Alerter:
        async def notify(self, alert) -> None:
            await asyncio.sleep(0.2)
            sent.append(alert.probe)

    class _Failing(BaseProbe):
        name = "failing"

        async def check(self) -> ProbeResult:
            return ProbeResult(name=self.name, status=ProbeStatus.UNHEALTHY)

    app = _lifespan_app([]) if with_lifespan else FastAPI()
    HealthRegistry(app, alerters=[_Alerter()]).add(_Failing())
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 503
        assert sent == []
    assert sent == ["failing"]


def test_alert_worker_survives_a_second_event_loop():
    """Each ``with TestClient(app)`` runs on a new loop.  The alert queue stayed
    bound to the first, so the next worker died on its first wait and a
    coalesced recovery alert was never sent."""
    from fastapi_watch.alerts import BaseAlerter

    sent: list = []
    state = {"status": ProbeStatus.HEALTHY}

    class _Recording(BaseAlerter):
        async def notify(self, alert):
            sent.append(alert.new_status)

    class _Switch(BaseProbe):
        name = "switch"

        async def check(self) -> ProbeResult:
            return ProbeResult(name=self.name, status=state["status"])

    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None, alerters=[_Recording()], alert_min_interval_s=0.2)
    registry.add(_Switch())
    for cycle in (1, 2):
        sent.clear()
        registry._alert_last_sent_at.clear()
        with TestClient(app) as client:
            state["status"] = ProbeStatus.UNHEALTHY
            client.get("/health/ready")
            time.sleep(0.05)
            state["status"] = ProbeStatus.HEALTHY
            client.get("/health/ready")  # coalesced: sent once the interval ends
            deadline = time.monotonic() + 2
            while len(sent) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            worker = registry._alert_worker
            assert worker is not None and not worker.done(), f"cycle {cycle}: worker died"
        assert sent == [ProbeStatus.UNHEALTHY, ProbeStatus.HEALTHY], f"cycle {cycle}"
