import asyncio
import json
import logging
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi_watch import HealthRegistry
from fastapi_watch.probes.noop import NoOpProbe
from fastapi_watch.models import HealthReport, ProbeResult, ProbeStatus
from fastapi_watch.registry import _normalize_interval


@pytest.fixture
def client_with_probes():
    app = FastAPI()
    registry = HealthRegistry(app)
    registry.add(NoOpProbe(name="svc-a"))
    registry.add(NoOpProbe(name="svc-b"))
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
    class FailingProbe(NoOpProbe):
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
    class FailingProbe(NoOpProbe):
        name = "cache"

        async def check(self):
            return ProbeResult(name="cache", status=ProbeStatus.UNHEALTHY, error="timeout")

    app = FastAPI()
    registry = HealthRegistry(app)
    registry.add(NoOpProbe(name="ok"))
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
    registry.add(NoOpProbe())
    client = TestClient(app)
    assert client.get("/ops/health/live").status_code == 200
    assert client.get("/health/live").status_code == 404


def test_add_chaining():
    app = FastAPI()
    registry = HealthRegistry(app)
    result = registry.add(NoOpProbe(name="a")).add(NoOpProbe(name="b"))
    assert result is registry
    assert len(registry._probes) == 2


def test_add_probes():
    app = FastAPI()
    registry = HealthRegistry(app)
    result = registry.add_probes([NoOpProbe(name="a"), NoOpProbe(name="b"), NoOpProbe(name="c")])
    assert result is registry
    assert len(registry._probes) == 3


def test_add_probes_skips_duplicates():
    app = FastAPI()
    registry = HealthRegistry(app)
    probe = NoOpProbe(name="a")
    registry.add_probes([probe, probe])
    assert len(registry._probes) == 1


def test_add_skips_duplicate():
    app = FastAPI()
    registry = HealthRegistry(app)
    probe = NoOpProbe(name="a")
    registry.add(probe)
    registry.add(probe)
    assert len(registry._probes) == 1


@pytest.mark.asyncio
async def test_run_all_returns_probe_results():
    app = FastAPI()
    registry = HealthRegistry(app)
    registry.add(NoOpProbe(name="a"))
    registry.add(NoOpProbe(name="b"))
    results = await registry.run_all()
    assert len(results) == 2
    assert all(r.status == ProbeStatus.HEALTHY for r in results)


@pytest.mark.asyncio
async def test_probe_exception_becomes_unhealthy_result():
    class BombProbe(NoOpProbe):
        name = "bomb"

        async def check(self):
            raise RuntimeError("exploded")

    app = FastAPI()
    registry = HealthRegistry(app)
    registry.add(BombProbe())
    results = await registry.run_all()
    assert results[0].status == ProbeStatus.UNHEALTHY
    assert results[0].error == "probe check failed"


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
    registry._storage._cache["x"] = ProbeResult(name="x", status=ProbeStatus.HEALTHY)
    registry._storage._cache_times["x"] = 0.0
    registry.set_poll_interval(0)
    assert registry._poll_interval_ms is None
    assert len(registry._storage._cache) == 0


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
    # One warning for clamped interval, one for missing auth — find the interval one
    interval_calls = [
        call for call in mock_logger.warning.call_args_list
        if 200 in call[0] or 1000 in call[0]
    ]
    assert interval_calls, "expected a warning about clamped interval"
    args = interval_calls[0][0]
    assert 200 in args
    assert 1000 in args


def test_no_warning_without_logger():
    """Should not raise even when interval is clamped and no logger is set."""
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=200)
    assert registry._poll_interval_ms == 1000  # still clamped


@pytest.mark.asyncio
async def test_logger_records_probe_exception():
    class BombProbe(NoOpProbe):
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
    class BombProbe(NoOpProbe):
        name = "bomb"

        async def check(self):
            raise RuntimeError("exploded")

    app = FastAPI()
    registry = HealthRegistry(app)
    registry.add(BombProbe())
    results = await registry.run_all()
    assert results[0].status == ProbeStatus.UNHEALTHY
    assert results[0].error == "probe check failed"


# ---------------------------------------------------------------------------
# Polling: single-fetch mode (poll_interval_ms=None) via HTTP
# ---------------------------------------------------------------------------


def test_single_fetch_mode_readiness():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(NoOpProbe(name="mem"))
    client = TestClient(app)
    resp = client.get("/health/ready")
    assert resp.status_code == 200


def test_single_fetch_mode_status():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(NoOpProbe(name="mem"))
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
    registry.add(NoOpProbe(name="mem"))

    # Simulate startup then run the poll loop once manually.
    await registry.run_all()
    cached = await registry._storage.get_all_latest()
    assert len(cached) > 0
    assert "mem" in cached


@pytest.mark.asyncio
async def test_get_results_uses_cache_in_poll_mode():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=60_000)

    cached_result = ProbeResult(name="cached", status=ProbeStatus.HEALTHY)
    registry._probes.append((NoOpProbe(name="cached"), True))
    await registry._storage.set_latest(cached_result)

    results = await registry._get_results()
    assert any(r.name == "cached" for r in results)
    # Polled probe with warm cache should not have been re-run
    assert await registry._storage.get_latest("cached") is cached_result


@pytest.mark.asyncio
async def test_get_results_runs_fresh_in_single_fetch_mode():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(NoOpProbe(name="fresh"))

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
    registry.add(NoOpProbe(name="mem"))

    assert registry._poll_task is None
    await registry._on_connect()
    assert registry._poll_task is not None
    assert not registry._poll_task.done()
    registry._cancel_poll_task()


@pytest.mark.asyncio
async def test_poll_task_stops_when_last_client_disconnects():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=60_000)
    registry.add(NoOpProbe(name="mem"))

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
    registry.add(NoOpProbe(name="mem"))

    await registry._on_connect()
    assert registry._poll_task is None
    await registry._on_disconnect()


@pytest.mark.asyncio
async def test_set_poll_interval_restarts_task_when_connections_active():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=60_000)
    registry.add(NoOpProbe(name="mem"))

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
    registry.add(NoOpProbe(name="mem"))
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
    registry.add(NoOpProbe(name="mem"))

    disconnect_after = 2
    call_count = 0

    async def is_disconnected() -> bool:
        nonlocal call_count
        call_count += 1
        return call_count >= disconnect_after

    request = AsyncMock()
    request.is_disconnected = is_disconnected

    def make_report(results: list) -> str:
        return HealthReport.from_results(results).model_dump_json()

    events = []
    async for chunk in registry._event_stream(request, make_report):
        events.append(chunk)

    assert len(events) >= 1
    assert events[0].startswith("data:")
    payload = json.loads(events[0][len("data:"):].strip().rstrip("\n"))
    assert payload["status"] == "healthy"
    assert registry._active_connections == 0
    assert registry._poll_task is None


# ---------------------------------------------------------------------------
# last_checked_at timestamp
# ---------------------------------------------------------------------------


def test_status_response_includes_checked_at():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(NoOpProbe(name="mem"))
    client = TestClient(app)
    resp = client.get("/health/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "checked_at" in data
    assert data["checked_at"] is not None


def test_ready_response_includes_checked_at():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(NoOpProbe(name="mem"))
    client = TestClient(app)
    resp = client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["checked_at"] is not None


@pytest.mark.asyncio
async def test_last_checked_at_set_after_run_all():
    app = FastAPI()
    registry = HealthRegistry(app)
    registry.add(NoOpProbe(name="mem"))
    assert registry._last_checked_at is None
    await registry.run_all()
    assert registry._last_checked_at is not None


@pytest.mark.asyncio
async def test_last_checked_at_is_utc():
    app = FastAPI()
    registry = HealthRegistry(app)
    registry.add(NoOpProbe(name="mem"))
    await registry.run_all()
    assert registry._last_checked_at.utcoffset().total_seconds() == 0


@pytest.mark.asyncio
async def test_timezone_applied_to_checked_at():
    from zoneinfo import ZoneInfo
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None, timezone="America/New_York")
    registry.add(NoOpProbe(name="mem"))
    await registry.run_all()
    assert registry._last_checked_at.tzinfo == ZoneInfo("America/New_York")


def test_timezone_in_status_response():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None, timezone="Europe/London")
    registry.add(NoOpProbe(name="mem"))
    client = TestClient(app)
    data = client.get("/health/status").json()
    assert data["timezone"] == "Europe/London"


def test_default_timezone_is_utc():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(NoOpProbe(name="mem"))
    client = TestClient(app)
    data = client.get("/health/status").json()
    assert data["timezone"] == "UTC"


def test_checked_at_none_before_first_run():
    app = FastAPI()
    registry = HealthRegistry(app)
    assert registry._last_checked_at is None


# ---------------------------------------------------------------------------
# Per-probe timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_timeout_becomes_unhealthy():
    class SlowProbe(NoOpProbe):
        timeout = 0.1  # 100 ms

        def __init__(self):
            super().__init__(name="slow")

        async def check(self):
            await asyncio.sleep(10)
            return await super().check()

    app = FastAPI()
    registry = HealthRegistry(app)
    registry.add(SlowProbe())
    results = await registry.run_all()
    assert results[0].status == ProbeStatus.UNHEALTHY
    assert results[0].error == "probe check failed"


@pytest.mark.asyncio
async def test_probe_no_timeout_by_default():
    """Probe with no timeout set completes normally."""
    app = FastAPI()
    registry = HealthRegistry(app)
    registry.add(NoOpProbe(name="mem"))
    results = await registry.run_all()
    assert results[0].status == ProbeStatus.HEALTHY


@pytest.mark.asyncio
async def test_probe_timeout_does_not_affect_other_probes():
    class SlowProbe(NoOpProbe):
        timeout = 0.1

        def __init__(self):
            super().__init__(name="slow")

        async def check(self):
            await asyncio.sleep(10)
            return await super().check()

    app = FastAPI()
    registry = HealthRegistry(app)
    registry.add(SlowProbe())
    registry.add(NoOpProbe(name="fast"))
    results = await registry.run_all()
    by_name = {r.name: r for r in results}
    assert by_name["slow"].status == ProbeStatus.UNHEALTHY
    assert by_name["fast"].status == ProbeStatus.HEALTHY


# ---------------------------------------------------------------------------
# Per-probe severity (critical flag)
# ---------------------------------------------------------------------------


def test_non_critical_probe_failure_does_not_affect_readiness():
    class FailingProbe(NoOpProbe):
        async def check(self):
            return ProbeResult(name=self.name, status=ProbeStatus.UNHEALTHY, error="down")

    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(NoOpProbe(name="ok"))
    registry.add(FailingProbe(name="non-critical"), critical=False)
    client = TestClient(app)
    resp = client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_critical_probe_failure_causes_503():
    class FailingProbe(NoOpProbe):
        async def check(self):
            return ProbeResult(name=self.name, status=ProbeStatus.UNHEALTHY, error="down")

    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(NoOpProbe(name="ok"))
    registry.add(FailingProbe(name="critical"), critical=True)
    client = TestClient(app)
    resp = client.get("/health/ready")
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_probe_result_includes_critical_flag():
    app = FastAPI()
    registry = HealthRegistry(app)
    registry.add(NoOpProbe(name="a"), critical=True)
    registry.add(NoOpProbe(name="b"), critical=False)
    results = await registry.run_all()
    by_name = {r.name: r for r in results}
    assert by_name["a"].critical is True
    assert by_name["b"].critical is False


def test_status_response_includes_critical_flag():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(NoOpProbe(name="mem"), critical=False)
    client = TestClient(app)
    data = client.get("/health/status").json()
    assert data["probes"][0]["critical"] is False


def test_non_critical_failure_still_appears_in_status():
    class FailingProbe(NoOpProbe):
        async def check(self):
            return ProbeResult(name=self.name, status=ProbeStatus.UNHEALTHY, error="down")

    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(FailingProbe(name="nc"), critical=False)
    client = TestClient(app)
    resp = client.get("/health/status")
    # 200 because no critical probes failed, but probe is still reported
    assert resp.status_code == 200
    probes = resp.json()["probes"]
    assert probes[0]["name"] == "nc"
    assert probes[0]["status"] == "unhealthy"


def test_add_probes_critical_flag_applied_to_all():
    app = FastAPI()
    registry = HealthRegistry(app)
    registry.add_probes([NoOpProbe(name="a"), NoOpProbe(name="b")], critical=False)
    # Both should be stored as non-critical
    for _, critical in registry._probes:
        assert critical is False


# ---------------------------------------------------------------------------
# State-change callbacks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_fired_on_state_change():
    events = []

    def on_change(name, old, new):
        events.append((name, old, new))

    class ToggleProbe(NoOpProbe):
        def __init__(self):
            super().__init__(name="toggle")
            self._healthy = True

        async def check(self):
            status = ProbeStatus.HEALTHY if self._healthy else ProbeStatus.UNHEALTHY
            return ProbeResult(name=self.name, status=status)

    app = FastAPI()
    registry = HealthRegistry(app)
    probe = ToggleProbe()
    registry.add(probe)
    registry.on_state_change(on_change)

    await registry.run_all()          # first run — stores initial state, no callback
    assert events == []

    probe._healthy = False
    await registry.run_all()          # healthy → unhealthy
    assert len(events) == 1
    assert events[0] == ("toggle", ProbeStatus.HEALTHY, ProbeStatus.UNHEALTHY)

    probe._healthy = True
    await registry.run_all()          # unhealthy → healthy
    assert len(events) == 2
    assert events[1] == ("toggle", ProbeStatus.UNHEALTHY, ProbeStatus.HEALTHY)


@pytest.mark.asyncio
async def test_callback_not_fired_when_status_unchanged():
    events = []

    registry = HealthRegistry(FastAPI())
    registry.add(NoOpProbe(name="stable"))
    registry.on_state_change(lambda n, o, nw: events.append((n, o, nw)))

    await registry.run_all()
    await registry.run_all()
    await registry.run_all()
    assert events == []


@pytest.mark.asyncio
async def test_async_callback_is_awaited():
    called = []

    async def async_cb(name, old, new):
        called.append(name)

    class ToggleProbe(NoOpProbe):
        def __init__(self):
            super().__init__(name="t")
            self._healthy = True

        async def check(self):
            status = ProbeStatus.HEALTHY if self._healthy else ProbeStatus.UNHEALTHY
            return ProbeResult(name=self.name, status=status)

    registry = HealthRegistry(FastAPI())
    probe = ToggleProbe()
    registry.add(probe)
    registry.on_state_change(async_cb)

    await registry.run_all()
    probe._healthy = False
    await registry.run_all()
    assert called == ["t"]


@pytest.mark.asyncio
async def test_on_state_change_returns_self():
    registry = HealthRegistry(FastAPI())
    result = registry.on_state_change(lambda n, o, nw: None)
    assert result is registry


@pytest.mark.asyncio
async def test_multiple_callbacks_all_fired():
    fired = {"a": 0, "b": 0}

    def cb_a(n, o, nw):
        fired["a"] += 1

    def cb_b(n, o, nw):
        fired["b"] += 1

    registry = HealthRegistry(FastAPI())

    class ToggleProbe(NoOpProbe):
        def __init__(self):
            super().__init__(name="t")
            self._healthy = True

        async def check(self):
            status = ProbeStatus.HEALTHY if self._healthy else ProbeStatus.UNHEALTHY
            return ProbeResult(name=self.name, status=status)

    probe = ToggleProbe()
    registry.add(probe)
    registry.on_state_change(cb_a)
    registry.on_state_change(cb_b)

    await registry.run_all()
    probe._healthy = False
    await registry.run_all()
    assert fired == {"a": 1, "b": 1}


# ---------------------------------------------------------------------------
# Startup grace period
# ---------------------------------------------------------------------------


def test_grace_period_ready_returns_503_starting():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None, grace_period_ms=60_000)
    registry.add(NoOpProbe(name="mem"))
    client = TestClient(app)
    resp = client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["status"] == "starting"


def test_grace_period_zero_does_not_block_readiness():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None, grace_period_ms=0)
    registry.add(NoOpProbe(name="mem"))
    client = TestClient(app)
    resp = client.get("/health/ready")
    assert resp.status_code == 200


def test_grace_period_status_endpoint_not_affected():
    """Grace period only blocks /ready — /status still returns real results."""
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None, grace_period_ms=60_000)
    registry.add(NoOpProbe(name="mem"))
    client = TestClient(app)
    resp = client.get("/health/status")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_grace_period_expired_allows_readiness():
    from datetime import timedelta
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None, grace_period_ms=1_000)
    registry.add(NoOpProbe(name="mem"))
    # Backdate start_time so grace period appears expired
    registry._start_time = datetime.now(timezone.utc) - timedelta(seconds=2)
    client = TestClient(app)
    resp = client.get("/health/ready")
    assert resp.status_code == 200


def test_in_grace_period_true_when_active():
    app = FastAPI()
    registry = HealthRegistry(app, grace_period_ms=60_000)
    assert registry._in_grace_period() is True


def test_in_grace_period_false_when_zero():
    app = FastAPI()
    registry = HealthRegistry(app, grace_period_ms=0)
    assert registry._in_grace_period() is False


# ---------------------------------------------------------------------------
# Probe result history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_populated_after_run_all():
    app = FastAPI()
    registry = HealthRegistry(app, history_size=5)
    registry.add(NoOpProbe(name="mem"))
    await registry.run_all()
    history = await registry._storage.get_history()
    assert "mem" in history
    assert len(history["mem"]) == 1


@pytest.mark.asyncio
async def test_history_respects_history_size():
    app = FastAPI()
    registry = HealthRegistry(app, history_size=3)
    registry.add(NoOpProbe(name="mem"))
    for _ in range(10):
        await registry.run_all()
    history = await registry._storage.get_history()
    assert len(history["mem"]) == 3


@pytest.mark.asyncio
async def test_history_entries_are_probe_results():
    app = FastAPI()
    registry = HealthRegistry(app, history_size=5)
    registry.add(NoOpProbe(name="mem"))
    await registry.run_all()
    history = await registry._storage.get_history()
    entry = history["mem"][0]
    assert isinstance(entry, ProbeResult)
    assert entry.name == "mem"
    assert entry.status == ProbeStatus.HEALTHY


def test_history_endpoint_returns_empty_before_any_run():
    app = FastAPI()
    HealthRegistry(app, poll_interval_ms=None)
    client = TestClient(app)
    resp = client.get("/health/history")
    assert resp.status_code == 200
    assert resp.json()["probes"] == {}


def test_history_endpoint_returns_results_after_run():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None, history_size=5)
    registry.add(NoOpProbe(name="mem"))
    client = TestClient(app)
    # Trigger a run via /status so history is populated
    client.get("/health/status")
    resp = client.get("/health/history")
    assert resp.status_code == 200
    data = resp.json()
    assert "mem" in data["probes"]
    assert len(data["probes"]["mem"]) == 1
    assert data["probes"]["mem"][0]["status"] == "healthy"


def test_history_default_size_is_120():
    app = FastAPI()
    registry = HealthRegistry(app)
    assert registry._history_size == 120


# ---------------------------------------------------------------------------
# Per-probe poll frequency
# ---------------------------------------------------------------------------


def test_probe_poll_interval_overrides_registry_default():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=60_000)
    probe = NoOpProbe(name="mem")
    probe.poll_interval_ms = 5_000
    registry.add(probe)
    assert registry._effective_interval_s(probe) == 5.0


def test_probe_poll_interval_falls_back_to_registry():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=30_000)
    probe = NoOpProbe(name="mem")
    # poll_interval_ms is None by default
    assert registry._effective_interval_s(probe) == 30.0


def test_probe_poll_interval_none_in_single_fetch_mode():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    probe = NoOpProbe(name="mem")
    assert registry._effective_interval_s(probe) is None


def test_is_probe_due_when_never_run():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=60_000)
    probe = NoOpProbe(name="mem")
    registry.add(probe)
    assert registry._is_probe_due(probe, now=1000.0) is True


def test_is_probe_due_after_interval_elapsed():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=10_000)
    probe = NoOpProbe(name="mem")
    registry.add(probe)
    registry._probe_last_run["mem"] = 1000.0
    assert registry._is_probe_due(probe, now=1010.1) is True


def test_is_probe_not_due_before_interval():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=10_000)
    probe = NoOpProbe(name="mem")
    registry.add(probe)
    registry._probe_last_run["mem"] = 1000.0
    assert registry._is_probe_due(probe, now=1005.0) is False


def test_has_custom_intervals_true():
    app = FastAPI()
    registry = HealthRegistry(app)
    probe = NoOpProbe(name="mem")
    probe.poll_interval_ms = 5_000
    registry.add(probe)
    assert registry._has_custom_intervals() is True


def test_has_custom_intervals_false():
    app = FastAPI()
    registry = HealthRegistry(app)
    registry.add(NoOpProbe(name="mem"))
    assert registry._has_custom_intervals() is False


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_circuit_opens_after_threshold():
    class AlwaysFail(NoOpProbe):
        async def check(self):
            return ProbeResult(name=self.name, status=ProbeStatus.UNHEALTHY, error="fail")

    app = FastAPI()
    registry = HealthRegistry(app, circuit_breaker_threshold=3, circuit_breaker_cooldown_ms=60_000)
    probe = AlwaysFail(name="bad")
    registry.add(probe)

    for _ in range(3):
        await registry._safe_check(probe, critical=True)

    assert probe.name in registry._circuit_open_until


@pytest.mark.asyncio
async def test_circuit_returns_cached_when_open():
    class AlwaysFail(NoOpProbe):
        async def check(self):
            return ProbeResult(name=self.name, status=ProbeStatus.UNHEALTHY, error="fail")

    app = FastAPI()
    registry = HealthRegistry(app, circuit_breaker_threshold=2, circuit_breaker_cooldown_ms=60_000)
    probe = AlwaysFail(name="bad")
    registry.add(probe)

    # Trip the circuit
    for _ in range(2):
        await registry._safe_check(probe, critical=True)

    # Next call should be served from cache with circuit_breaker dict showing open=True
    result = await registry._safe_check(probe, critical=True)
    assert result.details is not None
    cb = result.details.get("circuit_breaker", {})
    assert cb.get("open") is True


@pytest.mark.asyncio
async def test_circuit_resets_on_success():
    call_count = 0

    class SometimesFail(NoOpProbe):
        async def check(self):
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                return ProbeResult(name=self.name, status=ProbeStatus.UNHEALTHY, error="fail")
            return ProbeResult(name=self.name, status=ProbeStatus.HEALTHY)

    app = FastAPI()
    registry = HealthRegistry(app, circuit_breaker_threshold=3, circuit_breaker_cooldown_ms=0)
    probe = SometimesFail(name="flaky")
    registry.add(probe)

    # Trip the circuit
    for _ in range(3):
        await registry._safe_check(probe, critical=True)
    assert probe.name in registry._circuit_open_until

    # Force the circuit open_until to the past so it's eligible to retry
    registry._circuit_open_until[probe.name] = 0.0

    # Next run should succeed and clear the circuit
    result = await registry._safe_check(probe, critical=True)
    assert result.is_healthy
    assert probe.name not in registry._circuit_open_until
    assert probe.name not in registry._circuit_err_count


@pytest.mark.asyncio
async def test_circuit_breaker_disabled():
    call_count = 0

    class AlwaysFail(NoOpProbe):
        async def check(self):
            nonlocal call_count
            call_count += 1
            return ProbeResult(name=self.name, status=ProbeStatus.UNHEALTHY, error="fail")

    app = FastAPI()
    registry = HealthRegistry(app, circuit_breaker=False, circuit_breaker_threshold=2)
    probe = AlwaysFail(name="bad")
    registry.add(probe)

    for _ in range(5):
        await registry._safe_check(probe, critical=True)

    # Circuit is disabled — probe should have been called every time
    assert call_count == 5
    assert probe.name not in registry._circuit_open_until


@pytest.mark.asyncio
async def test_circuit_per_probe_threshold_override():
    class AlwaysFail(NoOpProbe):
        async def check(self):
            return ProbeResult(name=self.name, status=ProbeStatus.UNHEALTHY, error="fail")

    app = FastAPI()
    registry = HealthRegistry(app, circuit_breaker_threshold=10, circuit_breaker_cooldown_ms=60_000)
    probe = AlwaysFail(name="bad")
    probe.circuit_breaker_threshold = 2  # per-probe override
    registry.add(probe)

    for _ in range(2):
        await registry._safe_check(probe, critical=True)

    # Should have opened after 2, not 10
    assert probe.name in registry._circuit_open_until


# ---------------------------------------------------------------------------
# Webhook on state change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_fired_on_state_change(monkeypatch):
    posted = []

    async def fake_dispatch(self, alert):
        posted.append((alert.probe, alert.old_status.value, alert.new_status.value))

    monkeypatch.setattr("fastapi_watch.registry.HealthRegistry._dispatch_alert", fake_dispatch)

    app = FastAPI()
    registry = HealthRegistry(app, webhook_url="http://hooks.example.com/health")

    # Seed initial state as healthy
    registry._probe_states["svc"] = ProbeStatus.HEALTHY

    # Fire a state change: healthy → unhealthy
    results = [ProbeResult(name="svc", status=ProbeStatus.UNHEALTHY, error="down")]
    await registry._fire_state_changes(results)

    # yield to the event loop so the create_task coroutine runs
    await asyncio.sleep(0)

    assert len(posted) == 1
    assert posted[0] == ("svc", "healthy", "unhealthy")


@pytest.mark.asyncio
async def test_webhook_not_fired_when_status_unchanged(monkeypatch):
    posted = []

    async def fake_dispatch(self, alert):
        posted.append((alert.probe, alert.old_status.value, alert.new_status.value))

    monkeypatch.setattr("fastapi_watch.registry.HealthRegistry._dispatch_alert", fake_dispatch)

    app = FastAPI()
    registry = HealthRegistry(app, webhook_url="http://hooks.example.com/health")

    registry._probe_states["svc"] = ProbeStatus.HEALTHY
    results = [ProbeResult(name="svc", status=ProbeStatus.HEALTHY)]
    await registry._fire_state_changes(results)

    assert posted == []


@pytest.mark.asyncio
async def test_webhook_not_fired_without_url():
    app = FastAPI()
    registry = HealthRegistry(app)  # no webhook_url, no alerters

    registry._probe_states["svc"] = ProbeStatus.HEALTHY
    results = [ProbeResult(name="svc", status=ProbeStatus.UNHEALTHY, error="down")]
    await registry._fire_state_changes(results)

    # No alerters registered when no webhook_url provided
    assert registry._alerters == []


@pytest.mark.asyncio
async def test_webhook_not_fired_on_first_run():
    """First run seeds state — no alerters called until a *subsequent* run shows a change."""
    posted = []

    async def fake_dispatch(self, alert):
        posted.append(alert.probe)

    import fastapi_watch.registry as reg_module
    monkeypatch_attr = reg_module.HealthRegistry._dispatch_alert

    app = FastAPI()
    registry = HealthRegistry(app, webhook_url="http://hooks.example.com/health")
    # _probe_states is empty — old will be None, so no alert dispatched
    results = [ProbeResult(name="svc", status=ProbeStatus.UNHEALTHY, error="down")]
    await registry._fire_state_changes(results)

    # State seeded but no alert because there was no *previous* state to compare
    assert posted == []
    assert registry._probe_states["svc"] == ProbeStatus.UNHEALTHY


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_auth_basic_missing_credentials():
    app = FastAPI()
    HealthRegistry(app, auth={"type": "basic", "username": "admin", "password": "secret"})
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/health/live")
    assert resp.status_code == 401
    assert "WWW-Authenticate" in resp.headers


def test_auth_basic_wrong_credentials():
    app = FastAPI()
    HealthRegistry(app, auth={"type": "basic", "username": "admin", "password": "secret"})
    client = TestClient(app, raise_server_exceptions=False)
    import base64
    creds = base64.b64encode(b"admin:wrong").decode()
    resp = client.get("/health/live", headers={"Authorization": f"Basic {creds}"})
    assert resp.status_code == 401


def test_auth_basic_correct_credentials():
    app = FastAPI()
    HealthRegistry(app, auth={"type": "basic", "username": "admin", "password": "secret"})
    client = TestClient(app, raise_server_exceptions=False)
    import base64
    creds = base64.b64encode(b"admin:secret").decode()
    resp = client.get("/health/live", headers={"Authorization": f"Basic {creds}"})
    assert resp.status_code == 200


def test_auth_apikey_missing():
    app = FastAPI()
    HealthRegistry(app, auth={"type": "apikey", "key": "mykey"})
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/health/live")
    assert resp.status_code == 403


def test_auth_apikey_wrong_key():
    app = FastAPI()
    HealthRegistry(app, auth={"type": "apikey", "key": "mykey"})
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/health/live", headers={"X-API-Key": "wrong"})
    assert resp.status_code == 403


def test_auth_apikey_correct_key():
    app = FastAPI()
    HealthRegistry(app, auth={"type": "apikey", "key": "mykey"})
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/health/live", headers={"X-API-Key": "mykey"})
    assert resp.status_code == 200


def test_auth_apikey_custom_header():
    app = FastAPI()
    HealthRegistry(app, auth={"type": "apikey", "key": "mykey", "header": "X-Token"})
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/health/live", headers={"X-Token": "mykey"})
    assert resp.status_code == 200


def test_auth_no_auth_open_access():
    app = FastAPI()
    HealthRegistry(app)  # no auth
    client = TestClient(app)
    resp = client.get("/health/live")
    assert resp.status_code == 200


def test_auth_custom_callable_allows():
    from fastapi import Request

    def always_allow(request: Request) -> bool:
        return True

    app = FastAPI()
    HealthRegistry(app, auth=always_allow)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/health/live")
    assert resp.status_code == 200


def test_auth_custom_callable_denies():
    from fastapi import Request

    def always_deny(request: Request) -> bool:
        return False

    app = FastAPI()
    HealthRegistry(app, auth=always_deny)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/health/live")
    assert resp.status_code == 403


def test_auth_invalid_type_raises():
    app = FastAPI()
    import pytest
    with pytest.raises(ValueError, match="Unknown auth type"):
        HealthRegistry(app, auth={"type": "oauth2"})


# ---------------------------------------------------------------------------
# Startup probe
# ---------------------------------------------------------------------------


def test_startup_returns_503_before_set_started():
    app = FastAPI()
    HealthRegistry(app)
    client = TestClient(app)
    resp = client.get("/health/startup")
    assert resp.status_code == 503
    assert resp.json()["status"] == "starting"


def test_startup_returns_200_after_set_started():
    app = FastAPI()
    registry = HealthRegistry(app)
    registry.set_started()
    client = TestClient(app)
    resp = client.get("/health/startup")
    assert resp.status_code == 200
    assert resp.json()["status"] == "started"


def test_startup_503_when_startup_probe_fails():
    class FailingProbe(NoOpProbe):
        async def check(self):
            return ProbeResult(name=self.name, status=ProbeStatus.UNHEALTHY, error="not ready")

    app = FastAPI()
    registry = HealthRegistry(app, startup_probes=[FailingProbe(name="db-init")])
    registry.set_started()
    client = TestClient(app)
    resp = client.get("/health/startup")
    assert resp.status_code == 503


def test_startup_200_when_startup_probe_passes():
    app = FastAPI()
    registry = HealthRegistry(app, startup_probes=[NoOpProbe(name="db-init")])
    registry.set_started()
    client = TestClient(app)
    resp = client.get("/health/startup")
    assert resp.status_code == 200


def test_startup_set_started_returns_self():
    app = FastAPI()
    registry = HealthRegistry(app)
    result = registry.set_started()
    assert result is registry


def test_startup_probes_not_in_status():
    """Startup probes must not appear in /health/status."""
    app = FastAPI()
    startup_probe = NoOpProbe(name="db-init")
    registry = HealthRegistry(app, startup_probes=[startup_probe])
    registry.set_started()
    client = TestClient(app)
    resp = client.get("/health/status")
    names = {p["name"] for p in resp.json()["probes"]}
    assert "db-init" not in names
