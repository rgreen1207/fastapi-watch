import asyncio
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from fastapi_watch import HealthRegistry, RequestMetricsMiddleware, RequestMetricsProbe
from fastapi_watch.models import ProbeStatus


def _make_app(per_route: bool = True, max_error_rate: float = 0.1, max_avg_rtt_ms=None):
    """Build a test app where the middleware instance is shared with the probe.

    Because app.add_middleware() creates a NEW instance internally, we instead
    wrap the FastAPI app with the middleware ourselves and hand the same object
    to RequestMetricsProbe.  TestClient receives the middleware as the ASGI app
    so every request passes through it.
    """
    app = FastAPI()

    @app.get("/ok")
    async def ok_route():
        return {"status": "ok"}

    @app.get("/fail")
    async def fail_route():
        raise HTTPException(status_code=500, detail="boom")

    registry = HealthRegistry(app, poll_interval_ms=None)
    middleware = RequestMetricsMiddleware(app, per_route=per_route)
    probe = RequestMetricsProbe(
        middleware,
        max_error_rate=max_error_rate,
        max_avg_rtt_ms=max_avg_rtt_ms,
    )
    registry.add(probe)

    # TestClient wraps the middleware so every request goes through it.
    # raise_server_exceptions=False lets 5xx responses come back as HTTP 500.
    client = TestClient(middleware, raise_server_exceptions=False)
    return middleware, probe, client


@pytest.mark.asyncio
async def test_no_requests_returns_healthy():
    app = FastAPI()
    middleware = RequestMetricsMiddleware(app)
    probe = RequestMetricsProbe(middleware)
    result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.details.get("message") == "no requests observed yet"


@pytest.mark.asyncio
async def test_healthy_on_successful_requests():
    _, probe, client = _make_app()
    for _ in range(5):
        client.get("/ok")

    result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.details["request_count"] >= 5
    assert result.details["error_rate"] == pytest.approx(0.0, abs=0.01)


@pytest.mark.asyncio
async def test_unhealthy_when_error_rate_exceeded():
    middleware, probe, client = _make_app(max_error_rate=0.1)

    # 9 failures, 1 success → 90% error rate
    for _ in range(9):
        client.get("/fail")
    client.get("/ok")

    result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY
    assert "error rate" in result.error


@pytest.mark.asyncio
async def test_per_route_breakdown():
    middleware, probe, client = _make_app(per_route=True)
    client.get("/ok")
    client.get("/ok")

    result = await probe.check()
    assert "routes" in result.details
    routes = result.details["routes"]
    assert any("/ok" in path for path in routes)


@pytest.mark.asyncio
async def test_no_per_route_when_disabled():
    middleware, probe, client = _make_app(per_route=False)
    client.get("/ok")

    result = await probe.check()
    assert "routes" not in result.details


@pytest.mark.asyncio
async def test_aggregate_counts_errors_correctly():
    middleware, probe, client = _make_app()
    client.get("/ok")
    client.get("/fail")

    snap = middleware._aggregate.snapshot()
    assert snap["error_count"] >= 1
    assert snap["request_count"] >= 2


@pytest.mark.asyncio
async def test_consecutive_errors_reset_on_success():
    middleware, probe, client = _make_app()
    client.get("/fail")
    client.get("/fail")
    client.get("/ok")

    snap = middleware._aggregate.snapshot()
    assert snap["consecutive_errors"] == 0


@pytest.mark.asyncio
async def test_avg_rtt_ms_is_numeric():
    middleware, probe, client = _make_app()
    for _ in range(3):
        client.get("/ok")
    snap = middleware._aggregate.snapshot()
    assert isinstance(snap["avg_rtt_ms"], float)
    assert snap["avg_rtt_ms"] >= 0


@pytest.mark.asyncio
async def test_probe_latency_ms_equals_avg_rtt():
    middleware, probe, client = _make_app()
    client.get("/ok")
    snap = middleware._aggregate.snapshot()
    result = await probe.check()
    assert result.latency_ms == snap["avg_rtt_ms"]


# ---------------------------------------------------------------------------
# Unhandled exceptions
# ---------------------------------------------------------------------------

def _installed_middleware(app: FastAPI) -> RequestMetricsMiddleware:
    """Return the instance Starlette built from ``app.add_middleware``."""
    node = app.middleware_stack
    while not isinstance(node, RequestMetricsMiddleware):
        node = node.app
    return node


def _crashing_app() -> FastAPI:
    app = FastAPI()

    @app.get("/items/{item_id}")
    async def crash(item_id: int):
        raise RuntimeError("unhandled")

    @app.get("/ok")
    async def ok_route():
        return {"status": "ok"}

    return app


@pytest.mark.asyncio
async def test_unhandled_exception_counted_as_500_via_add_middleware():
    app = _crashing_app()
    app.add_middleware(RequestMetricsMiddleware, per_route=True)
    client = TestClient(app, raise_server_exceptions=False)

    assert client.get("/items/1").status_code == 500
    middleware = _installed_middleware(app)
    snap = middleware._aggregate.snapshot()
    assert snap["request_count"] == 1
    assert snap["error_count"] == 1
    assert middleware._routes["/items/{item_id}"].snapshot()["error_count"] == 1


@pytest.mark.asyncio
async def test_unhandled_exception_still_propagates():
    app = _crashing_app()
    app.add_middleware(RequestMetricsMiddleware)

    with pytest.raises(Exception) as excinfo:
        TestClient(app).get("/items/1")

    # Starlette <0.28 runs BaseHTTPMiddleware in an anyio task group, which
    # delivers the original error inside an ExceptionGroup.
    def _contains_original(exc: BaseException) -> bool:
        if isinstance(exc, RuntimeError) and str(exc) == "unhandled":
            return True
        return any(_contains_original(inner) for inner in getattr(exc, "exceptions", ()))

    assert _contains_original(excinfo.value)


@pytest.mark.asyncio
async def test_unhandled_exception_counted_once_when_wrapping_app_directly():
    app = _crashing_app()
    middleware = RequestMetricsMiddleware(app)
    client = TestClient(middleware, raise_server_exceptions=False)

    assert client.get("/items/1").status_code == 500
    client.get("/ok")
    snap = middleware._aggregate.snapshot()
    assert snap["request_count"] == 2
    assert snap["error_count"] == 1


@pytest.mark.asyncio
async def test_unhandled_exception_makes_probe_unhealthy():
    app = _crashing_app()
    app.add_middleware(RequestMetricsMiddleware)
    client = TestClient(app, raise_server_exceptions=False)
    for _ in range(5):
        client.get("/items/1")

    result = await RequestMetricsProbe(_installed_middleware(app)).check()
    assert result.status == ProbeStatus.UNHEALTHY
