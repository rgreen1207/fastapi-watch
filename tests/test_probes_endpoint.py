"""Tests for GET /health/probes — probe introspection endpoint."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi_watch import HealthRegistry
from fastapi_watch.probes.noop import NoOpProbe


def test_probes_endpoint_returns_registered_probes():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    probe = NoOpProbe(name="postgresql")
    probe.tags = ["database"]
    probe.description = "GET /users/{id}"
    probe.poll_interval_ms = 30_000
    registry.add(probe, critical=True)

    client = TestClient(app)
    resp = client.get("/health/probes")
    assert resp.status_code == 200
    data = resp.json()
    assert "probes" in data
    assert len(data["probes"]) == 1
    p = data["probes"][0]
    assert p["name"] == "postgresql"
    assert p["critical"] is True
    assert p["tags"] == ["database"]
    assert p["description"] == "GET /users/{id}"
    assert p["poll_interval_ms"] == 30_000
    assert p["circuit_breaker_enabled"] is True


def test_probes_endpoint_multiple_probes():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(NoOpProbe(name="svc-a"), critical=True)
    registry.add(NoOpProbe(name="svc-b"), critical=False)

    client = TestClient(app)
    resp = client.get("/health/probes")
    assert resp.status_code == 200
    probes = {p["name"]: p for p in resp.json()["probes"]}
    assert "svc-a" in probes
    assert "svc-b" in probes
    assert probes["svc-a"]["critical"] is True
    assert probes["svc-b"]["critical"] is False


def test_probes_endpoint_no_tags_returns_empty_list():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    probe = NoOpProbe(name="bare")
    registry.add(probe)

    client = TestClient(app)
    resp = client.get("/health/probes")
    p = resp.json()["probes"][0]
    assert p["tags"] == []


def test_probes_endpoint_no_description_returns_null():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    probe = NoOpProbe(name="bare")
    registry.add(probe)

    client = TestClient(app)
    resp = client.get("/health/probes")
    p = resp.json()["probes"][0]
    assert p["description"] is None


def test_probes_endpoint_poll_interval_null_when_not_set():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    probe = NoOpProbe(name="bare")
    registry.add(probe)

    client = TestClient(app)
    resp = client.get("/health/probes")
    p = resp.json()["probes"][0]
    assert p["poll_interval_ms"] is None


def test_probes_endpoint_empty_registry():
    app = FastAPI()
    HealthRegistry(app, poll_interval_ms=None)

    client = TestClient(app)
    resp = client.get("/health/probes")
    assert resp.status_code == 200
    assert resp.json()["probes"] == []


def test_probes_endpoint_does_not_run_probes():
    run_count = 0

    class CountingProbe(NoOpProbe):
        async def check(self):
            nonlocal run_count
            run_count += 1
            return await super().check()

    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(CountingProbe(name="counting"))

    client = TestClient(app)
    client.get("/health/probes")
    assert run_count == 0


def test_probes_endpoint_circuit_breaker_enabled_field():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    probe = NoOpProbe(name="svc")
    probe.circuit_breaker_enabled = False
    registry.add(probe)

    client = TestClient(app)
    resp = client.get("/health/probes")
    p = resp.json()["probes"][0]
    assert p["circuit_breaker_enabled"] is False


def test_probes_endpoint_requires_auth_when_configured():
    app = FastAPI()
    HealthRegistry(app, poll_interval_ms=None, auth={"type": "apikey", "key": "secret"})
    client = TestClient(app)

    # No credentials → 403
    resp = client.get("/health/probes")
    assert resp.status_code == 403

    # Wrong credentials → 403
    resp = client.get("/health/probes", headers={"X-API-Key": "wrong"})
    assert resp.status_code == 403

    # Correct credentials → 200
    resp = client.get("/health/probes", headers={"X-API-Key": "secret"})
    assert resp.status_code == 200
