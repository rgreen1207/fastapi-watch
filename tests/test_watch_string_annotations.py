"""probe.watch() on handlers whose annotations are strings.

``from __future__ import annotations`` (below) turns every annotation in this
module into a string.  Older FastAPI releases evaluate those in the namespace
of the callable they are given, so before the wrappers carried a resolved
``__signature__`` a watched ``request: Request`` became a required query
parameter and every call to the route failed with 422 (a WebSocket closed
with 1008).  CI runs this module against the oldest supported FastAPI.
"""
from __future__ import annotations

import inspect

from fastapi import Depends, FastAPI, Request, WebSocket
from fastapi.testclient import TestClient

from fastapi_watch import FastAPIRouteProbe, FastAPIWebSocketProbe


def _user(request: Request) -> str:
    return request.headers.get("x-user", "anonymous")


def test_watched_async_route_keeps_request_query_and_dependencies():
    probe = FastAPIRouteProbe(name="items")
    app = FastAPI()

    async def items(request: Request, q: int = 1, user: str = Depends(_user)):
        return {"q": q, "path": request.url.path, "user": user}

    app.get("/items")(probe.watch(items))

    resp = TestClient(app).get("/items?q=3", headers={"x-user": "ada"})
    assert resp.status_code == 200
    assert resp.json() == {"q": 3, "path": "/items", "user": "ada"}
    assert probe._call_count == 1


def test_watched_sync_route_keeps_request():
    probe = FastAPIRouteProbe(name="sync")
    app = FastAPI()

    def sync(request: Request):
        return {"path": request.url.path}

    app.get("/sync")(probe.watch(sync))

    resp = TestClient(app).get("/sync")
    assert resp.status_code == 200
    assert resp.json() == {"path": "/sync"}


def test_watched_websocket_keeps_socket_param():
    probe = FastAPIWebSocketProbe(name="echo")
    app = FastAPI()

    async def echo(sock: WebSocket):
        await sock.accept()
        await sock.send_text(await sock.receive_text())
        await sock.close()

    app.websocket("/ws")(probe.watch(echo))

    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_text("hi")
        assert ws.receive_text() == "hi"
    assert (probe._messages_received, probe._messages_sent) == (1, 1)


def test_wrapper_signature_has_evaluated_annotations():
    probe = FastAPIRouteProbe(name="sig")

    async def handler(request: Request, q: int = 1): ...

    params = inspect.signature(probe.watch(handler)).parameters
    assert params["request"].annotation is Request
    assert params["q"].annotation is int


def test_unresolvable_annotation_is_left_alone():
    """An annotation naming a class local to a function cannot be evaluated
    from the module; watching must still work and keep the string form."""
    probe = FastAPIRouteProbe(name="local")

    class Local:
        pass

    async def handler(x: Local): ...

    wrapped = probe.watch(handler)
    assert inspect.signature(wrapped).parameters["x"].annotation == "Local"
