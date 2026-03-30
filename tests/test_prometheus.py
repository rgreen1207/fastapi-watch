import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi_watch import HealthRegistry
from fastapi_watch.probes.noop import NoOpProbe
from fastapi_watch.models import ProbeResult, ProbeStatus
from fastapi_watch.prometheus import render_prometheus


# ---------------------------------------------------------------------------
# Unit tests for the renderer
# ---------------------------------------------------------------------------

def _results(*pairs):
    return [ProbeResult(name=n, status=s) for n, s in pairs]


def test_render_prometheus_healthy_gauge():
    text = render_prometheus(_results(("db", ProbeStatus.HEALTHY)))
    assert 'probe_healthy{name="db",critical="true"} 1' in text


def test_render_prometheus_unhealthy_gauge():
    text = render_prometheus(_results(("db", ProbeStatus.UNHEALTHY)))
    assert 'probe_healthy{name="db",critical="true"} 0' in text


def test_render_prometheus_degraded_gauge():
    text = render_prometheus(_results(("db", ProbeStatus.DEGRADED)))
    assert 'probe_degraded{name="db",critical="true"} 1' in text
    assert 'probe_healthy{name="db",critical="true"} 0' in text


def test_render_prometheus_latency():
    r = ProbeResult(name="svc", status=ProbeStatus.HEALTHY, latency_ms=12.5)
    text = render_prometheus([r])
    assert 'probe_latency_ms{name="svc",critical="true"} 12.5' in text


def test_render_prometheus_circuit_open():
    r = ProbeResult(
        name="svc",
        status=ProbeStatus.UNHEALTHY,
        details={"circuit_breaker": {"open": True, "consecutive_failures": 3}},
    )
    text = render_prometheus([r])
    assert 'probe_circuit_open{name="svc",critical="true"} 1' in text


def test_render_prometheus_circuit_closed():
    r = ProbeResult(
        name="svc",
        status=ProbeStatus.HEALTHY,
        details={"circuit_breaker": {"open": False}},
    )
    text = render_prometheus([r])
    assert 'probe_circuit_open{name="svc",critical="true"} 0' in text


def test_render_prometheus_trips_counter():
    r = ProbeResult(name="svc", status=ProbeStatus.HEALTHY)
    text = render_prometheus([r], trips={"svc": 7})
    assert 'probe_circuit_trips_total{name="svc",critical="true"} 7' in text


def test_render_prometheus_non_critical_label():
    r = ProbeResult(name="cache", status=ProbeStatus.HEALTHY, critical=False)
    text = render_prometheus([r])
    assert 'critical="false"' in text


def test_render_prometheus_help_and_type_lines():
    text = render_prometheus(_results(("x", ProbeStatus.HEALTHY)))
    assert "# HELP probe_healthy" in text
    assert "# TYPE probe_healthy gauge" in text
    assert "# TYPE probe_circuit_trips_total counter" in text


def test_render_prometheus_trailing_newline():
    text = render_prometheus(_results(("x", ProbeStatus.HEALTHY)))
    assert text.endswith("\n")


# ---------------------------------------------------------------------------
# Integration: /health/metrics endpoint
# ---------------------------------------------------------------------------

def test_metrics_endpoint_returns_prometheus_format():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(NoOpProbe(name="mem"))
    client = TestClient(app)

    resp = client.get("/health/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert "probe_healthy" in resp.text
    assert 'name="mem"' in resp.text


def test_metrics_endpoint_in_endpoint_list():
    app = FastAPI()
    HealthRegistry(app)
    client = TestClient(app)
    # Endpoint must be reachable (not 404)
    resp = client.get("/health/metrics")
    assert resp.status_code == 200
