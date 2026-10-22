import math
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi_watch import HealthRegistry
from fastapi_watch.probes.base import BaseProbe
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


# ---------------------------------------------------------------------------
# metrics_prefix
# ---------------------------------------------------------------------------

def test_default_metric_names_are_unchanged():
    app = FastAPI()
    HealthRegistry(app, poll_interval_ms=None).add(NoOpProbe(name="db"))
    body = TestClient(app).get("/health/metrics").text
    assert 'probe_healthy{name="db",critical="true"} 1' in body
    assert "# TYPE probe_circuit_trips_total counter" in body


def test_metrics_prefix_namespaces_every_metric():
    app = FastAPI()
    HealthRegistry(app, poll_interval_ms=None, metrics_prefix="fastapi_watch_").add(NoOpProbe(name="db"))
    body = TestClient(app).get("/health/metrics").text
    metric_lines = [line for line in body.splitlines() if line and not line.startswith("#")]
    assert metric_lines and all(line.startswith("fastapi_watch_probe_") for line in metric_lines)
    assert "# HELP fastapi_watch_probe_healthy " in body
    assert "\nprobe_healthy" not in body


@pytest.mark.parametrize("bad", ["1abc", "has-dash", "sp ace", "dot."])
def test_invalid_metrics_prefix_rejected(bad):
    with pytest.raises(ValueError, match="metrics_prefix"):
        HealthRegistry(FastAPI(), metrics_prefix=bad)


# ---------------------------------------------------------------------------
# Odd probe output: one probe must not break the whole scrape
# ---------------------------------------------------------------------------

# A sample line as Prometheus parses it: the value is a float or NaN/±Inf.
_SAMPLE_RE = re.compile(r'^[a-zA-Z_:][a-zA-Z0-9_:]*\{[^}]*\} (NaN|[+-]Inf|-?\d+(\.\d+)?([eE][+-]?\d+)?)$')


def _assert_valid_exposition(text: str) -> None:
    for line in text.splitlines():
        if line and not line.startswith("#"):
            assert _SAMPLE_RE.match(line), line


class _DetailsProbe(BaseProbe):
    def __init__(self, name: str, details: dict) -> None:
        self.name = name
        self._details = details

    async def check(self) -> ProbeResult:
        return ProbeResult(name=self.name, status=ProbeStatus.HEALTHY, latency_ms=1.0, details=self._details)


@pytest.mark.parametrize("circuit_breaker", ["open", None, [1, 2], 3])
def test_metrics_endpoint_survives_a_non_dict_circuit_breaker_detail(circuit_breaker):
    # With the breaker off the registry leaves details alone, so a custom
    # probe's own "circuit_breaker" value reached the exporter: a string 500'd
    # every scrape, hiding every other probe's metrics.
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None, circuit_breaker=False)
    registry.add(_DetailsProbe("custom", {"circuit_breaker": circuit_breaker}))
    registry.add(NoOpProbe(name="db"))
    resp = TestClient(app).get("/health/metrics")
    assert resp.status_code == 200
    assert 'probe_healthy{name="db",critical="true"} 1' in resp.text
    assert 'probe_circuit_open{name="custom",critical="true"} 0' in resp.text
    _assert_valid_exposition(resp.text)


def test_render_prometheus_writes_only_parseable_values():
    # "True" is not a sample value, and one unparseable value fails the whole
    # scrape; NaN and ±Inf are spelled as the format defines them.
    results = [
        ProbeResult(name="a", status=ProbeStatus.HEALTHY, latency_ms=math.nan,
                    details={"circuit_breaker": {"open": True, "consecutive_failures": True}}),
        ProbeResult(name="b", status=ProbeStatus.HEALTHY, latency_ms=math.inf,
                    details={"circuit_breaker": {"consecutive_failures": "three"}}),
        ProbeResult(name="c", status=ProbeStatus.HEALTHY, latency_ms=-math.inf,
                    details={"circuit_breaker": {"consecutive_failures": 4.0}}),
        ProbeResult(name="d", status=ProbeStatus.HEALTHY, latency_ms=12.5),
    ]
    text = render_prometheus(results)
    _assert_valid_exposition(text)
    assert 'probe_latency_ms{name="a",critical="true"} NaN' in text
    assert 'probe_latency_ms{name="b",critical="true"} +Inf' in text
    assert 'probe_latency_ms{name="c",critical="true"} -Inf' in text
    assert 'probe_latency_ms{name="d",critical="true"} 12.5' in text
    assert 'probe_circuit_open{name="a",critical="true"} 1' in text
    assert 'probe_circuit_consecutive_failures{name="a",critical="true"} 1' in text
    assert 'probe_circuit_consecutive_failures{name="b",critical="true"} NaN' in text
    assert 'probe_circuit_consecutive_failures{name="c",critical="true"} 4.0' in text
