"""A probe with nothing to report yet has no status (``no_data``).

Passive probes, route probes, WebSocket probes and RequestMetricsProbe used
to report ``healthy`` before observing a single call.  They now report
``no_data``: never healthy or failing, never alerting, and never failing
readiness (a pod whose probes need traffic must still become ready).
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi_watch import FastAPIRouteProbe, HealthRegistry, InMemoryProbeStorage, ThresholdProbe
from fastapi_watch.models import HealthReport, ProbeResult, ProbeStatus
from fastapi_watch.probes import NoOpProbe
from fastapi_watch.probes.base import BaseProbe
from fastapi_watch.prometheus import render_prometheus


class _Scripted(BaseProbe):
    def __init__(self, name: str, statuses: list[ProbeStatus]) -> None:
        self.name = name
        self._statuses = statuses

    async def check(self) -> ProbeResult:
        status = self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]
        return ProbeResult(name=self.name, status=status)


def _result(name, status, critical=True):
    return ProbeResult(name=name, status=status, critical=critical)


def test_report_with_only_no_data_has_no_status():
    report = HealthReport.from_results([_result("a", ProbeStatus.NO_DATA)])
    assert report.status == ProbeStatus.NO_DATA
    assert report.failing == []


@pytest.mark.parametrize("other, overall", [
    (ProbeStatus.HEALTHY, ProbeStatus.HEALTHY),
    (ProbeStatus.DEGRADED, ProbeStatus.DEGRADED),
    (ProbeStatus.UNHEALTHY, ProbeStatus.UNHEALTHY),
])
def test_no_data_does_not_affect_the_others(other, overall):
    report = HealthReport.from_results([_result("a", ProbeStatus.NO_DATA), _result("b", other)])
    assert report.status == overall


def test_non_critical_data_does_not_give_critical_no_data_a_status():
    report = HealthReport.from_results([
        _result("a", ProbeStatus.NO_DATA), _result("b", ProbeStatus.HEALTHY, critical=False),
    ])
    assert report.status == ProbeStatus.NO_DATA


def test_result_properties():
    result = _result("a", ProbeStatus.NO_DATA)
    assert not result.has_data and not result.is_healthy and result.is_passing


def test_ready_and_status_pass_without_data():
    app = FastAPI()
    HealthRegistry(app, poll_interval_ms=None).add(FastAPIRouteProbe(name="orders"))
    client = TestClient(app)
    ready = client.get("/health/ready")
    assert ready.status_code == 200 and ready.json()["status"] == "no_data"
    status = client.get("/health/status")
    assert status.status_code == 200
    assert status.json()["probes"][0]["status"] == "no_data"


async def test_no_data_never_alerts_or_calls_back():
    app = FastAPI()
    sent, changes = [], []

    class _Alerter:
        async def notify(self, alert):
            sent.append((alert.old_status.value, alert.new_status.value))

    registry = HealthRegistry(app, poll_interval_ms=None, alerters=[_Alerter()])
    registry.on_state_change(lambda name, old, new: changes.append((old.value, new.value)))
    registry.add(_Scripted("svc", [ProbeStatus.NO_DATA, ProbeStatus.NO_DATA, ProbeStatus.HEALTHY,
                                   ProbeStatus.UNHEALTHY]))
    for _ in range(4):
        await registry.run_all()
    await registry._alert_queue.join()
    # no_data -> healthy is not a change; the first real failure is.
    assert changes == [("healthy", "unhealthy")]
    assert sent == [("healthy", "unhealthy")]
    assert [a.new_status for a in await registry._storage.get_alerts()] == [ProbeStatus.UNHEALTHY]


def test_prometheus_gives_no_status_sample_without_data():
    text = render_prometheus([_result("idle", ProbeStatus.NO_DATA), _result("busy", ProbeStatus.HEALTHY)])
    lines = text.splitlines()
    assert 'probe_healthy{name="busy",critical="true"} 1' in lines
    assert not any(line.startswith('probe_healthy{name="idle"') for line in lines)
    assert not any(line.startswith('probe_degraded{name="idle"') for line in lines)
    assert 'probe_no_data{name="idle",critical="true"} 1' in lines
    assert 'probe_no_data{name="busy",critical="true"} 0' in lines


async def test_threshold_passes_no_data_through():
    inner = FastAPIRouteProbe(name="orders")
    # Would match on the empty details if it were evaluated.
    probe = ThresholdProbe(inner, fail_if=lambda d: d.get("error_rate", 1.0) > 0.5)
    assert (await probe.check()).status == ProbeStatus.NO_DATA


def test_dashboard_renders_no_data():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(FastAPIRouteProbe(name="orders")).add(NoOpProbe(name="db"))
    response = TestClient(app).get("/health/dashboard")
    assert response.status_code == 200


async def test_no_data_is_not_written_to_storage():
    # Replicas share a probe's history: one replica's no_data interleaved with
    # another's real results, and a replica still on 1.7.1 sharing a custom
    # backend failed every read of the unknown status (/health/ready 500).
    shared = InMemoryProbeStorage()
    idle, busy = (HealthRegistry(FastAPI(), poll_interval_ms=None, storage=shared) for _ in range(2))
    idle.add(_Scripted("svc", [ProbeStatus.NO_DATA]))
    busy.add(_Scripted("svc", [ProbeStatus.HEALTHY]))
    for _ in range(2):
        assert (await idle.run_all())[0].status == ProbeStatus.NO_DATA
        await busy.run_all()
    assert [r.status for r in (await shared.get_history())["svc"]] == [ProbeStatus.HEALTHY] * 2
    assert (await shared.get_latest("svc")).status == ProbeStatus.HEALTHY


async def test_forgetting_a_probe_that_only_had_no_data_leaves_shared_data_alone():
    # Nothing was written for it, so another replica's data under that name is
    # not this process's to delete when it stops registering the probe.
    shared = InMemoryProbeStorage()
    idle, busy = (HealthRegistry(FastAPI(), poll_interval_ms=None, storage=shared) for _ in range(2))
    idle.add(_Scripted("svc", [ProbeStatus.NO_DATA]))
    busy.add(_Scripted("svc", [ProbeStatus.HEALTHY]))
    await busy.run_all()
    await idle.run_all()
    assert idle._written_names == set()
    idle._probes.clear()  # e.g. discover_routes(refresh=True) dropped it
    await idle._forget_unregistered()
    assert [r.status for r in (await shared.get_history())["svc"]] == [ProbeStatus.HEALTHY]
    assert (await shared.get_latest("svc")).status == ProbeStatus.HEALTHY
