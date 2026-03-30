"""Tests for the three-state health system (HEALTHY / DEGRADED / UNHEALTHY)."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi_watch import HealthRegistry
from fastapi_watch.models import ProbeResult, ProbeStatus, HealthReport
from fastapi_watch.probes.noop import NoOpProbe


# ---------------------------------------------------------------------------
# ProbeStatus enum
# ---------------------------------------------------------------------------

def test_probe_status_has_degraded():
    assert ProbeStatus.DEGRADED == "degraded"


def test_is_healthy_strict():
    r = ProbeResult(name="x", status=ProbeStatus.DEGRADED)
    assert r.is_healthy is False


def test_is_degraded_property():
    r = ProbeResult(name="x", status=ProbeStatus.DEGRADED)
    assert r.is_degraded is True


def test_is_passing_healthy():
    r = ProbeResult(name="x", status=ProbeStatus.HEALTHY)
    assert r.is_passing is True


def test_is_passing_degraded():
    r = ProbeResult(name="x", status=ProbeStatus.DEGRADED)
    assert r.is_passing is True


def test_is_passing_unhealthy():
    r = ProbeResult(name="x", status=ProbeStatus.UNHEALTHY)
    assert r.is_passing is False


# ---------------------------------------------------------------------------
# HealthReport.from_results overall status
# ---------------------------------------------------------------------------

def test_report_healthy_all_healthy():
    results = [
        ProbeResult(name="a", status=ProbeStatus.HEALTHY),
        ProbeResult(name="b", status=ProbeStatus.HEALTHY),
    ]
    report = HealthReport.from_results(results)
    assert report.status == ProbeStatus.HEALTHY


def test_report_degraded_when_critical_is_degraded():
    results = [
        ProbeResult(name="a", status=ProbeStatus.HEALTHY),
        ProbeResult(name="b", status=ProbeStatus.DEGRADED),
    ]
    report = HealthReport.from_results(results)
    assert report.status == ProbeStatus.DEGRADED


def test_report_unhealthy_takes_priority_over_degraded():
    results = [
        ProbeResult(name="a", status=ProbeStatus.DEGRADED),
        ProbeResult(name="b", status=ProbeStatus.UNHEALTHY),
    ]
    report = HealthReport.from_results(results)
    assert report.status == ProbeStatus.UNHEALTHY


def test_report_healthy_when_degraded_is_non_critical():
    results = [
        ProbeResult(name="a", status=ProbeStatus.HEALTHY, critical=True),
        ProbeResult(name="b", status=ProbeStatus.DEGRADED, critical=False),
    ]
    report = HealthReport.from_results(results)
    assert report.status == ProbeStatus.HEALTHY


def test_report_healthy_when_unhealthy_is_non_critical():
    results = [
        ProbeResult(name="a", status=ProbeStatus.HEALTHY, critical=True),
        ProbeResult(name="b", status=ProbeStatus.UNHEALTHY, critical=False),
    ]
    report = HealthReport.from_results(results)
    assert report.status == ProbeStatus.HEALTHY


# ---------------------------------------------------------------------------
# /health/ready HTTP behavior with DEGRADED
# ---------------------------------------------------------------------------

class DegradedProbe(NoOpProbe):
    async def check(self):
        return ProbeResult(name=self.name, status=ProbeStatus.DEGRADED)


def test_ready_returns_200_when_degraded():
    """DEGRADED critical probe → /ready still 200 (traffic still flows)."""
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(DegradedProbe(name="svc"))
    client = TestClient(app)
    resp = client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "degraded"


def test_ready_returns_503_when_unhealthy():
    class UnhealthyProbe(NoOpProbe):
        async def check(self):
            return ProbeResult(name=self.name, status=ProbeStatus.UNHEALTHY, error="down")

    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(UnhealthyProbe(name="svc"))
    client = TestClient(app)
    resp = client.get("/health/ready")
    assert resp.status_code == 503


def test_status_returns_207_when_degraded():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(DegradedProbe(name="svc"))
    client = TestClient(app)
    resp = client.get("/health/status")
    assert resp.status_code == 207


# ---------------------------------------------------------------------------
# Circuit breaker resets on DEGRADED (is_passing)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_circuit_resets_on_degraded():
    class DegProbe(NoOpProbe):
        async def check(self):
            return ProbeResult(name=self.name, status=ProbeStatus.DEGRADED)

    app = FastAPI()
    registry = HealthRegistry(app, circuit_breaker_threshold=3)
    probe = DegProbe(name="dp")
    registry.add(probe)

    # Seed some failures
    registry._circuit_err_count["dp"] = 2

    # DEGRADED result should reset the error count (is_passing=True)
    await registry._safe_check(probe, critical=True)
    assert "dp" not in registry._circuit_err_count
