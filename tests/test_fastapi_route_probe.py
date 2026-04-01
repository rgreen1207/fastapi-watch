import asyncio
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from fastapi_watch import HealthRegistry, FastAPIRouteProbe
from fastapi_watch.models import ProbeStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _call(wrapper, *args, **kwargs):
    """Call a wrapped handler, swallowing expected exceptions."""
    try:
        return await wrapper(*args, **kwargs)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Before any requests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_before_any_requests_is_healthy():
    probe = FastAPIRouteProbe(name="api")
    result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.name == "api"
    assert result.details["message"] == "no requests observed yet"


@pytest.mark.asyncio
async def test_latency_ms_zero_before_any_requests():
    probe = FastAPIRouteProbe(name="api")
    result = await probe.check()
    assert result.latency_ms == 0.0


# ---------------------------------------------------------------------------
# RTT recording — async handlers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rtt_recorded_for_async_handler():
    probe = FastAPIRouteProbe(name="api")

    @probe.watch
    async def handler():
        return {"ok": True}

    await handler()
    result = await probe.check()
    assert result.details["last_rtt_ms"] is not None
    assert result.details["last_rtt_ms"] >= 0


@pytest.mark.asyncio
async def test_avg_rtt_ms_updated_after_requests():
    probe = FastAPIRouteProbe(name="api")

    @probe.watch
    async def handler():
        return {"ok": True}

    await handler()
    await handler()
    result = await probe.check()
    assert result.details["avg_rtt_ms"] is not None
    assert result.details["avg_rtt_ms"] >= 0


@pytest.mark.asyncio
async def test_request_count_increments():
    probe = FastAPIRouteProbe(name="api")

    @probe.watch
    async def handler():
        return {}

    for _ in range(5):
        await handler()

    result = await probe.check()
    assert result.details["request_count"] == 5


# ---------------------------------------------------------------------------
# RTT recording — sync handlers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rtt_recorded_for_sync_handler():
    probe = FastAPIRouteProbe(name="api")

    @probe.watch
    def handler():
        return {"ok": True}

    handler()
    result = await probe.check()
    assert result.details["last_rtt_ms"] is not None
    assert result.details["request_count"] == 1


def test_sync_handler_still_returns_value():
    probe = FastAPIRouteProbe(name="api")

    @probe.watch
    def handler():
        return {"key": "value"}

    assert handler() == {"key": "value"}


# ---------------------------------------------------------------------------
# Status code capture
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_last_status_code_200_on_success():
    probe = FastAPIRouteProbe(name="api")

    @probe.watch
    async def handler():
        return {}

    await handler()
    result = await probe.check()
    assert result.details["last_status_code"] == 200


@pytest.mark.asyncio
async def test_http_exception_status_code_captured():
    probe = FastAPIRouteProbe(name="api")

    @probe.watch
    async def handler():
        raise HTTPException(status_code=404, detail="not found")

    await _call(handler)
    result = await probe.check()
    assert result.details["last_status_code"] == 404
    # 404 is below the default min_error_status of 500, so it does not count as an error
    assert result.details["error_count"] == 0


@pytest.mark.asyncio
async def test_http_exception_is_reraised():
    probe = FastAPIRouteProbe(name="api")

    @probe.watch
    async def handler():
        raise HTTPException(status_code=422)

    with pytest.raises(HTTPException) as exc_info:
        await handler()
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_unhandled_exception_recorded_as_500():
    probe = FastAPIRouteProbe(name="api")

    @probe.watch
    async def handler():
        raise ValueError("oops")

    await _call(handler)
    result = await probe.check()
    assert result.details["last_status_code"] == 500
    assert result.details["error_count"] == 1


@pytest.mark.asyncio
async def test_unhandled_exception_is_reraised():
    probe = FastAPIRouteProbe(name="api")

    @probe.watch
    async def handler():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await handler()


# ---------------------------------------------------------------------------
# Error rate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_error_rate_calculated_correctly():
    probe = FastAPIRouteProbe(name="api")

    @probe.watch
    async def handler(fail: bool):
        if fail:
            raise HTTPException(status_code=500)
        return {}

    for _ in range(8):
        await _call(handler, fail=False)
    for _ in range(2):
        await _call(handler, fail=True)

    result = await probe.check()
    assert abs(result.details["error_rate"] - 0.2) < 0.001


@pytest.mark.asyncio
async def test_healthy_when_error_rate_within_threshold():
    probe = FastAPIRouteProbe(name="api", max_error_rate=0.1)

    @probe.watch
    async def handler():
        return {}

    for _ in range(10):
        await handler()

    result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY


@pytest.mark.asyncio
async def test_unhealthy_when_error_rate_exceeds_threshold():
    probe = FastAPIRouteProbe(name="api", max_error_rate=0.1)

    @probe.watch
    async def handler(fail: bool):
        if fail:
            raise HTTPException(status_code=500)
        return {}

    for _ in range(8):
        await _call(handler, fail=False)
    for _ in range(2):
        await _call(handler, fail=True)  # 20% error rate

    result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY
    assert "error rate" in result.error


# ---------------------------------------------------------------------------
# Consecutive errors
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_consecutive_errors_increments_on_failure():
    probe = FastAPIRouteProbe(name="api")

    @probe.watch
    async def fail():
        raise HTTPException(status_code=503)

    for _ in range(3):
        await _call(fail)

    result = await probe.check()
    assert result.details["consecutive_errors"] == 3


@pytest.mark.asyncio
async def test_consecutive_errors_resets_on_success():
    probe = FastAPIRouteProbe(name="api")

    @probe.watch
    async def handler(fail: bool):
        if fail:
            raise HTTPException(status_code=503)
        return {}

    for _ in range(3):
        await _call(handler, fail=True)
    await _call(handler, fail=False)

    result = await probe.check()
    assert result.details["consecutive_errors"] == 0


# ---------------------------------------------------------------------------
# RTT thresholds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_healthy_when_avg_rtt_within_threshold():
    probe = FastAPIRouteProbe(name="api", max_avg_rtt_ms=10_000)

    @probe.watch
    async def handler():
        return {}

    await handler()
    result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY


@pytest.mark.asyncio
async def test_unhealthy_when_avg_rtt_exceeds_threshold():
    probe = FastAPIRouteProbe(name="api", max_avg_rtt_ms=1)  # 1 ms threshold

    @probe.watch
    async def handler():
        await asyncio.sleep(0.05)  # 50 ms — reliably exceeds 1 ms on all platforms
        return {}

    await handler()
    result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY
    assert "avg RTT" in result.error


@pytest.mark.asyncio
async def test_no_rtt_threshold_by_default():
    probe = FastAPIRouteProbe(name="api")
    assert probe.max_avg_rtt_ms is None

    @probe.watch
    async def handler():
        await asyncio.sleep(0.05)
        return {}

    await handler()
    result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY


# ---------------------------------------------------------------------------
# p95, min, max RTT
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_min_max_rtt_tracked():
    probe = FastAPIRouteProbe(name="api")

    @probe.watch
    async def handler():
        return {}

    for _ in range(5):
        await handler()

    result = await probe.check()
    assert result.details["min_rtt_ms"] is not None
    assert result.details["max_rtt_ms"] is not None
    assert result.details["min_rtt_ms"] <= result.details["max_rtt_ms"]


@pytest.mark.asyncio
async def test_p95_rtt_none_before_requests():
    probe = FastAPIRouteProbe(name="api")
    result = await probe.check()
    # No requests yet — p95 not in details (or None)
    assert result.details.get("p95_rtt_ms") is None


@pytest.mark.asyncio
async def test_p95_rtt_populated_after_requests():
    probe = FastAPIRouteProbe(name="api")

    @probe.watch
    async def handler():
        return {}

    for _ in range(10):
        await handler()

    result = await probe.check()
    assert result.details["p95_rtt_ms"] is not None
    assert result.details["p95_rtt_ms"] >= result.details["min_rtt_ms"]


# ---------------------------------------------------------------------------
# Throughput
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_requests_per_minute_none_before_two_requests():
    probe = FastAPIRouteProbe(name="api")

    @probe.watch
    async def handler():
        return {}

    await handler()
    result = await probe.check()
    assert result.details.get("requests_per_minute") is None


@pytest.mark.asyncio
async def test_requests_per_minute_populated_after_two_requests():
    probe = FastAPIRouteProbe(name="api")

    @probe.watch
    async def handler():
        return {}

    await handler()
    await handler()
    result = await probe.check()
    assert result.details.get("requests_per_minute") is not None
    assert result.details["requests_per_minute"] >= 0


# ---------------------------------------------------------------------------
# FastAPI integration
# ---------------------------------------------------------------------------

def test_decorated_route_still_serves_responses():
    probe = FastAPIRouteProbe(name="items")
    app = FastAPI()

    @app.get("/items")
    @probe.watch
    async def list_items():
        return {"items": [1, 2, 3]}

    client = TestClient(app)
    resp = client.get("/items")
    assert resp.status_code == 200
    assert resp.json() == {"items": [1, 2, 3]}


def test_decorated_route_records_stats_via_real_requests():
    probe = FastAPIRouteProbe(name="items")
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(probe)

    @app.get("/items")
    @probe.watch
    async def list_items():
        return {"items": []}

    client = TestClient(app)
    client.get("/items")
    client.get("/items")

    resp = client.get("/health/status")
    probes = {p["name"]: p for p in resp.json()["probes"]}
    assert "items" in probes
    assert probes["items"]["details"]["request_count"] == 2


def test_decorated_route_records_http_exception_status_code():
    probe = FastAPIRouteProbe(name="items", max_error_rate=1.0, min_error_status=400)  # never unhealthy; count 4xx
    app = FastAPI()

    @app.get("/items/{item_id}")
    @probe.watch
    async def get_item(item_id: int):
        if item_id == 0:
            raise HTTPException(status_code=404, detail="not found")
        return {"id": item_id}

    client = TestClient(app)
    client.get("/items/0")  # 404

    import asyncio
    result = asyncio.get_event_loop().run_until_complete(probe.check())
    assert result.details["last_status_code"] == 404
    assert result.details["error_count"] == 1


def test_watch_preserves_function_name():
    probe = FastAPIRouteProbe(name="api")

    @probe.watch
    async def my_handler():
        return {}

    assert my_handler.__name__ == "my_handler"


def test_watch_preserves_sync_function_name():
    probe = FastAPIRouteProbe(name="api")

    @probe.watch
    def my_sync_handler():
        return {}

    assert my_sync_handler.__name__ == "my_sync_handler"


# ---------------------------------------------------------------------------
# latency_ms on ProbeResult mirrors avg_rtt_ms
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_probe_result_latency_ms_equals_avg_rtt():
    probe = FastAPIRouteProbe(name="api")

    @probe.watch
    async def handler():
        return {}

    for _ in range(3):
        await handler()

    result = await probe.check()
    assert result.latency_ms == result.details["avg_rtt_ms"]


# ---------------------------------------------------------------------------
# Name
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_probe_name_in_result():
    probe = FastAPIRouteProbe(name="checkout")

    @probe.watch
    async def handler():
        return {}

    await handler()
    result = await probe.check()
    assert result.name == "checkout"


# ---------------------------------------------------------------------------
# Percentiles (p50, p99)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p50_and_p99_rtt_present_after_requests():
    probe = FastAPIRouteProbe(name="api")

    @probe.watch
    async def handler():
        return {}

    for _ in range(5):
        await handler()

    result = await probe.check()
    assert result.details["p50_rtt_ms"] is not None
    assert result.details["p99_rtt_ms"] is not None


# ---------------------------------------------------------------------------
# Slow calls
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_slow_calls_counted_when_threshold_set():
    probe = FastAPIRouteProbe(name="api", slow_call_threshold_ms=1)

    @probe.watch
    async def handler():
        await asyncio.sleep(0.05)
        return {}

    await handler()
    result = await probe.check()
    assert "slow_calls" in result.details
    assert result.details["slow_calls"] == 1


@pytest.mark.asyncio
async def test_slow_calls_absent_without_threshold():
    probe = FastAPIRouteProbe(name="api")

    @probe.watch
    async def handler():
        return {}

    await handler()
    result = await probe.check()
    assert "slow_calls" not in result.details


# ---------------------------------------------------------------------------
# Status distribution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_distribution_tracks_code_families():
    probe = FastAPIRouteProbe(name="api", max_error_rate=1.0)

    @probe.watch
    async def handler(fail: bool):
        if fail:
            raise HTTPException(status_code=503)
        return {}

    for _ in range(3):
        await handler(fail=False)
    await _call(handler, fail=True)

    result = await probe.check()
    dist = result.details["status_distribution"]
    assert dist.get("2xx") == 3
    assert dist.get("5xx") == 1


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_error_types_tracked_for_exceptions():
    probe = FastAPIRouteProbe(name="api", max_error_rate=1.0)

    @probe.watch
    async def handler():
        raise ValueError("bad input")

    await _call(handler)
    result = await probe.check()
    assert "error_types" in result.details
    assert result.details["error_types"].get("ValueError") == 1


# ---------------------------------------------------------------------------
# Cache hit / miss recording
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_hit_miss_recording():
    probe = FastAPIRouteProbe(name="api")

    probe.record_cache_hit()
    probe.record_cache_hit()
    probe.record_cache_miss()

    @probe.watch
    async def handler():
        return {}

    await handler()
    result = await probe.check()
    assert result.details["cache_hits"] == 2
    assert result.details["cache_misses"] == 1


# ---------------------------------------------------------------------------
# last_error_at / last_success_at
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_last_error_at_set_after_error():
    probe = FastAPIRouteProbe(name="api")

    @probe.watch
    async def handler():
        raise HTTPException(status_code=500)

    await _call(handler)
    result = await probe.check()
    assert "last_error_at" in result.details
    assert result.details["last_error_at"] is not None


@pytest.mark.asyncio
async def test_last_error_at_absent_before_any_error():
    probe = FastAPIRouteProbe(name="api")

    @probe.watch
    async def handler():
        return {}

    await handler()
    result = await probe.check()
    assert "last_error_at" not in result.details


@pytest.mark.asyncio
async def test_last_success_at_absent_when_not_mostly_failing():
    probe = FastAPIRouteProbe(name="api", window_size=10)

    @probe.watch
    async def handler(fail: bool):
        if fail:
            raise HTTPException(status_code=500)
        return {}

    for _ in range(5):
        await handler(fail=False)
    for _ in range(5):
        await _call(handler, fail=True)

    result = await probe.check()
    # 50% failure rate — below the 99% threshold
    assert "last_success_at" not in result.details


@pytest.mark.asyncio
async def test_last_success_at_shown_when_mostly_failing():
    probe = FastAPIRouteProbe(name="api", window_size=10)

    @probe.watch
    async def handler(fail: bool):
        if fail:
            raise HTTPException(status_code=500)
        return {}

    await handler(fail=False)  # records last_success_at
    for _ in range(10):
        await _call(handler, fail=True)  # fills window with failures

    result = await probe.check()
    assert "last_success_at" in result.details
    assert result.details["last_success_at"] is not None


# ---------------------------------------------------------------------------
# circuit_breaker_enabled
# ---------------------------------------------------------------------------

def test_circuit_breaker_disabled_via_init():
    probe = FastAPIRouteProbe(name="api", circuit_breaker_enabled=False)
    assert probe.circuit_breaker_enabled is False


def test_circuit_breaker_enabled_by_default():
    probe = FastAPIRouteProbe(name="api")
    assert probe.circuit_breaker_enabled is True


# ---------------------------------------------------------------------------
# watch with label
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_watch_with_string_label():
    probe = FastAPIRouteProbe(name="api")

    @probe.watch("GET /users")
    async def handler():
        return {}

    await handler()
    result = await probe.check()
    assert result.details.get("description") == "GET /users"
