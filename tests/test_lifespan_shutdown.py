"""Shutdown work runs with every form of app lifespan.

The registry used to hook ``router.on_shutdown``, which Starlette skips when
the app has a ``lifespan=``: probes were never closed, pending storage writes
never flushed and queued alerts never sent.
"""
import asyncio
import contextlib

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from fastapi_watch import HealthRegistry, InMemoryProbeStorage
from fastapi_watch.models import ProbeResult, ProbeStatus
from fastapi_watch.probes.base import BaseProbe


class _ClosingProbe(BaseProbe):
    def __init__(self, events: list[str]) -> None:
        self.name = "closing"
        self._events = events

    async def check(self) -> ProbeResult:
        return ProbeResult(name=self.name, status=ProbeStatus.HEALTHY)

    async def aclose(self) -> None:
        self._events.append("aclose")


def _lifespan_app(events: list[str]) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app):
        events.append("startup")
        yield {"answer": 42}
        events.append("app teardown")

    return FastAPI(lifespan=lifespan)


def test_probes_closed_with_a_lifespan_app():
    events: list[str] = []
    app = _lifespan_app(events)
    HealthRegistry(app).add(_ClosingProbe(events))
    with TestClient(app):
        pass
    assert "aclose" in events


def test_shutdown_runs_before_the_app_teardown():
    """The app's teardown may close the client the final writes go through."""
    events: list[str] = []
    app = _lifespan_app(events)
    HealthRegistry(app).add(_ClosingProbe(events))
    with TestClient(app):
        pass
    assert events == ["startup", "aclose", "app teardown"]


def test_probes_closed_without_a_lifespan():
    events: list[str] = []
    app = FastAPI()
    HealthRegistry(app).add(_ClosingProbe(events))
    with TestClient(app):
        pass
    assert events == ["aclose"]


def test_lifespan_state_still_reaches_requests():
    events: list[str] = []
    app = _lifespan_app(events)
    HealthRegistry(app)

    @app.get("/answer")
    async def answer(request: Request):
        return {"answer": request.state.answer}

    with TestClient(app) as client:
        assert client.get("/answer").json() == {"answer": 42}


def test_shutdown_runs_once():
    events: list[str] = []
    app = FastAPI()
    HealthRegistry(app).add(_ClosingProbe(events))
    with TestClient(app):
        pass
    assert events.count("aclose") == 1


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
    events: list[str] = []
    app = _lifespan_app(events)
    storage = _SlowStorage()
    HealthRegistry(app, storage=storage).add(_ClosingProbe(events))
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200
        assert storage.written == []  # still in flight when the app stops
    assert storage.written == ["closing"]


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
