import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi_watch import HealthRegistry
from fastapi_watch.probes.memory import MemoryProbe
from fastapi_watch.models import ProbeResult, ProbeStatus


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


def test_registry_chaining():
    app = FastAPI()
    registry = HealthRegistry(app)
    result = registry.add(MemoryProbe(name="a")).add(MemoryProbe(name="b"))
    assert result is registry
    assert len(registry._probes) == 2


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
