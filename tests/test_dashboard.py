import pytest
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi_watch import HealthRegistry
from fastapi_watch._dashboard import render_dashboard
from fastapi_watch.models import HealthReport, ProbeResult, ProbeStatus
from fastapi_watch.probes import NoOpProbe


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_report(
    probes: list[ProbeResult] | None = None,
    status: ProbeStatus = ProbeStatus.HEALTHY,
) -> HealthReport:
    if probes is None:
        probes = [ProbeResult(name="mem", status=ProbeStatus.HEALTHY)]
    return HealthReport.from_results(
        probes,
        checked_at=datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        timezone="UTC",
    )


# ---------------------------------------------------------------------------
# render_dashboard — structure
# ---------------------------------------------------------------------------

def test_render_returns_html_document():
    html = render_dashboard(_make_report(), stream_url="/health/status/stream")
    assert html.strip().startswith("<!DOCTYPE html>")
    assert "</html>" in html


def test_render_includes_probe_name():
    report = _make_report([ProbeResult(name="postgres", status=ProbeStatus.HEALTHY)])
    html = render_dashboard(report, stream_url="/health/status/stream")
    assert "postgres" in html


def test_render_includes_all_probe_names():
    probes = [
        ProbeResult(name="db", status=ProbeStatus.HEALTHY),
        ProbeResult(name="cache", status=ProbeStatus.UNHEALTHY, error="timeout"),
    ]
    html = render_dashboard(_make_report(probes), stream_url="/health/status/stream")
    assert "db" in html
    assert "cache" in html


# ---------------------------------------------------------------------------
# render_dashboard — overall status
# ---------------------------------------------------------------------------

def test_render_healthy_report_shows_all_systems_operational():
    report = _make_report([ProbeResult(name="db", status=ProbeStatus.HEALTHY)])
    html = render_dashboard(report, stream_url="/health/status/stream")
    assert "All Systems Operational" in html


def test_render_unhealthy_report_shows_unhealthy():
    probes = [ProbeResult(name="db", status=ProbeStatus.UNHEALTHY, error="down")]
    report = _make_report(probes)
    html = render_dashboard(report, stream_url="/health/status/stream")
    assert "Unhealthy" in html


def test_render_healthy_header_class():
    report = _make_report([ProbeResult(name="db", status=ProbeStatus.HEALTHY)])
    html = render_dashboard(report, stream_url="/health/status/stream")
    assert "header healthy" in html


def test_render_unhealthy_header_class():
    probes = [ProbeResult(name="db", status=ProbeStatus.UNHEALTHY, error="down")]
    report = _make_report(probes)
    html = render_dashboard(report, stream_url="/health/status/stream")
    assert "header unhealthy" in html


# ---------------------------------------------------------------------------
# render_dashboard — probe cards
# ---------------------------------------------------------------------------

def test_render_healthy_probe_card_class():
    probes = [ProbeResult(name="db", status=ProbeStatus.HEALTHY)]
    html = render_dashboard(_make_report(probes), stream_url="/health/status/stream")
    assert 'probe-card healthy' in html


def test_render_unhealthy_probe_card_class():
    probes = [ProbeResult(name="db", status=ProbeStatus.UNHEALTHY, error="timeout")]
    html = render_dashboard(_make_report(probes), stream_url="/health/status/stream")
    assert 'probe-card unhealthy' in html


def test_render_error_message_shown_when_unhealthy():
    probes = [ProbeResult(name="db", status=ProbeStatus.UNHEALTHY, error="Connection refused")]
    html = render_dashboard(_make_report(probes), stream_url="/health/status/stream")
    assert "Connection refused" in html


def test_render_error_hidden_when_healthy():
    probes = [ProbeResult(name="db", status=ProbeStatus.HEALTHY)]
    html = render_dashboard(_make_report(probes), stream_url="/health/status/stream")
    # The error div is present but hidden via style="display:none"
    assert 'style="display:none"' in html


def test_render_optional_badge_for_non_critical_probe():
    probes = [ProbeResult(name="cache", status=ProbeStatus.HEALTHY, critical=False)]
    html = render_dashboard(_make_report(probes), stream_url="/health/status/stream")
    assert "optional" in html


def test_render_no_optional_badge_for_critical_probe():
    probes = [ProbeResult(name="db", status=ProbeStatus.HEALTHY, critical=True)]
    html = render_dashboard(_make_report(probes), stream_url="/health/status/stream")
    # The CSS always contains the class name; check the rendered element is absent
    assert 'badge-optional">optional</span>' not in html


# ---------------------------------------------------------------------------
# render_dashboard — details table
# ---------------------------------------------------------------------------

def test_render_details_keys_present():
    probes = [ProbeResult(
        name="db",
        status=ProbeStatus.HEALTHY,
        details={"version": "16.2", "active_connections": 5},
    )]
    html = render_dashboard(_make_report(probes), stream_url="/health/status/stream")
    assert "Version" in html
    assert "Active Connections" in html


def test_render_details_values_present():
    probes = [ProbeResult(
        name="db",
        status=ProbeStatus.HEALTHY,
        details={"version": "16.2", "active_connections": 5},
    )]
    html = render_dashboard(_make_report(probes), stream_url="/health/status/stream")
    assert "16.2" in html
    assert "5" in html


def test_render_error_rate_formatted_as_percentage():
    probes = [ProbeResult(
        name="api",
        status=ProbeStatus.HEALTHY,
        details={"error_rate": 0.025},
    )]
    html = render_dashboard(_make_report(probes), stream_url="/health/status/stream")
    assert "2.50%" in html


def test_render_ms_field_formatted_with_unit():
    probes = [ProbeResult(
        name="api",
        status=ProbeStatus.HEALTHY,
        details={"avg_rtt_ms": 45.5},
    )]
    html = render_dashboard(_make_report(probes), stream_url="/health/status/stream")
    assert "45.50 ms" in html


def test_render_none_detail_values_skipped():
    probes = [ProbeResult(
        name="api",
        status=ProbeStatus.HEALTHY,
        details={"present": "yes", "missing": None},
    )]
    html = render_dashboard(_make_report(probes), stream_url="/health/status/stream")
    assert "present" in html.lower()
    # None values are not rendered
    assert "missing" not in html.lower()


def test_render_no_details_table_when_details_empty():
    probes = [ProbeResult(name="mem", status=ProbeStatus.HEALTHY, details={})]
    html = render_dashboard(_make_report(probes), stream_url="/health/status/stream")
    # The CSS always contains the class name; check no table element is rendered
    assert '<table class="details-table">' not in html


# ---------------------------------------------------------------------------
# render_dashboard — timestamp and SSE
# ---------------------------------------------------------------------------

def test_render_checked_at_timestamp():
    html = render_dashboard(_make_report(), stream_url="/health/status/stream")
    assert "2024-06-01" in html


def test_render_stream_url_embedded():
    html = render_dashboard(_make_report(), stream_url="/ops/health/status/stream")
    assert "/ops/health/status/stream" in html


def test_render_sse_script_included():
    html = render_dashboard(_make_report(), stream_url="/health/status/stream")
    assert "EventSource" in html


# ---------------------------------------------------------------------------
# render_dashboard — HTML escaping
# ---------------------------------------------------------------------------

def test_render_probe_name_escaped():
    probes = [ProbeResult(name="<script>alert(1)</script>", status=ProbeStatus.HEALTHY)]
    html = render_dashboard(_make_report(probes), stream_url="/health/status/stream")
    # The page always has a <script> tag for the SSE JS; check the payload itself is escaped
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_render_error_message_escaped():
    probes = [ProbeResult(
        name="db",
        status=ProbeStatus.UNHEALTHY,
        error='<img src=x onerror="alert(1)">',
    )]
    html = render_dashboard(_make_report(probes), stream_url="/health/status/stream")
    assert '<img src=x' not in html


# ---------------------------------------------------------------------------
# GET /health/dashboard — HTTP endpoint
# ---------------------------------------------------------------------------

def test_dashboard_endpoint_returns_200():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(NoOpProbe(name="mem"))
    client = TestClient(app)
    resp = client.get("/health/dashboard")
    assert resp.status_code == 200


def test_dashboard_endpoint_content_type_html():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(NoOpProbe(name="mem"))
    client = TestClient(app)
    resp = client.get("/health/dashboard")
    assert "text/html" in resp.headers["content-type"]


def test_dashboard_endpoint_contains_probe_name():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(NoOpProbe(name="my-service"))
    client = TestClient(app)
    resp = client.get("/health/dashboard")
    assert "my-service" in resp.text


def test_dashboard_endpoint_respects_custom_prefix():
    app = FastAPI()
    registry = HealthRegistry(app, prefix="/ops/health", poll_interval_ms=None)
    registry.add(NoOpProbe(name="mem"))
    client = TestClient(app)
    assert client.get("/ops/health/dashboard").status_code == 200
    assert client.get("/health/dashboard").status_code == 404


# ---------------------------------------------------------------------------
# dashboard=False — opt out
# ---------------------------------------------------------------------------

def test_dashboard_false_omits_route():
    app = FastAPI()
    HealthRegistry(app, dashboard=False, poll_interval_ms=None)
    client = TestClient(app)
    assert client.get("/health/dashboard").status_code == 404


def test_dashboard_true_registers_route():
    app = FastAPI()
    HealthRegistry(app, dashboard=True, poll_interval_ms=None)
    client = TestClient(app)
    assert client.get("/health/dashboard").status_code == 200


def test_dashboard_default_is_enabled():
    app = FastAPI()
    HealthRegistry(app, poll_interval_ms=None)
    client = TestClient(app)
    assert client.get("/health/dashboard").status_code == 200


# ---------------------------------------------------------------------------
# probe count summary
# ---------------------------------------------------------------------------

def test_dashboard_shows_probe_count_summary():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(NoOpProbe(name="a"))
    registry.add(NoOpProbe(name="b"))
    client = TestClient(app)
    resp = client.get("/health/dashboard")
    # Should show "2 / 2 probes healthy" or similar
    assert "2" in resp.text
