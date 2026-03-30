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
