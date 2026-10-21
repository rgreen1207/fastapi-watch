"""Tests for maintenance mode."""
import pytest
from datetime import datetime, timedelta
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi_watch import HealthRegistry
from fastapi_watch.probes.noop import NoOpProbe
from fastapi_watch.models import ProbeResult, ProbeStatus


def _future(seconds: int = 3600):
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("UTC")) + timedelta(seconds=seconds)


def _past(seconds: int = 3600):
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("UTC")) - timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# set_maintenance / clear_maintenance
# ---------------------------------------------------------------------------

def test_not_in_maintenance_by_default():
    app = FastAPI()
    registry = HealthRegistry(app)
    assert registry._in_maintenance() is False


def test_in_maintenance_with_future_until():
    app = FastAPI()
    registry = HealthRegistry(app)
    registry.set_maintenance(until=_future())
    assert registry._in_maintenance() is True


def test_not_in_maintenance_with_past_until():
    app = FastAPI()
    registry = HealthRegistry(app)
    registry.set_maintenance(until=_past())
    assert registry._in_maintenance() is False


def test_clear_maintenance_exits_mode():
    app = FastAPI()
    registry = HealthRegistry(app)
    registry.set_maintenance(until=_future())
    registry.clear_maintenance()
    assert registry._in_maintenance() is False


def test_set_maintenance_returns_self():
    app = FastAPI()
    registry = HealthRegistry(app)
    assert registry.set_maintenance() is registry


def test_clear_maintenance_returns_self():
    app = FastAPI()
    registry = HealthRegistry(app)
    assert registry.clear_maintenance() is registry


def test_set_maintenance_indefinite_until_none():
    """set_maintenance() with no args activates indefinite maintenance mode."""
    app = FastAPI()
    registry = HealthRegistry(app)
    registry.set_maintenance()
    assert registry._in_maintenance() is True


# ---------------------------------------------------------------------------
# /health/ready during maintenance
# ---------------------------------------------------------------------------

def test_ready_returns_200_maintenance_during_maintenance():
    class UnhealthyProbe(NoOpProbe):
        async def check(self):
            return ProbeResult(name=self.name, status=ProbeStatus.UNHEALTHY, error="down")

    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(UnhealthyProbe(name="db"))
    registry.set_maintenance(until=_future())
    client = TestClient(app)

    resp = client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "maintenance"


def test_ready_resumes_normal_after_maintenance_cleared():
    class UnhealthyProbe(NoOpProbe):
        async def check(self):
            return ProbeResult(name=self.name, status=ProbeStatus.UNHEALTHY, error="down")

    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(UnhealthyProbe(name="db"))
    registry.set_maintenance(until=_future())
    registry.clear_maintenance()

    client = TestClient(app)
    resp = client.get("/health/ready")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Webhook suppressed during maintenance
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_webhook_suppressed_during_maintenance(monkeypatch):
    posted = []

    async def fake_dispatch(self, alert):
        posted.append(alert.probe)

    monkeypatch.setattr("fastapi_watch.registry.HealthRegistry._dispatch_alert", fake_dispatch)

    app = FastAPI()
    registry = HealthRegistry(app, webhook_url="http://hooks.example.com/health")
    registry.set_maintenance(until=_future())

    registry._probe_states["svc"] = ProbeStatus.HEALTHY
    results = [ProbeResult(name="svc", status=ProbeStatus.UNHEALTHY, error="down")]
    await registry._fire_state_changes(results)
    import asyncio
    await asyncio.sleep(0)

    assert posted == []


@pytest.mark.asyncio
async def test_webhook_fires_after_maintenance_ends(monkeypatch):
    posted = []

    async def fake_dispatch(self, alert):
        posted.append(alert.probe)

    monkeypatch.setattr("fastapi_watch.registry.HealthRegistry._dispatch_alert", fake_dispatch)

    app = FastAPI()
    registry = HealthRegistry(app, webhook_url="http://hooks.example.com/health")
    registry.clear_maintenance()

    registry._probe_states["svc"] = ProbeStatus.HEALTHY
    results = [ProbeResult(name="svc", status=ProbeStatus.UNHEALTHY, error="down")]
    await registry._fire_state_changes(results)
    import asyncio
    await asyncio.sleep(0)

    assert "svc" in posted


# ---------------------------------------------------------------------------
# Dashboard shows maintenance banner
# ---------------------------------------------------------------------------

def test_dashboard_shows_maintenance_banner():
    from fastapi_watch.dashboard import render_dashboard
    from fastapi_watch.models import HealthReport

    report = HealthReport.from_results([])
    html = render_dashboard(report, stream_url="/health/status/stream", maintenance_banner=True)
    assert "maintenance" in html.lower()
    assert "maintenance-banner" in html


def test_dashboard_no_banner_when_not_in_maintenance():
    from fastapi_watch.dashboard import render_dashboard
    from fastapi_watch.models import HealthReport

    report = HealthReport.from_results([])
    html = render_dashboard(report, stream_url="/health/status/stream", maintenance_banner=False)
    # The CSS contains .maintenance-banner but the <div> should not be present
    assert '<div class="maintenance-banner">' not in html


# ---------------------------------------------------------------------------
# Write endpoints require auth; naive and past "until" values
# ---------------------------------------------------------------------------

_AUTH = {"type": "apikey", "key": "k"}
_KEY = {"X-API-Key": "k"}


def _naive_future(hours: int = 1) -> datetime:
    """A naive wall-clock time in the registry's default timezone (UTC)."""
    return _future(hours * 3600).replace(tzinfo=None)


def test_write_endpoints_not_mounted_without_auth():
    app = FastAPI()
    registry = HealthRegistry(app)
    client = TestClient(app)

    assert client.post("/health/maintenance", json={}).status_code == 405
    assert client.delete("/health/maintenance").status_code == 405
    assert client.get("/health/maintenance").status_code == 200
    assert registry._in_maintenance() is False


def test_write_endpoints_mounted_and_guarded_with_auth():
    app = FastAPI()
    HealthRegistry(app, auth=_AUTH)
    client = TestClient(app)

    assert client.post("/health/maintenance", json={}).status_code == 403
    assert client.post("/health/maintenance", json={}, headers=_KEY).status_code == 200
    assert client.delete("/health/maintenance", headers=_KEY).json() == {"active": False, "until": None}


def test_programmatic_maintenance_still_works_without_auth():
    app = FastAPI()
    registry = HealthRegistry(app)
    registry.set_maintenance()
    assert TestClient(app).get("/health/ready").json() == {"status": "maintenance"}


def test_public_access_warning_logged_without_a_logger(caplog):
    with caplog.at_level("WARNING", logger="fastapi_watch"):
        HealthRegistry(FastAPI())
    assert any("POST/DELETE /health/maintenance are not mounted" in r.message for r in caplog.records)


@pytest.mark.parametrize("kwargs", [{"auth": _AUTH}, {"serve_routes": False}])
def test_no_public_access_warning_when_not_exposed(caplog, kwargs):
    with caplog.at_level("WARNING", logger="fastapi_watch"):
        HealthRegistry(FastAPI(), **kwargs)
    assert not [r for r in caplog.records if "publicly accessible" in r.message]


def test_naive_until_via_set_maintenance_does_not_break_ready():
    app = FastAPI()
    registry = HealthRegistry(app)
    registry.set_maintenance(until=_naive_future())

    resp = TestClient(app).get("/health/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "maintenance"}


def test_naive_until_is_read_in_registry_timezone():
    from zoneinfo import ZoneInfo
    registry = HealthRegistry(FastAPI(), timezone="America/Denver", serve_routes=False)
    registry.set_maintenance(until=datetime(2099, 1, 1, 12, 0))

    assert registry._maintenance_until == datetime(2099, 1, 1, 12, 0, tzinfo=ZoneInfo("America/Denver"))


def test_aware_until_is_left_unchanged():
    registry = HealthRegistry(FastAPI(), timezone="America/Denver", serve_routes=False)
    until = _future()
    registry.set_maintenance(until=until)
    assert registry._maintenance_until is until


def test_naive_until_via_post_does_not_break_ready():
    app = FastAPI()
    HealthRegistry(app, auth=_AUTH)
    client = TestClient(app)
    naive = _naive_future().isoformat()

    post = client.post("/health/maintenance", json={"until": naive}, headers=_KEY)
    assert post.status_code == 200
    assert post.json()["until"].endswith("+00:00")
    assert client.get("/health/ready", headers=_KEY).json() == {"status": "maintenance"}


def test_past_until_via_post_rejected_without_ending_current_window():
    app = FastAPI()
    registry = HealthRegistry(app, auth=_AUTH)
    registry.set_maintenance()  # indefinite window already running
    client = TestClient(app)

    resp = client.post("/health/maintenance", json={"until": _past().isoformat()}, headers=_KEY)
    assert resp.status_code == 422
    assert registry._in_maintenance() is True
    assert registry._maintenance_until is None
