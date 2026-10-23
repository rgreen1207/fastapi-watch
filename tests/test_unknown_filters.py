"""A probe or tag filter that selects no registered probe is refused with 404.

A report with no probes in it counted as healthy, so ``/health/ready?probe=``
with a mistyped name (a Kubernetes readiness URL, say) passed every check,
outage or not.  The unknown names are remembered, bounded, for the dashboard.
"""
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi_watch import HealthRegistry, registry as registry_module
from fastapi_watch.models import ProbeResult, ProbeStatus
from fastapi_watch.probes import NoOpProbe
from fastapi_watch.probes.base import BaseProbe


def _client(**kwargs):
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None, **kwargs)
    db = NoOpProbe(name="postgres")
    db.tags = ["db"]
    registry.add(db).add(NoOpProbe(name="cache"))
    return registry, TestClient(app)


@pytest.mark.parametrize("path", ["/health/ready", "/health/status", "/health/ready/stream", "/health/status/stream"])
def test_unknown_probe_is_404(path):
    _, client = _client()
    response = client.get(f"{path}?probe=postgress")
    assert response.status_code == 404
    assert response.json() == {"error": "unknown probe", "unknown_probes": ["postgress"], "unknown_tags": []}


def test_one_unknown_name_among_known_ones_is_404():
    _, client = _client()
    response = client.get("/health/ready?probe=postgres,cahce")
    assert response.status_code == 404
    assert response.json()["unknown_probes"] == ["cahce"]


def test_unknown_tag_is_404():
    _, client = _client()
    response = client.get("/health/ready?tag=dbs")
    assert response.status_code == 404
    assert response.json() == {"error": "unknown tag", "unknown_probes": [], "unknown_tags": ["dbs"]}


def test_unknown_probe_and_tag():
    _, client = _client()
    body = client.get("/health/ready?probe=x&tag=y").json()
    assert body["error"] == "unknown probe and unknown tag"


def test_known_filters_selecting_nothing_together_is_404():
    _, client = _client()
    response = client.get("/health/ready?probe=cache&tag=db")
    assert response.status_code == 404
    assert response.json()["error"] == "no probe matches every filter"


def test_known_filters_still_work():
    _, client = _client()
    assert client.get("/health/ready?probe=postgres").status_code == 200
    assert client.get("/health/ready?tag=db").status_code == 200
    assert client.get("/health/ready").status_code == 200


def test_404_even_in_maintenance_and_grace():
    registry, client = _client(grace_period_ms=60_000)
    registry.set_maintenance()
    assert client.get("/health/ready?probe=postgress").status_code == 404


def test_unknown_names_are_remembered_most_recent_first():
    registry, client = _client()
    client.get("/health/ready?probe=postgress")
    client.get("/health/status?tag=dbs")
    client.get("/health/ready?probe=postgress")
    seen = registry._recent_unknown_requests()
    assert [(e["kind"], e["name"], e["endpoint"], e["count"]) for e in seen] == [
        ("probe", "postgress", "ready", 2),
        ("tag", "dbs", "status", 1),
    ]


def test_remembered_names_are_bounded():
    registry, client = _client()
    for i in range(registry_module._MAX_UNKNOWN_REQUESTS + 15):
        client.get(f"/health/ready?probe=typo{i}")
    client.get("/health/ready?probe=" + "x" * 5000)
    seen = registry._recent_unknown_requests()
    assert len(seen) == registry_module._MAX_UNKNOWN_REQUESTS
    assert len(seen[0]["name"]) == registry_module._MAX_UNKNOWN_NAME_LEN


def test_remembered_names_expire():
    registry, client = _client()
    client.get("/health/ready?probe=postgress")
    entry = next(iter(registry._unknown_requests.values()))
    entry["last_requested_at"] -= timedelta(seconds=registry_module._UNKNOWN_REQUEST_TTL_S + 1)
    assert registry._recent_unknown_requests() == []
    assert registry._unknown_requests == {}


def test_dashboard_lists_unknown_names_without_status():
    registry, client = _client()
    client.get("/health/ready?probe=postgress")
    page = client.get("/health/dashboard").text
    section = page[page.index('id="unregistered"'):]
    section = section[:section.index("</section>")]
    assert " hidden" not in section.split(">")[0]
    assert "postgress" in section and "ready" in section
    assert "Healthy" not in section and "Unhealthy" not in section


def test_dashboard_hides_the_section_when_nothing_unknown_was_asked_for():
    _, client = _client()
    page = client.get("/health/dashboard").text
    assert '<section class="unregistered" id="unregistered" hidden>' in page


def test_unknown_names_are_escaped_on_the_dashboard():
    _, client = _client()
    client.get("/health/ready", params={"tag": '<img src=x onerror=alert(1)>"'})
    page = client.get("/health/dashboard").text
    assert "<img src=x" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;&quot;" in page


def test_dashboard_stream_carries_unknown_names():
    import json

    _, client = _client()
    client.get("/health/ready?probe=postgress")
    with client.stream("GET", "/health/status/stream?view=dashboard") as stream:
        event = next(line for line in stream.iter_lines() if line.startswith("data: "))
    entries = json.loads(event[len("data: "):])["unregistered"]
    assert [(e["name"], e["kind"], e["endpoint"], e["count"]) for e in entries] == [("postgress", "probe", "ready", 1)]
    assert isinstance(entries[0]["last_requested_at"], str)


class _OwnTags(BaseProbe):
    """Registered with tag "db", but reports tags of its own."""

    name = "postgres"
    tags = ["db"]

    async def check(self) -> ProbeResult:
        return ProbeResult(name=self.name, status=ProbeStatus.UNHEALTHY, tags=["primary"])


@pytest.mark.parametrize("path", ["/health/ready", "/health/status"])
def test_registered_tag_that_selects_no_result_is_404(path):
    # Results are filtered by the tags they report, so the tag the probe was
    # registered with passed the unknown-name check and selected nothing: an
    # empty "healthy" report (200) while the probe was failing.
    app = FastAPI()
    HealthRegistry(app, poll_interval_ms=None).add(_OwnTags())
    client = TestClient(app)
    response = client.get(f"{path}?tag=db")
    assert response.status_code == 404
    assert response.json() == {"error": "no probe matches every filter", "unknown_probes": [], "unknown_tags": []}
    assert client.get(f"{path}?tag=primary").json()["status"] == "unhealthy"


@pytest.mark.parametrize("path", ["/health/ready/stream", "/health/status/stream"])
@pytest.mark.parametrize("poll_interval_ms", [None, 60_000])
def test_stream_whose_filter_selects_no_result_reports_an_error_not_healthy(path, poll_interval_ms):
    app = FastAPI()
    HealthRegistry(app, poll_interval_ms=poll_interval_ms).add(_OwnTags())
    with TestClient(app).stream("GET", f"{path}?tag=db") as stream:
        body = "".join(stream.iter_text())  # the stream ends after the error
    assert "event: error\n" in body
    assert '"error":"no probe matches every filter"' in body
    assert "healthy" not in body


@pytest.mark.parametrize("path", ["/health/ready", "/health/status"])
def test_tag_only_a_result_reports_is_known_on_a_fresh_pod(path):
    """Before the probe had run once, its own tag was "unknown": 404 forever in single-fetch mode."""
    app = FastAPI()
    HealthRegistry(app, poll_interval_ms=None).add(_OwnTags())
    response = TestClient(app).get(f"{path}?tag=primary")
    assert response.status_code in (207, 503)
    assert response.json()["status"] == "unhealthy"


def test_unknown_tag_does_not_rerun_probes_that_already_reported():
    calls = []

    class _Counted(BaseProbe):
        name = "counted"

        async def check(self) -> ProbeResult:
            calls.append(1)
            return ProbeResult(name=self.name, status=ProbeStatus.HEALTHY)

    app = FastAPI()
    HealthRegistry(app, poll_interval_ms=60_000).add(_Counted())
    client = TestClient(app)
    client.get("/health/ready")
    for _ in range(3):
        assert client.get("/health/ready?tag=typo").status_code == 404
    assert len(calls) == 1
