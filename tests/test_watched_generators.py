"""Watching generators: ``yield`` dependencies and streaming endpoints.

``watch()`` used to wrap a generator function in a plain function.  FastAPI
before it followed ``__wrapped__`` then injected the generator object itself
into a route instead of the value it yields (a session, say), breaking the
application; later releases ran it, but the probe timed only the creation of
the generator (~0 ms) and never saw an error raised while it ran.
"""
import asyncio
import inspect
import threading

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from fastapi_watch import FastAPIRouteProbe, HealthRegistry, PassiveProbe
from fastapi_watch.models import ProbeStatus


class Session:
    def __init__(self) -> None:
        self.closed = False
        self.rolled_back = False


def _details(probe: PassiveProbe) -> dict:
    return asyncio.run(probe.check()).details


# ---------------------------------------------------------------------------
# yield dependencies
# ---------------------------------------------------------------------------

def _session_app(probe: PassiveProbe, sessions: list, *, sync: bool) -> FastAPI:
    if sync:
        @probe.watch
        def get_session():
            session = Session()
            sessions.append(session)
            try:
                yield session
            except HTTPException:
                session.rolled_back = True
                raise
            finally:
                session.closed = True
    else:
        @probe.watch
        async def get_session():
            session = Session()
            sessions.append(session)
            try:
                yield session
            except HTTPException:
                session.rolled_back = True
                raise
            finally:
                session.closed = True

    app = FastAPI()

    @app.get("/ok")
    async def ok(session=Depends(get_session)):
        return {"type": type(session).__name__}

    @app.get("/missing")
    async def missing(session=Depends(get_session)):
        raise HTTPException(status_code=404)

    return app


@pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
def test_watched_yield_dependency_injects_the_yielded_value(sync):
    probe = PassiveProbe("db")
    sessions: list = []
    client = TestClient(_session_app(probe, sessions, sync=sync))

    assert client.get("/ok").json() == {"type": "Session"}
    assert sessions[0].closed

    details = _details(probe)
    assert details["call_count"] == 1
    assert details["error_count"] == 0


@pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
def test_watched_function_stays_a_generator_function(sync):
    probe = PassiveProbe("p")

    def gen():
        yield 1

    async def agen():
        yield 1

    wrapped = probe.watch(gen if sync else agen)
    assert (inspect.isgeneratorfunction if sync else inspect.isasyncgenfunction)(wrapped)


@pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
def test_endpoint_error_thrown_into_a_dependency_reaches_it_and_is_not_its_error(sync):
    """FastAPI throws the endpoint's exception into the dependency (so it can
    roll back); the wrapper forwards it, and an exception that passes back out
    unchanged is the endpoint's failure, not the dependency's."""
    probe = PassiveProbe("db")
    sessions: list = []
    client = TestClient(_session_app(probe, sessions, sync=sync))

    assert client.get("/missing").status_code == 404
    assert sessions[0].closed
    assert sessions[0].rolled_back  # the HTTPException reached the dependency's except
    details = _details(probe)
    assert details["call_count"] == 1
    assert details["error_count"] == 0


def test_error_raised_by_the_generator_is_counted():
    probe = PassiveProbe("db")

    @probe.watch
    def failing_teardown():
        yield "value"
        raise ConnectionError("commit failed")

    gen = failing_teardown()
    assert next(gen) == "value"
    with pytest.raises(ConnectionError):
        next(gen)

    details = _details(probe)
    assert details["error_count"] == 1
    assert details["last_error"] == "ConnectionError"


@pytest.mark.asyncio
async def test_async_error_raised_by_the_generator_is_counted():
    probe = PassiveProbe("db")

    @probe.watch
    async def failing_teardown():
        yield "value"
        raise ConnectionError("commit failed")

    gen = failing_teardown()
    assert await gen.__anext__() == "value"
    with pytest.raises(ConnectionError):
        await gen.__anext__()

    details = (await probe.check()).details
    assert details["error_count"] == 1


@pytest.mark.asyncio
async def test_rtt_is_time_spent_running_not_time_suspended():
    """A dependency's RTT is its own setup and teardown, not the endpoint's
    work done while it is suspended at ``yield``."""
    probe = PassiveProbe("db")

    @probe.watch
    async def dependency():
        await asyncio.sleep(0.05)
        yield "value"
        await asyncio.sleep(0.05)

    gen = dependency()
    await gen.__anext__()
    await asyncio.sleep(0.3)  # the endpoint
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()

    rtt = (await probe.check()).details["last_rtt_ms"]
    assert 90 <= rtt < 250


def test_send_close_and_return_value_are_forwarded():
    probe = PassiveProbe("p")
    received = []

    @probe.watch
    def echo():
        while True:
            value = yield
            if value is None:
                return "done"
            received.append(value)

    gen = echo()
    next(gen)
    gen.send("a")
    gen.send("b")
    with pytest.raises(StopIteration) as stop:
        gen.send(None)
    assert stop.value.value == "done"
    assert received == ["a", "b"]

    closed = []

    @probe.watch
    def closable():
        try:
            yield 1
            yield 2
        finally:
            closed.append(True)

    gen = closable()
    next(gen)
    gen.close()
    assert closed == [True]
    assert _details(probe)["call_count"] == 2


@pytest.mark.asyncio
async def test_async_aclose_is_forwarded():
    probe = PassiveProbe("p")
    closed = []

    @probe.watch
    async def closable():
        try:
            yield 1
            yield 2
        finally:
            closed.append(True)

    gen = closable()
    await gen.__anext__()
    await gen.aclose()
    assert closed == [True]
    assert (await probe.check()).details["call_count"] == 1


# ---------------------------------------------------------------------------
# Streaming (generator) endpoints, FastAPI releases that support them
# ---------------------------------------------------------------------------

def test_generator_endpoint_error_mid_stream_is_counted():
    app = FastAPI()

    @app.get("/items")
    async def items():
        yield {"n": 1}
        raise RuntimeError("feed broke")

    HealthRegistry(app).discover_routes()
    response = TestClient(app, raise_server_exceptions=False).get("/items")
    if '"n"' not in response.text:
        pytest.skip("this FastAPI release does not stream generator endpoints")

    route = next(r for r in app.routes if getattr(r, "path", None) == "/items")
    probe = route.endpoint._fastapi_watch_probe
    details = _details(probe)
    assert details["request_count"] == 1
    assert details["error_count"] == 1


# ---------------------------------------------------------------------------
# Route probes: client hang-ups, threadpool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_probe_counts_client_disconnect_as_499_not_an_error():
    probe = FastAPIRouteProbe("upload")

    @probe.watch
    async def upload(request: Request):
        raise ClientDisconnect()

    with pytest.raises(ClientDisconnect):
        await upload(None)

    details = (await probe.check()).details
    assert details["last_status_code"] == 499
    assert details["error_count"] == 0
    assert (await probe.check()).status == ProbeStatus.HEALTHY


def test_watched_sync_handlers_still_run_in_the_threadpool():
    """Wrapping must not move a ``def`` handler onto the event loop, where its
    blocking work would stall every other request."""
    app = FastAPI()
    threads: dict = {}

    @app.get("/loop")
    async def loop():
        threads["loop"] = threading.current_thread()
        return {}

    @app.get("/manual")
    @FastAPIRouteProbe("manual").watch
    def manual():
        threads["manual"] = threading.current_thread()
        return {}

    @app.get("/discovered")
    def discovered():
        threads["discovered"] = threading.current_thread()
        return {}

    HealthRegistry(app).discover_routes()
    with TestClient(app) as client:
        for path in ("/loop", "/manual", "/discovered"):
            assert client.get(path).status_code == 200

    assert threads["manual"] is not threads["loop"]
    assert threads["discovered"] is not threads["loop"]
