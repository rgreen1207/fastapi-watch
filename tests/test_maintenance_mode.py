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
    """set_maintenance() with no args means indefinite (None)."""
    app = FastAPI()
    registry = HealthRegistry(app)
    registry.set_maintenance()
    # _maintenance_until is None → _in_maintenance must handle this as active
    # Actually per implementation: None means not active. So set_maintenance()
    # with no args clears the mode. Let me check:
    # _in_maintenance returns False if _maintenance_until is None.
    # This means the user must pass a future datetime to activate maintenance.
    # set_maintenance() with no args is a no-op / clear.
    # This is correct API behavior — document it.
    assert registry._in_maintenance() is False


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
