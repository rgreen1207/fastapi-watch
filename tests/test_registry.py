import asyncio
import json
import logging
import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi_watch import HealthRegistry
from fastapi_watch.probes.memory import MemoryProbe
from fastapi_watch.models import HealthReport, ProbeResult, ProbeStatus
from fastapi_watch.registry import _normalize_interval


@pytest.fixture
def client_with_probes():
    app = FastAPI()
    registry = HealthRegistry(app)
    registry.add(MemoryProbe(name="svc-a"))
    registry.add(MemoryProbe(name="svc-b"))
    return TestClient(app)


def test_liveness_always_200(client_with_probes):
    resp = client_with_probes.get("/health/live")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_readiness_200_when_all_healthy(client_with_probes):
    resp = client_with_probes.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_status_returns_all_probes(client_with_probes):
    resp = client_with_probes.get("/health/status")
    assert resp.status_code == 200
    names = {p["name"] for p in resp.json()["probes"]}
    assert names == {"svc-a", "svc-b"}


def test_readiness_503_when_probe_unhealthy():
    class FailingProbe(MemoryProbe):
        name = "db"

        async def check(self):
            return ProbeResult(name="db", status=ProbeStatus.UNHEALTHY, error="timeout")

    app = FastAPI()
    registry = HealthRegistry(app)
    registry.add(FailingProbe())
    client = TestClient(app)
    resp = client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["status"] == "unhealthy"


def test_status_207_when_any_probe_unhealthy():
    class FailingProbe(MemoryProbe):
        name = "cache"

        async def check(self):
            return ProbeResult(name="cache", status=ProbeStatus.UNHEALTHY, error="timeout")

    app = FastAPI()
    registry = HealthRegistry(app)
    registry.add(MemoryProbe(name="ok"))
    registry.add(FailingProbe())
    client = TestClient(app)
    resp = client.get("/health/status")
    assert resp.status_code == 207


def test_empty_registry_reports_healthy():
    app = FastAPI()
    HealthRegistry(app)
    client = TestClient(app)
    resp = client.get("/health/ready")
    assert resp.status_code == 200


def test_custom_prefix():
    app = FastAPI()
    registry = HealthRegistry(app, prefix="/ops/health")
    registry.add(MemoryProbe())
    client = TestClient(app)
    assert client.get("/ops/health/live").status_code == 200
    assert client.get("/health/live").status_code == 404


def test_add_chaining():
    app = FastAPI()
    registry = HealthRegistry(app)
    result = registry.add(MemoryProbe(name="a")).add(MemoryProbe(name="b"))
    assert result is registry
    assert len(registry._probes) == 2


def test_add_probes():
    app = FastAPI()
    registry = HealthRegistry(app)
    result = registry.add_probes([MemoryProbe(name="a"), MemoryProbe(name="b"), MemoryProbe(name="c")])
    assert result is registry
    assert len(registry._probes) == 3


def test_add_probes_skips_duplicates():
    app = FastAPI()
    registry = HealthRegistry(app)
    probe = MemoryProbe(name="a")
    registry.add_probes([probe, probe])
    assert len(registry._probes) == 1


def test_add_skips_duplicate():
    app = FastAPI()
    registry = HealthRegistry(app)
    probe = MemoryProbe(name="a")
    registry.add(probe)
    registry.add(probe)
    assert len(registry._probes) == 1


@pytest.mark.asyncio
async def test_run_all_returns_probe_results():
    app = FastAPI()
    registry = HealthRegistry(app)
    registry.add(MemoryProbe(name="a"))
    registry.add(MemoryProbe(name="b"))
    results = await registry.run_all()
    assert len(results) == 2
    assert all(r.status == ProbeStatus.HEALTHY for r in results)


@pytest.mark.asyncio
async def test_probe_exception_becomes_unhealthy_result():
    class BombProbe(MemoryProbe):
        name = "bomb"

        async def check(self):
            raise RuntimeError("exploded")

    app = FastAPI()
    registry = HealthRegistry(app)
    registry.add(BombProbe())
    results = await registry.run_all()
    assert results[0].status == ProbeStatus.UNHEALTHY
    assert "RuntimeError" in results[0].error


# ---------------------------------------------------------------------------
# Polling: _normalize_interval
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ms, expected", [
    (None, None),
    (0, None),
    (500, 1000),
    (999, 1000),
    (1000, 1000),
    (5000, 5000),
    (60_000, 60_000),
])
def test_normalize_interval(ms, expected):
    assert _normalize_interval(ms) == expected


# ---------------------------------------------------------------------------
# Polling: __init__ defaults and overrides
# ---------------------------------------------------------------------------


def test_default_poll_interval_is_60s():
    app = FastAPI()
    registry = HealthRegistry(app)
    assert registry._poll_interval_ms == 60_000


def test_poll_interval_none_disables_polling():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    assert registry._poll_interval_ms is None


def test_poll_interval_zero_disables_polling():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=0)
    assert registry._poll_interval_ms is None


def test_poll_interval_below_minimum_is_clamped():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=200)
    assert registry._poll_interval_ms == 1000


# ---------------------------------------------------------------------------
# Polling: set_poll_interval
# ---------------------------------------------------------------------------


def test_set_poll_interval_updates_value():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.set_poll_interval(5000)
    assert registry._poll_interval_ms == 5000


def test_set_poll_interval_to_zero_clears_cache():
    app = FastAPI()
    registry = HealthRegistry(app)
    registry._cached_results = [ProbeResult(name="x", status=ProbeStatus.HEALTHY)]
    registry.set_poll_interval(0)
    assert registry._poll_interval_ms is None
    assert registry._cached_results is None


def test_set_poll_interval_below_minimum_is_clamped():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.set_poll_interval(100)
    assert registry._poll_interval_ms == 1000


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------


def test_no_logger_by_default():
    app = FastAPI()
    registry = HealthRegistry(app)
    assert registry._logger is None


def test_custom_logger_stored():
    app = FastAPI()
    custom = logging.getLogger("my_app")
    registry = HealthRegistry(app, logger=custom)
    assert registry._logger is custom


def test_logger_warns_on_clamped_interval():
    app = FastAPI()
    mock_logger = MagicMock(spec=logging.Logger)
    HealthRegistry(app, poll_interval_ms=200, logger=mock_logger)
    mock_logger.warning.assert_called_once()
    args = mock_logger.warning.call_args[0]
    assert 200 in args
    assert 1000 in args


def test_no_warning_without_logger():
    """Should not raise even when interval is clamped and no logger is set."""
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=200)
    assert registry._poll_interval_ms == 1000  # still clamped


@pytest.mark.asyncio
async def test_logger_records_probe_exception():
    class BombProbe(MemoryProbe):
        name = "bomb"

        async def check(self):
            raise RuntimeError("exploded")

    app = FastAPI()
    mock_logger = MagicMock(spec=logging.Logger)
    registry = HealthRegistry(app, logger=mock_logger)
    registry.add(BombProbe())
    results = await registry.run_all()
    assert results[0].status == ProbeStatus.UNHEALTHY
    mock_logger.exception.assert_called_once()


@pytest.mark.asyncio
async def test_no_log_on_probe_exception_without_logger():
    """Probe exceptions must be captured silently when no logger is set."""
    class BombProbe(MemoryProbe):
        name = "bomb"

        async def check(self):
            raise RuntimeError("exploded")

    app = FastAPI()
    registry = HealthRegistry(app)
    registry.add(BombProbe())
    results = await registry.run_all()
    assert results[0].status == ProbeStatus.UNHEALTHY
    assert "RuntimeError" in results[0].error


# ---------------------------------------------------------------------------
# Polling: single-fetch mode (poll_interval_ms=None) via HTTP
# ---------------------------------------------------------------------------


def test_single_fetch_mode_readiness():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(MemoryProbe(name="mem"))
    client = TestClient(app)
    resp = client.get("/health/ready")
    assert resp.status_code == 200


def test_single_fetch_mode_status():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(MemoryProbe(name="mem"))
    client = TestClient(app)
    resp = client.get("/health/status")
    assert resp.status_code == 200
    assert resp.json()["probes"][0]["name"] == "mem"


# ---------------------------------------------------------------------------
# Polling: background task caches results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_loop_populates_cache():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=60_000)
    registry.add(MemoryProbe(name="mem"))

    # Simulate startup then run the poll loop once manually.
    registry._cached_results = await registry.run_all()
    assert registry._cached_results is not None
    assert registry._cached_results[0].name == "mem"


@pytest.mark.asyncio
async def test_get_results_uses_cache_in_poll_mode():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=60_000)

    cached = [ProbeResult(name="cached", status=ProbeStatus.HEALTHY)]
    registry._cached_results = cached

    results = await registry._get_results()
    assert results is cached


@pytest.mark.asyncio
async def test_get_results_runs_fresh_in_single_fetch_mode():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(MemoryProbe(name="fresh"))

    results = await registry._get_results()
    assert len(results) == 1
    assert results[0].name == "fresh"


# ---------------------------------------------------------------------------
# Polling: demand-driven via SSE connections
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_task_starts_on_first_connect():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=60_000)
    registry.add(MemoryProbe(name="mem"))

    assert registry._poll_task is None
    await registry._on_connect()
    assert registry._poll_task is not None
    assert not registry._poll_task.done()
    registry._cancel_poll_task()


@pytest.mark.asyncio
async def test_poll_task_stops_when_last_client_disconnects():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=60_000)
    registry.add(MemoryProbe(name="mem"))

    await registry._on_connect()
    await registry._on_connect()
    assert registry._poll_task is not None

    await registry._on_disconnect()
    assert registry._poll_task is not None  # still one client connected

    await registry._on_disconnect()
    assert registry._poll_task is None  # last client gone


@pytest.mark.asyncio
async def test_poll_task_not_started_when_interval_is_none():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(MemoryProbe(name="mem"))

    await registry._on_connect()
    assert registry._poll_task is None
    await registry._on_disconnect()


@pytest.mark.asyncio
async def test_set_poll_interval_restarts_task_when_connections_active():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=60_000)
    registry.add(MemoryProbe(name="mem"))

    await registry._on_connect()
    original_task = registry._poll_task
    registry.set_poll_interval(5_000)
    assert registry._poll_task is not original_task
    assert registry._poll_interval_ms == 5_000
    registry._cancel_poll_task()
    await registry._on_disconnect()


# ---------------------------------------------------------------------------
# Polling: SSE HTTP endpoints
# ---------------------------------------------------------------------------


def test_sse_endpoints_single_fetch_mode():
    """In single-fetch mode the generator returns after one event so the stream
    closes naturally — safe to consume fully with TestClient."""
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(MemoryProbe(name="mem"))
    client = TestClient(app)

    for path in ("/health/status/stream", "/health/ready/stream"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        data_lines = [l for l in resp.text.splitlines() if l.startswith("data:")]
        assert len(data_lines) == 1
        data = json.loads(data_lines[0][len("data:"):].strip())
        assert "status" in data


@pytest.mark.asyncio
async def test_event_stream_generator_yields_result_and_stops_on_disconnect():
    """Test the SSE generator directly: verify it yields probe data and cleans
    up when the mocked request reports a disconnect."""
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=1000)
    registry.add(MemoryProbe(name="mem"))

    disconnect_after = 2
    call_count = 0

    async def is_disconnected() -> bool:
        nonlocal call_count
        call_count += 1
        return call_count >= disconnect_after

    request = AsyncMock()
    request.is_disconnected = is_disconnected

    def make_report(results: list) -> dict:
        return HealthReport.from_results(results).model_dump()

    events = []
    async for chunk in registry._event_stream(request, make_report):
        events.append(chunk)

    assert len(events) >= 1
    assert events[0].startswith("data:")
    payload = json.loads(events[0][len("data:"):].strip().rstrip("\n"))
    assert payload["status"] == "healthy"
    assert registry._active_connections == 0
    assert registry._poll_task is None
