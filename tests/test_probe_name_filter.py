"""Tests for ?probe= name filtering on /health/status, /health/ready, and streams."""
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi_watch import HealthRegistry
from fastapi_watch.probes.noop import NoOpProbe


def _make_client():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(NoOpProbe(name="svc-a"))
    registry.add(NoOpProbe(name="svc-b"))
    registry.add(NoOpProbe(name="svc-c"))
    return TestClient(app)


def test_status_probe_filter_single():
    client = _make_client()
    resp = client.get("/health/status?probe=svc-a")
    assert resp.status_code == 200
    names = {p["name"] for p in resp.json()["probes"]}
    assert names == {"svc-a"}


def test_status_probe_filter_multiple():
    client = _make_client()
    resp = client.get("/health/status?probe=svc-a,svc-b")
    names = {p["name"] for p in resp.json()["probes"]}
    assert names == {"svc-a", "svc-b"}
    assert "svc-c" not in names


def test_status_probe_filter_nonexistent_returns_empty():
    client = _make_client()
    resp = client.get("/health/status?probe=nonexistent")
    assert resp.status_code == 200
    assert resp.json()["probes"] == []


def test_status_no_probe_param_returns_all():
    client = _make_client()
    resp = client.get("/health/status")
    names = {p["name"] for p in resp.json()["probes"]}
    assert names == {"svc-a", "svc-b", "svc-c"}


def test_status_probe_filter_spaces_trimmed():
    client = _make_client()
    resp = client.get("/health/status?probe=svc-a, svc-b")
    names = {p["name"] for p in resp.json()["probes"]}
    assert "svc-a" in names
    assert "svc-b" in names


def test_ready_probe_filter_single():
    client = _make_client()
    resp = client.get("/health/ready?probe=svc-a")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"


def test_ready_probe_filter_nonexistent_returns_healthy():
    client = _make_client()
    resp = client.get("/health/ready?probe=nonexistent")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_status_probe_and_tag_filter_and_logic():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    probe_a = NoOpProbe(name="svc-a")
    probe_a.tags = ["http"]
    probe_b = NoOpProbe(name="svc-b")
    probe_b.tags = ["http"]
    registry.add(probe_a)
    registry.add(probe_b)

    client = TestClient(app)
    resp = client.get("/health/status?tag=http&probe=svc-a")
    names = {p["name"] for p in resp.json()["probes"]}
    assert names == {"svc-a"}


def test_stream_probe_filter_single_fetch():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(NoOpProbe(name="svc-a"))
    registry.add(NoOpProbe(name="svc-b"))
    client = TestClient(app)

    resp = client.get("/health/status/stream?probe=svc-a")
    assert resp.status_code == 200
    data_lines = [l for l in resp.text.splitlines() if l.startswith("data:")]
    assert len(data_lines) == 1
    payload = json.loads(data_lines[0][len("data:"):].strip())
    names = {p["name"] for p in payload["probes"]}
    assert names == {"svc-a"}


def test_ready_stream_probe_filter_single_fetch():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(NoOpProbe(name="svc-a"))
    registry.add(NoOpProbe(name="svc-b"))
    client = TestClient(app)

    resp = client.get("/health/ready/stream?probe=svc-a")
    assert resp.status_code == 200
    data_lines = [l for l in resp.text.splitlines() if l.startswith("data:")]
    assert len(data_lines) == 1
    payload = json.loads(data_lines[0][len("data:"):].strip())
    names = {p["name"] for p in payload["probes"]}
    assert names == {"svc-a"}
    assert "svc-b" not in names


def test_probe_filter_excludes_when_tag_does_not_match():
    """?probe=X&tag=Y with X not having tag Y → empty result (AND logic)."""
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    probe_a = NoOpProbe(name="svc-a")
    probe_a.tags = ["http"]
    registry.add(probe_a)
    client = TestClient(app)

    resp = client.get("/health/status?probe=svc-a&tag=database")
    assert resp.status_code == 200
    assert resp.json()["probes"] == []
