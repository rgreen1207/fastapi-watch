"""``view=dashboard`` slims the SSE stream to what the built-in dashboard reads (#29).

Without it every event carries each probe's full details, including every
route RequestMetricsProbe tracks; consumers that don't pass ``view`` keep
getting exactly that.
"""
import json
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi_watch import HealthRegistry
from fastapi_watch.models import ProbeResult, ProbeStatus
from fastapi_watch.probes.base import BaseProbe

_ROUTE_COUNT = 150
_CHARTED = {"request_count", "p95_rtt_ms", "error_rate", "requests_per_minute"}
_APIKEY = {"type": "apikey", "key": "s3cret-key"}
_HEADER = {"X-API-Key": "s3cret-key"}


def _route_stats(i: int) -> dict:
    return {
        "request_count": i,
        "requests_per_minute": i / 10,
        "error_count": i % 3,
        "error_rate": (i % 3) / max(i, 1),
        "avg_rtt_ms": 2.5,
        "p50_rtt_ms": 2.0,
        "p95_rtt_ms": 9.0 + i,
        "p99_rtt_ms": 12.0 + i,
        "min_rtt_ms": 0.5,
        "max_rtt_ms": 40.0,
        "status_codes": {"200": i, "500": i % 3},
        "last_error": None,
    }


_DETAILS = {
    "request_count": sum(range(_ROUTE_COUNT)),
    "p95_rtt_ms": 30.0,
    "error_rate": 0.01,
    "routes": {f"/api/resource-{i:03d}/{{item_id}}": _route_stats(i) for i in range(_ROUTE_COUNT)},
}


class _ManyRoutes(BaseProbe):
    name = "requests"

    async def check(self) -> ProbeResult:
        return ProbeResult(name=self.name, status=ProbeStatus.HEALTHY, details=_DETAILS)


def _app(**kwargs) -> FastAPI:
    app = FastAPI()
    # Single-fetch: each stream sends one event and ends.
    # No circuit breaker: its details entry would be added to the probe's own.
    HealthRegistry(app, poll_interval_ms=None, circuit_breaker=False, **kwargs).add(_ManyRoutes())
    return app


def _event(resp) -> tuple[str, dict]:
    assert resp.status_code == 200
    line = next(line for line in resp.text.splitlines() if line.startswith("data: "))
    raw = line[len("data: "):]
    return raw, json.loads(raw)


@pytest.mark.parametrize("path", ["/health/status/stream", "/health/ready/stream"])
def test_dashboard_view_bounds_the_payload(path):
    client = TestClient(_app())
    full_raw, _ = _event(client.get(path))
    slim_raw, slim = _event(client.get(f"{path}?view=dashboard"))

    details = slim["probes"][0]["details"]
    assert len(details["routes"]) == 8
    top = sorted(_DETAILS["routes"], key=lambda p: -_DETAILS["routes"][p]["request_count"])[:8]
    assert list(details["routes"]) == top  # the busiest, as the page charts them
    assert all(set(stats) == _CHARTED for stats in details["routes"].values())
    # Everything else is kept for the "All fields" table.
    assert {k: v for k, v in details.items() if k != "routes"} == {
        k: v for k, v in _DETAILS.items() if k != "routes"
    }
    assert len(slim_raw) < 3_000
    assert len(full_raw) > 10 * len(slim_raw)


def test_without_view_the_payload_is_unchanged():
    _, data = _event(TestClient(_app()).get("/health/status/stream"))
    assert set(data) == {"status", "checked_at", "timezone", "probes", "failing"}
    assert data["probes"][0]["details"] == _DETAILS  # every route, every field


def test_unknown_view_is_rejected():
    assert TestClient(_app()).get("/health/status/stream?view=full").status_code == 422


def _stream_url(html: str) -> str:
    return re.search(r'data-stream-url="([^"]+)"', html).group(1).replace("&amp;", "&")


def test_dashboard_stream_url_requests_the_dashboard_view():
    client = TestClient(_app())
    url = _stream_url(client.get("/health/dashboard").text)
    assert url == "/health/status/stream?view=dashboard"
    _, data = _event(client.get(url))
    assert len(data["probes"][0]["details"]["routes"]) == 8


def test_dashboard_stream_url_with_token_keeps_both_parameters():
    client = TestClient(_app(auth=_APIKEY))
    url = _stream_url(client.get("/health/dashboard", headers=_HEADER).text)
    path, _, query = url.partition("?")
    params = dict(part.split("=", 1) for part in query.split("&"))
    assert path == "/health/status/stream"
    assert params["view"] == "dashboard" and params["token"]
    _, data = _event(client.get(url))  # no header, as from EventSource
    assert len(data["probes"][0]["details"]["routes"]) == 8


class _NaNLatency(BaseProbe):
    name = "nan"

    async def check(self) -> ProbeResult:
        return ProbeResult(name=self.name, status=ProbeStatus.HEALTHY, latency_ms=float("nan"),
                           details={"error_rate": float("nan")})


def test_dashboard_view_events_stay_valid_json_with_non_finite_numbers():
    # The page's JSON.parse rejects NaN; an event it cannot parse is dropped,
    # so one probe reporting NaN froze every card and chart on the page.
    app = FastAPI()
    HealthRegistry(app, poll_interval_ms=None, circuit_breaker=False).add(_NaNLatency())
    raw, _ = _event(TestClient(app).get("/health/status/stream?view=dashboard"))

    def reject(constant):
        raise ValueError(constant)

    probe = json.loads(raw, parse_constant=reject)["probes"][0]
    assert probe["latency_ms"] is None and probe["details"]["error_rate"] is None
