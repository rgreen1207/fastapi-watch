import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi_watch import HealthRegistry
from fastapi_watch.models import HealthReport, ProbeResult, ProbeStatus
from fastapi_watch.probes.noop import NoOpProbe


class FailingProbe(NoOpProbe):
    async def check(self):
        return ProbeResult(name=self.name, status=ProbeStatus.UNHEALTHY, error="timeout")


class DegradedProbe(NoOpProbe):
    async def check(self):
        return ProbeResult(name=self.name, status=ProbeStatus.DEGRADED)


def test_failing_empty_when_all_healthy():
    results = [
        ProbeResult(name="a", status=ProbeStatus.HEALTHY, critical=True),
        ProbeResult(name="b", status=ProbeStatus.HEALTHY, critical=True),
    ]
    report = HealthReport.from_results(results)
    assert report.failing == []


def test_failing_lists_unhealthy_critical_probes():
    results = [
        ProbeResult(name="db", status=ProbeStatus.UNHEALTHY, critical=True),
        ProbeResult(name="cache", status=ProbeStatus.HEALTHY, critical=True),
    ]
    report = HealthReport.from_results(results)
    assert report.failing == ["db"]


def test_failing_excludes_non_critical():
    results = [
        ProbeResult(name="optional", status=ProbeStatus.UNHEALTHY, critical=False),
        ProbeResult(name="main", status=ProbeStatus.HEALTHY, critical=True),
    ]
    report = HealthReport.from_results(results)
    assert report.failing == []


def test_failing_excludes_degraded():
    results = [
        ProbeResult(name="db", status=ProbeStatus.DEGRADED, critical=True),
    ]
    report = HealthReport.from_results(results)
    assert report.failing == []


def test_failing_multiple_unhealthy_probes():
    results = [
        ProbeResult(name="db", status=ProbeStatus.UNHEALTHY, critical=True),
        ProbeResult(name="cache", status=ProbeStatus.UNHEALTHY, critical=True),
        ProbeResult(name="queue", status=ProbeStatus.HEALTHY, critical=True),
    ]
    report = HealthReport.from_results(results)
    assert set(report.failing) == {"db", "cache"}


def test_failing_in_ready_response():
    app = FastAPI()
    registry = HealthRegistry(app)
    registry.add(FailingProbe(name="db"))
    client = TestClient(app)
    resp = client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["failing"] == ["db"]


def test_failing_empty_in_ready_response_when_healthy():
    app = FastAPI()
    registry = HealthRegistry(app)
    registry.add(NoOpProbe(name="db"))
    client = TestClient(app)
    resp = client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["failing"] == []


def test_failing_in_status_response():
    app = FastAPI()
    registry = HealthRegistry(app)
    registry.add(FailingProbe(name="redis"))
    client = TestClient(app)
    resp = client.get("/health/status")
    assert resp.status_code == 207
    assert resp.json()["failing"] == ["redis"]


def test_failing_excludes_non_critical_in_response():
    app = FastAPI()
    registry = HealthRegistry(app)
    registry.add(FailingProbe(name="optional"), critical=False)
    registry.add(NoOpProbe(name="main"))
    client = TestClient(app)
    resp = client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["failing"] == []
