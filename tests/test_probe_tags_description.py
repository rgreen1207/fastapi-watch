"""Tests for probe tags, descriptions, endpoint tag-filtering, and dashboard rendering."""
import pytest
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi_watch import HealthRegistry, FastAPIRouteProbe, FastAPIWebSocketProbe
from fastapi_watch._dashboard import render_dashboard
from fastapi_watch.models import HealthReport, ProbeResult, ProbeStatus
from fastapi_watch.probes.noop import NoOpProbe
from fastapi_watch.probes.base import PassiveProbe


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_report(probes: list[ProbeResult] | None = None) -> HealthReport:
    if probes is None:
        probes = [ProbeResult(name="mem", status=ProbeStatus.HEALTHY)]
    return HealthReport.from_results(
        probes,
        checked_at=datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        timezone="UTC",
    )


# ---------------------------------------------------------------------------
# ProbeResult — description and tags fields
# ---------------------------------------------------------------------------

def test_probe_result_description_defaults_none():
    r = ProbeResult(name="db", status=ProbeStatus.HEALTHY)
    assert r.description is None


def test_probe_result_tags_defaults_empty():
    r = ProbeResult(name="db", status=ProbeStatus.HEALTHY)
    assert r.tags == []


def test_probe_result_accepts_description():
    r = ProbeResult(name="db", status=ProbeStatus.HEALTHY, description="Primary database")
    assert r.description == "Primary database"


def test_probe_result_accepts_tags():
    r = ProbeResult(name="db", status=ProbeStatus.HEALTHY, tags=["database", "critical"])
    assert r.tags == ["database", "critical"]


# ---------------------------------------------------------------------------
# FastAPIRouteProbe — description and tags
# ---------------------------------------------------------------------------

def test_route_probe_accepts_description():
    probe = FastAPIRouteProbe(name="api", description="User service")
    assert probe.description == "User service"


def test_route_probe_accepts_tags():
    probe = FastAPIRouteProbe(name="api", tags=["users", "v2"])
    assert probe.tags == ["users", "v2"]


def test_route_probe_description_none_by_default():
    probe = FastAPIRouteProbe(name="api")
    assert probe.description is None


def test_route_probe_tags_empty_by_default():
    probe = FastAPIRouteProbe(name="api")
    assert probe.tags == []


# ---------------------------------------------------------------------------
# FastAPIWebSocketProbe — description and tags
# ---------------------------------------------------------------------------

def test_ws_probe_accepts_description():
    probe = FastAPIWebSocketProbe(name="chat", description="Chat service")
    assert probe.description == "Chat service"


def test_ws_probe_accepts_tags():
    probe = FastAPIWebSocketProbe(name="chat", tags=["realtime"])
    assert probe.tags == ["realtime"]


# ---------------------------------------------------------------------------
# PassiveProbe subclasses — description and tags forwarded via **kwargs
# ---------------------------------------------------------------------------

def test_passive_probe_accepts_description():
    from fastapi_watch.probes import HttpProbe
    probe = HttpProbe(name="stripe", description="External API")
    assert probe.description == "External API"


def test_passive_probe_accepts_tags():
    from fastapi_watch.probes import HttpProbe
    probe = HttpProbe(name="stripe", tags=["external"])
    assert probe.tags == ["external"]


# ---------------------------------------------------------------------------
# Registry injects description and tags into ProbeResult
# ---------------------------------------------------------------------------

def test_registry_injects_description_into_result():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    probe = FastAPIRouteProbe(name="checkout", description="Payment processing")

    @app.post("/checkout")
    @probe.watch
    async def checkout():
        return {}

    registry.add(probe)
    client = TestClient(app)
    client.post("/checkout")

    resp = client.get("/health/status")
    probes = {p["name"]: p for p in resp.json()["probes"]}
    assert probes["checkout"]["description"] == "Payment processing"


def test_registry_injects_tags_into_result():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    probe = FastAPIRouteProbe(name="checkout", tags=["payments"])

    @app.post("/checkout")
    @probe.watch
    async def checkout():
        return {}

    registry.add(probe)
    client = TestClient(app)
    client.post("/checkout")

    resp = client.get("/health/status")
    probes = {p["name"]: p for p in resp.json()["probes"]}
    assert "payments" in probes["checkout"]["tags"]


# ---------------------------------------------------------------------------
# Tag filtering — /health/ready?tag=...
# ---------------------------------------------------------------------------

def test_ready_tag_filter_returns_only_tagged_probes():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(NoOpProbe(name="db"), critical=True)
    registry.add(FastAPIRouteProbe(name="api", tags=["http"]), critical=True)

    client = TestClient(app)
    resp = client.get("/health/ready?tag=http")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"


def test_ready_no_tag_returns_all_probes():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(NoOpProbe(name="db"))
    registry.add(FastAPIRouteProbe(name="api", tags=["http"]))

    client = TestClient(app)
    resp = client.get("/health/ready")
    assert resp.status_code == 200


def test_ready_unknown_tag_returns_healthy_with_no_probes():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(NoOpProbe(name="db"))

    client = TestClient(app)
    resp = client.get("/health/ready?tag=nonexistent")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


# ---------------------------------------------------------------------------
# Tag filtering — /health/status?tag=...
# ---------------------------------------------------------------------------

def test_status_tag_filter_returns_only_matching_probes():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(NoOpProbe(name="infra"))
    registry.add(FastAPIRouteProbe(name="api", tags=["http"]))

    client = TestClient(app)
    resp = client.get("/health/status?tag=http")
    names = {p["name"] for p in resp.json()["probes"]}
    assert "api" in names
    assert "infra" not in names


def test_status_no_tag_returns_all_probes():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(NoOpProbe(name="infra"))
    registry.add(FastAPIRouteProbe(name="api", tags=["http"]))

    client = TestClient(app)
    resp = client.get("/health/status")
    names = {p["name"] for p in resp.json()["probes"]}
    assert "infra" in names
    assert "api" in names


def test_status_tag_filter_multiple_probes_with_same_tag():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(FastAPIRouteProbe(name="users", tags=["api"]))
    registry.add(FastAPIRouteProbe(name="orders", tags=["api"]))
    registry.add(NoOpProbe(name="db"))

    client = TestClient(app)
    resp = client.get("/health/status?tag=api")
    names = {p["name"] for p in resp.json()["probes"]}
    assert "users" in names
    assert "orders" in names
    assert "db" not in names


# ---------------------------------------------------------------------------
# Dashboard — description rendering
# ---------------------------------------------------------------------------

def test_dashboard_renders_probe_description():
    report = _make_report([
        ProbeResult(name="checkout", status=ProbeStatus.HEALTHY, description="Payment processing")
    ])
    html = render_dashboard(report, stream_url="/health/status/stream")
    assert "Payment processing" in html


def test_dashboard_no_description_no_subtitle():
    report = _make_report([
        ProbeResult(name="checkout", status=ProbeStatus.HEALTHY)
    ])
    html = render_dashboard(report, stream_url="/health/status/stream")
    # The CSS class is always in the stylesheet; only a rendered div should be absent
    assert '<div class="probe-description">' not in html


def test_dashboard_description_rendered_as_subtitle():
    report = _make_report([
        ProbeResult(name="api", status=ProbeStatus.HEALTHY, description="REST endpoints")
    ])
    html = render_dashboard(report, stream_url="/health/status/stream")
    assert 'class="probe-description"' in html
    assert "REST endpoints" in html


def test_dashboard_description_escaped():
    report = _make_report([
        ProbeResult(name="api", status=ProbeStatus.HEALTHY, description="<b>bold</b>")
    ])
    html = render_dashboard(report, stream_url="/health/status/stream")
    # Raw HTML tags must not appear inside the description div
    assert '<div class="probe-description"><b>bold</b></div>' not in html
    assert "&lt;b&gt;bold&lt;/b&gt;" in html


def test_dashboard_probe_name_group_present_with_description():
    report = _make_report([
        ProbeResult(name="api", status=ProbeStatus.HEALTHY, description="my api")
    ])
    html = render_dashboard(report, stream_url="/health/status/stream")
    assert "probe-name-group" in html
