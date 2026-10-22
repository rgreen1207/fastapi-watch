"""Monitoring a route never removes the application's own decorators.

Up to 1.7.2, discover_routes() and watch_router() wrapped
``getattr(route.endpoint, "__wrapped__", route.endpoint)``, which follows any
``functools.wraps`` decorator: an endpoint behind an auth guard was replaced by
its bare handler, so a route answering 403 served 200 and its data as soon as
monitoring was switched on.

Every case checks both halves: the guard still decides the response, and the
route is still monitored (its probe counts each request exactly once, so a
double wrap shows up too).  Runs on both FastAPI layouts: flat route copies
(< 0.137) and _IncludedRouter wrappers (0.137+).
"""
import asyncio
import functools

import pytest
from fastapi import APIRouter, FastAPI, HTTPException, Request, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fastapi_watch import FastAPIRouteProbe, HealthRegistry

SECRET = "s3cr3t-payload"


# ---------------------------------------------------------------------------
# Application decorators, as users write them
# ---------------------------------------------------------------------------

def guard_async(fn):
    """Async auth guard that always refuses."""
    @functools.wraps(fn)
    async def guarded(*args, **kwargs):
        raise HTTPException(status_code=403, detail="forbidden")

    return guarded


def guard_sync(fn):
    """Sync auth guard that always refuses (FastAPI runs it in its threadpool)."""
    @functools.wraps(fn)
    def guarded(*args, **kwargs):
        raise HTTPException(status_code=403, detail="forbidden")

    return guarded


def guard_header(fn):
    """Guard that reads the request and passes through when the header is right."""
    @functools.wraps(fn)
    async def guarded(*args, request: Request, **kwargs):
        if request.headers.get("x-admin") != "1":
            raise HTTPException(status_code=403, detail="forbidden")
        return await fn(*args, request=request, **kwargs)

    return guarded


def guard_ws(fn):
    """WebSocket guard that closes the socket with 1008 (policy violation)."""
    @functools.wraps(fn)
    async def guarded(*args, **kwargs):
        websocket = kwargs.get("websocket") or next(a for a in args if isinstance(a, WebSocket))
        await websocket.close(code=1008)

    return guarded


GUARDS = {"async": guard_async, "sync": guard_sync, "header": guard_header}


def _build(kind: str, manual: FastAPIRouteProbe | None = None):
    """An app with one guarded route on the app and one on a prefixed router."""
    guard = GUARDS[kind]
    watch = manual.watch if manual is not None else (lambda fn: fn)
    app = FastAPI()
    router = APIRouter()

    if kind == "sync":
        @app.get("/app/secret")
        @watch
        @guard
        def app_secret():
            return {"secret": SECRET}

        @router.get("/secret")
        @guard
        def router_secret():
            return {"secret": SECRET}
    else:
        @app.get("/app/secret")
        @watch
        @guard
        async def app_secret(request: Request):
            return {"secret": SECRET}

        @router.get("/secret")
        @guard
        async def router_secret(request: Request):
            return {"secret": SECRET}

    app.include_router(router, prefix="/rt")
    return app, router


# Each mode: (monitor function, {path: probe name it monitors}).
MODES = {
    "discover": (lambda reg, router: reg.discover_routes(), {"/app/secret": "app_secret", "/rt/secret": "router_secret"}),
    "discover_refresh": (
        lambda reg, router: reg.discover_routes().discover_routes(refresh=True),
        {"/app/secret": "app_secret", "/rt/secret": "router_secret"},
    ),
    "discover_twice": (
        lambda reg, router: reg.discover_routes().discover_routes(),
        {"/app/secret": "app_secret", "/rt/secret": "router_secret"},
    ),
    "watch_router": (lambda reg, router: reg.watch_router(router), {"/rt/secret": "router_secret"}),
    "discover_then_watch_router": (
        lambda reg, router: reg.discover_routes().watch_router(router),
        {"/app/secret": "app_secret", "/rt/secret": "router_secret"},
    ),
    "watch_router_then_refresh": (
        lambda reg, router: reg.watch_router(router).discover_routes(refresh=True),
        {"/app/secret": "app_secret", "/rt/secret": "router_secret"},
    ),
}


def _request_count(probe) -> int:
    return asyncio.run(probe.check()).details["request_count"]


def _probes_named(registry, name):
    return [p for p, _ in registry._probes if p.name == name]


def _exercise(client, path, kind) -> int:
    """Request *path* as an outsider (and, for the header guard, as an admin); return the request count."""
    resp = client.get(path)
    assert resp.status_code == 403, f"{path} lost its guard"
    assert SECRET not in resp.text
    if kind != "header":
        return 1
    allowed = client.get(path, headers={"x-admin": "1"})
    assert allowed.status_code == 200 and SECRET in allowed.text  # the guard passes through
    return 2


@pytest.mark.parametrize("mode", list(MODES))
@pytest.mark.parametrize("kind", list(GUARDS))
def test_guarded_routes_stay_guarded_and_monitored(kind, mode):
    app, router = _build(kind)
    client = TestClient(app)
    for path in ("/app/secret", "/rt/secret"):
        _exercise(client, path, kind)  # baseline, before monitoring

    monitor, monitored = MODES[mode]
    registry = HealthRegistry(app, poll_interval_ms=None)
    monitor(registry, router)

    for path in ("/app/secret", "/rt/secret"):
        made = _exercise(client, path, kind)
        if path in monitored:
            probes = _probes_named(registry, monitored[path])
            # One probe per route, counting each request once: no double wrap.
            assert len(probes) == 1, f"{mode}: {len(probes)} probes for {path}"
            assert _request_count(probes[0]) == made


@pytest.mark.parametrize("kind", list(GUARDS))
def test_manual_watch_over_a_guard_is_kept_by_discovery(kind):
    manual = FastAPIRouteProbe(name="manual_secret")
    app, _ = _build(kind, manual=manual)
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(manual)
    registry.discover_routes()
    registry.discover_routes(refresh=True)

    made = _exercise(TestClient(app), "/app/secret", kind)
    assert _request_count(manual) == made
    assert _probes_named(registry, "app_secret") == []  # manual monitoring wins


def test_route_already_wrapped_by_fastapi_watch_is_rewrapped_once():
    """Re-discovery replaces our own wrapper, keeps the guard, and counts each request once."""
    app, router = _build("async")
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.discover_routes()
    first = _probes_named(registry, "router_secret")[0]
    for _ in range(3):
        registry.discover_routes(refresh=True)
    client = TestClient(app)
    for _ in range(4):
        _exercise(client, "/rt/secret", "async")

    (probe,) = _probes_named(registry, "router_secret")
    assert probe is not first
    assert _request_count(probe) == 4
    assert first._call_count == 0  # the replaced wrapper is no longer in the call chain


@pytest.mark.parametrize("mode", ["discover", "discover_refresh", "discover_twice"])
def test_guarded_websocket_stays_guarded_and_monitored(mode):
    app = FastAPI()

    @app.websocket("/ws")
    @guard_ws
    async def ws_secret(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text(SECRET)
        await websocket.close()

    registry = HealthRegistry(app, poll_interval_ms=None)
    MODES[mode][0](registry, None)

    with pytest.raises(WebSocketDisconnect) as closed:
        with TestClient(app).websocket_connect("/ws") as ws:
            ws.receive_text()
    assert closed.value.code == 1008  # refused by the guard, nothing sent

    (probe,) = _probes_named(registry, "ws_secret")
    assert asyncio.run(probe.check()).details["total_connections"] == 1
