"""RequestMetricsMiddleware as pure ASGI middleware.

Covers what changed when it stopped being a ``BaseHTTPMiddleware``: response
time runs to the last body chunk, non-HTTP traffic passes straight through,
context variables propagate, fastapi-watch's own routes are not counted, and
the per-route table is capped.
"""
import asyncio
import contextvars
import time

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, StreamingResponse

from fastapi_watch import HealthRegistry, RequestMetricsMiddleware, RequestMetricsProbe
from fastapi_watch.middleware import OTHER_ROUTE, UNMATCHED_ROUTE


def _installed_middleware(app: FastAPI) -> RequestMetricsMiddleware:
    node = app.middleware_stack
    while not isinstance(node, RequestMetricsMiddleware):
        node = node.app
    return node


def _http_scope(path: str = "/") -> dict:
    return {"type": "http", "method": "GET", "path": path, "root_path": "", "headers": [], "query_string": b""}


async def _receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

def test_streaming_response_timed_until_last_chunk():
    app = FastAPI()

    @app.get("/stream")
    async def stream():
        async def chunks():
            for _ in range(3):
                await asyncio.sleep(0.05)
                yield b"x"

        return StreamingResponse(chunks())

    app.add_middleware(RequestMetricsMiddleware)
    client = TestClient(app)
    assert client.get("/stream").content == b"xxx"

    snap = _installed_middleware(app)._aggregate.snapshot()
    assert snap["request_count"] == 1
    assert snap["p50_rtt_ms"] >= 140  # three 50 ms chunks, not just the headers


def test_background_task_not_counted_as_response_time():
    app = FastAPI()

    @app.get("/bg")
    async def bg():
        return JSONResponse({"ok": True}, background=BackgroundTask(asyncio.sleep, 0.3))

    app.add_middleware(RequestMetricsMiddleware)
    TestClient(app).get("/bg")

    snap = _installed_middleware(app)._aggregate.snapshot()
    assert snap["request_count"] == 1
    assert snap["p50_rtt_ms"] < 250


# ---------------------------------------------------------------------------
# Status codes
# ---------------------------------------------------------------------------

def test_status_taken_from_response_start():
    app = FastAPI()

    @app.get("/teapot")
    async def teapot():
        return JSONResponse({}, status_code=418)

    app.add_middleware(RequestMetricsMiddleware, min_error_status=400)
    TestClient(app).get("/teapot")

    snap = _installed_middleware(app)._routes["/teapot"].snapshot()
    assert snap["request_count"] == 1
    assert snap["error_count"] == 1


def test_exception_mid_stream_counted_as_500_and_reraised():
    app = FastAPI()

    @app.get("/broken")
    async def broken():
        async def chunks():
            yield b"partial"
            raise RuntimeError("stream broke")

        return StreamingResponse(chunks())

    app.add_middleware(RequestMetricsMiddleware)
    with pytest.raises(RuntimeError, match="stream broke"):
        TestClient(app).get("/broken")

    snap = _installed_middleware(app)._aggregate.snapshot()
    assert snap["request_count"] == 1
    assert snap["error_count"] == 1


@pytest.mark.asyncio
async def test_response_that_never_started_counts_as_500():
    async def silent_app(scope, receive, send):
        return None

    middleware = RequestMetricsMiddleware(silent_app)
    await middleware(_http_scope(), _receive, lambda message: asyncio.sleep(0))

    snap = middleware._aggregate.snapshot()
    assert snap["request_count"] == 1
    assert snap["error_count"] == 1


@pytest.mark.asyncio
async def test_each_request_recorded_once():
    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"a", "more_body": True})
        await send({"type": "http.response.body", "body": b"b"})

    async def send(message):
        sent.append(message["type"])

    middleware = RequestMetricsMiddleware(app)
    await middleware(_http_scope(), _receive, send)

    assert sent == ["http.response.start", "http.response.body", "http.response.body"]
    snap = middleware._aggregate.snapshot()
    assert snap["request_count"] == 1
    assert snap["error_count"] == 0


# ---------------------------------------------------------------------------
# Pass-through
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_http_scopes_pass_straight_through():
    seen = []

    async def app(scope, receive, send):
        seen.append((scope["type"], receive, send))

    async def receive():
        return {}

    async def send(message):
        pass

    middleware = RequestMetricsMiddleware(app)
    for scope_type in ("lifespan", "websocket"):
        await middleware({"type": scope_type}, receive, send)

    # Not even a wrapped send: the app gets the server's own callables.
    assert seen == [("lifespan", receive, send), ("websocket", receive, send)]
    assert middleware._aggregate.snapshot()["request_count"] == 0


def test_websocket_routes_work_and_are_not_counted():
    app = FastAPI()

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    app.add_middleware(RequestMetricsMiddleware)
    with TestClient(app) as client:  # also runs lifespan through the middleware
        with client.websocket_connect("/ws") as conn:
            conn.send_text("ping")
            assert conn.receive_text() == "ping"

    assert _installed_middleware(app)._aggregate.snapshot()["request_count"] == 0


def test_context_variables_set_by_the_app_propagate_outward():
    request_id = contextvars.ContextVar("request_id", default=None)
    seen_outside = []

    class Outer:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            await self.app(scope, receive, send)
            if scope["type"] == "http":
                seen_outside.append(request_id.get())

    app = FastAPI()

    @app.get("/ok")
    async def ok():
        request_id.set("abc")
        return {}

    app.add_middleware(RequestMetricsMiddleware)
    app.add_middleware(Outer)  # added last, so it wraps RequestMetricsMiddleware
    TestClient(app).get("/ok")

    assert seen_outside == ["abc"]


# ---------------------------------------------------------------------------
# fastapi-watch's own routes are not counted
# ---------------------------------------------------------------------------

def test_health_routes_are_not_counted():
    app = FastAPI()

    @app.get("/ok")
    async def ok():
        return {}

    HealthRegistry(app, poll_interval_ms=None)
    app.add_middleware(RequestMetricsMiddleware)
    client = TestClient(app)
    for path in ("/health/live", "/health/ready", "/health/status", "/health/history", "/health/dashboard"):
        assert client.get(path).status_code < 500, path
    client.get("/ok")

    middleware = _installed_middleware(app)
    assert middleware._aggregate.snapshot()["request_count"] == 1
    assert set(middleware._routes) == {"/ok"}


def test_health_routes_under_a_router_prefix_are_not_counted():
    from fastapi import APIRouter

    app = FastAPI()
    sub = FastAPI()
    HealthRegistry(sub, poll_interval_ms=None, prefix="/hc")
    app.mount("/internal", sub)

    api = APIRouter()

    @api.get("/ping")
    async def ping():
        return {}

    app.include_router(api, prefix="/api")
    app.add_middleware(RequestMetricsMiddleware)
    client = TestClient(app)
    assert client.get("/internal/hc/live").status_code == 200
    client.get("/api/ping")

    assert set(_installed_middleware(app)._routes) == {"/api/ping"}


# ---------------------------------------------------------------------------
# Bounded route table and argument validation
# ---------------------------------------------------------------------------

def _tenant_app(**kwargs) -> FastAPI:
    app = FastAPI()
    sub = FastAPI()

    @sub.get("/items")
    async def items():
        return []

    @app.get("/ok")
    async def ok():
        return {}

    app.mount("/t/{tenant}", sub)
    app.add_middleware(RequestMetricsMiddleware, **kwargs)
    return app


def test_route_table_capped_at_max_routes():
    app = _tenant_app(max_routes=3)
    client = TestClient(app)
    client.get("/ok")
    for tenant in range(20):
        client.get(f"/t/{tenant}/items")
    client.get("/ok")

    middleware = _installed_middleware(app)
    routes = middleware._routes
    assert len(routes) == 4  # three keys plus the shared overflow bucket
    assert routes["/ok"].snapshot()["request_count"] == 2  # known keys keep their own bucket
    assert routes[OTHER_ROUTE].snapshot()["request_count"] == 18
    assert middleware._aggregate.snapshot()["request_count"] == 22


@pytest.mark.asyncio
async def test_overflow_bucket_reported_by_probe():
    app = _tenant_app(max_routes=2)
    client = TestClient(app)
    for tenant in range(5):
        client.get(f"/t/{tenant}/items")

    result = await RequestMetricsProbe(app).check()
    assert set(result.details["routes"]) == {"/t/0/items", "/t/1/items", OTHER_ROUTE}


def test_default_max_routes_is_500():
    middleware = RequestMetricsMiddleware(FastAPI())
    assert middleware._max_routes == 500
    assert UNMATCHED_ROUTE == "<unmatched>"


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"window_size": 0}, "window_size"),
        ({"window_size": -1}, "window_size"),
        ({"ema_alpha": 0}, "ema_alpha"),
        ({"ema_alpha": 1.5}, "ema_alpha"),
        ({"ema_alpha": -0.1}, "ema_alpha"),
        ({"max_routes": 0}, "max_routes"),
    ],
)
def test_invalid_arguments_rejected(kwargs, match):
    with pytest.raises(ValueError, match=match):
        RequestMetricsMiddleware(FastAPI(), **kwargs)


def test_boundary_arguments_accepted():
    middleware = RequestMetricsMiddleware(FastAPI(), window_size=1, ema_alpha=1, max_routes=1)
    assert middleware.per_route is True


# ---------------------------------------------------------------------------
# Overhead
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hot_path_overhead_is_small():
    """A loose guard against regressions: well under 50 µs per request."""
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def send(message):
        pass

    middleware = RequestMetricsMiddleware(app)
    scope = _http_scope()
    n = 2000
    began = time.perf_counter()
    for _ in range(n):
        await middleware(scope, _receive, send)
    per_request_us = (time.perf_counter() - began) / n * 1e6

    assert middleware._aggregate.snapshot()["request_count"] == n
    assert per_request_us < 50
