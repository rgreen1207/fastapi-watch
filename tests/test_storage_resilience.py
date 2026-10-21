"""The monitoring store being slow, down, or shared must not affect health checks.

Covers: storage errors and timeouts contained (#16), results limited to this
process's own registered probes (#17), no storage read per request (#7),
circuit-open cached results keeping their checked_at (#19), forgetting
probes this process stopped registering without touching anyone else's
(#31), and probe ``aclose()`` at shutdown.
"""
import asyncio
import logging
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import fastapi_watch.registry as registry_module
from fastapi_watch import HealthRegistry
from fastapi_watch.models import ProbeResult, ProbeStatus
from fastapi_watch.probes.base import BaseProbe
from fastapi_watch.probes.noop import NoOpProbe
from fastapi_watch.storage import InMemoryProbeStorage


class _BrokenStorage:
    """Every operation raises, as a Redis client does with the server down."""

    def __init__(self):
        self.calls: list[str] = []

    def _fail(self, op):
        self.calls.append(op)
        raise ConnectionError("storage down")

    async def get_latest(self, name):
        self._fail("get_latest")

    async def get_all_latest(self):
        self._fail("get_all_latest")

    async def set_latest(self, result):
        self._fail("set_latest")

    def clear_latest(self):
        self._fail("clear_latest")

    async def append_history(self, result):
        self._fail("append_history")

    async def get_history(self):
        self._fail("get_history")

    async def append_alert(self, alert):
        self._fail("append_alert")

    async def get_alerts(self):
        self._fail("get_alerts")

    async def forget(self, names):
        self._fail("forget")


class _HangingStorage(InMemoryProbeStorage):
    """Every operation hangs, as a backend does during a network partition."""

    async def _hang(self):
        await asyncio.sleep(30)

    async def set_latest(self, result):
        await self._hang()

    async def append_history(self, result):
        await self._hang()

    async def append_alert(self, alert):
        await self._hang()

    async def get_history(self):
        await self._hang()

    async def get_alerts(self):
        await self._hang()


class _CountingStorage(InMemoryProbeStorage):
    def __init__(self):
        super().__init__()
        self.reads: list[str] = []
        self.forgotten: list[set] = []

    async def get_latest(self, name):
        self.reads.append("get_latest")
        return await super().get_latest(name)

    async def get_all_latest(self):
        self.reads.append("get_all_latest")
        return await super().get_all_latest()

    async def forget(self, names):
        self.forgotten.append(set(names))
        await super().forget(names)


class _Failing(BaseProbe):
    def __init__(self, name="dep"):
        self.name = name
        self.calls = 0

    async def check(self):
        self.calls += 1
        return ProbeResult(name=self.name, status=ProbeStatus.UNHEALTHY, error="down")


def _app(storage, **kwargs):
    app = FastAPI()
    registry = HealthRegistry(app, storage=storage, poll_interval_ms=None, **kwargs)
    registry.add(NoOpProbe(name="db"))
    return app, registry


# ---------------------------------------------------------------------------
# #16 Storage failures never take health endpoints down
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/health/ready", "/health/status", "/health/metrics"])
def test_live_endpoints_work_with_storage_down(path):
    app, _ = _app(_BrokenStorage())
    resp = TestClient(app).get(path)
    assert resp.status_code == 200
    assert "db" in resp.text


@pytest.mark.parametrize("path", ["/health/ready/stream", "/health/status/stream"])
def test_streams_work_with_storage_down(path):
    app, _ = _app(_BrokenStorage())
    resp = TestClient(app).get(path)
    assert resp.status_code == 200
    assert '"name":"db"' in resp.text


@pytest.mark.parametrize("path", ["/health/history", "/health/alerts"])
def test_storage_backed_endpoints_return_503_json(path):
    app, _ = _app(_BrokenStorage())
    resp = TestClient(app).get(path)
    assert resp.status_code == 503
    assert resp.json() == {"error": "storage unavailable"}


def test_dashboard_renders_without_history_when_storage_down():
    app, _ = _app(_BrokenStorage())
    resp = TestClient(app).get("/health/dashboard")
    assert resp.status_code == 200
    assert 'data-probe="db"' in resp.text


def test_readiness_still_reports_failures_with_storage_down():
    app = FastAPI()
    registry = HealthRegistry(app, storage=_BrokenStorage(), poll_interval_ms=None)
    registry.add(_Failing())
    assert TestClient(app).get("/health/ready").status_code == 503


@pytest.mark.asyncio
async def test_hanging_storage_is_bounded_by_storage_timeout():
    registry = HealthRegistry(FastAPI(), storage=_HangingStorage(), poll_interval_ms=None, storage_timeout=0.1)
    registry.add(_Failing())  # also writes an alert record
    started = time.monotonic()
    results = await registry._get_results()
    elapsed = time.monotonic() - started

    assert [r.status for r in results] == [ProbeStatus.UNHEALTHY]
    # Writes run concurrently: one timeout, not one per write.
    assert elapsed < 0.3


def test_hanging_storage_history_returns_503_after_timeout():
    app = FastAPI()
    HealthRegistry(app, storage=_HangingStorage(), poll_interval_ms=None, storage_timeout=0.1)
    assert TestClient(app).get("/health/history").status_code == 503


@pytest.mark.asyncio
async def test_storage_timeout_none_waits_for_slow_storage():
    class _Slow(InMemoryProbeStorage):
        async def set_latest(self, result):
            await asyncio.sleep(0.15)
            await super().set_latest(result)

    storage = _Slow()
    registry = HealthRegistry(FastAPI(), storage=storage, poll_interval_ms=None, storage_timeout=None)
    registry.add(NoOpProbe(name="db"))
    await registry._get_results()
    assert await storage.get_latest("db") is not None


@pytest.mark.parametrize("value", [0, -1])
def test_invalid_storage_timeout_is_rejected(value):
    with pytest.raises(ValueError, match="storage_timeout"):
        HealthRegistry(FastAPI(), storage_timeout=value)


def test_storage_failures_are_logged_once_per_interval(caplog):
    app, registry = _app(_BrokenStorage())
    client = TestClient(app)
    with caplog.at_level(logging.WARNING, logger="fastapi_watch"):
        for _ in range(5):
            client.get("/health/ready")
    set_latest_logs = [r for r in caplog.records if "storage set_latest failed" in r.getMessage()]
    assert len(set_latest_logs) == 1
    assert set_latest_logs[0].exc_info is not None  # the cause is kept
    assert registry._storage_failures["set_latest"][1] == 4  # suppressed, counted

    # Once the interval has passed, the next failure is logged with the count.
    last, suppressed = registry._storage_failures["set_latest"]
    registry._storage_failures["set_latest"] = (last - registry_module._STORAGE_LOG_INTERVAL_S, suppressed)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="fastapi_watch"):
        client.get("/health/ready")
    messages = [r.getMessage() for r in caplog.records if "storage set_latest failed" in r.getMessage()]
    assert len(messages) == 1 and "4 more failure(s)" in messages[0]


@pytest.mark.asyncio
async def test_timeout_is_logged_without_traceback(caplog):
    registry = HealthRegistry(FastAPI(), storage=_HangingStorage(), poll_interval_ms=None, storage_timeout=0.05)
    registry.add(NoOpProbe(name="db"))
    with caplog.at_level(logging.WARNING, logger="fastapi_watch"):
        await registry._get_results()
    records = [r for r in caplog.records if "storage set_latest timed out after 0.05 s" in r.getMessage()]
    assert len(records) == 1 and records[0].exc_info is None


def test_storage_failure_goes_to_the_configured_logger():
    messages = []

    class _Handler(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    logger = logging.getLogger("test_storage_resilience.custom")
    logger.addHandler(_Handler())
    app, _ = _app(_BrokenStorage(), logger=logger)
    TestClient(app).get("/health/ready")
    assert any("storage set_latest failed" in m for m in messages)


@pytest.mark.asyncio
async def test_state_change_with_storage_down_still_runs_callbacks_and_alerts():
    sent = []

    class _Alerter:
        async def notify(self, alert):
            sent.append(alert.new_status)

    registry = HealthRegistry(FastAPI(), storage=_BrokenStorage(), poll_interval_ms=None, alerters=[_Alerter()])
    changes = []
    registry.on_state_change(lambda *args: changes.append(args))
    registry.add(_Failing())
    await registry.run_all()
    await asyncio.sleep(0.05)
    try:
        assert changes == [("dep", ProbeStatus.HEALTHY, ProbeStatus.UNHEALTHY)]
        assert sent == [ProbeStatus.UNHEALTHY]
    finally:
        await registry._shutdown()


@pytest.mark.asyncio
async def test_circuit_open_with_storage_down_and_no_local_result():
    registry = HealthRegistry(FastAPI(), storage=_BrokenStorage(), poll_interval_ms=None)
    probe = _Failing()
    registry.add(probe)
    registry._circuit_open_until[probe.name] = asyncio.get_running_loop().time() + 60
    result = await registry._safe_check(probe, critical=True)
    assert result.status == ProbeStatus.UNHEALTHY
    assert "circuit breaker open" in result.error
    assert probe.calls == 0


def test_set_poll_interval_survives_a_failing_clear_latest():
    registry = HealthRegistry(FastAPI(), storage=_BrokenStorage())
    registry.set_poll_interval(0)  # must not raise
    assert registry._poll_interval_ms is None


# ---------------------------------------------------------------------------
# #17 Only this process's registered probes, as checked by this process
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shared_storage_does_not_leak_other_replicas_results():
    shared = InMemoryProbeStorage()
    # Another replica found "db" down; another app sharing the store has "billing".
    await shared.set_latest(ProbeResult(name="db", status=ProbeStatus.UNHEALTHY, error="theirs"))
    await shared.set_latest(ProbeResult(name="billing", status=ProbeStatus.UNHEALTHY))

    app = FastAPI()
    registry = HealthRegistry(app, storage=shared, poll_interval_ms=60_000)
    probe = NoOpProbe(name="db")
    registry.add(probe)
    results = await registry._get_results()

    assert [(r.name, r.status) for r in results] == [("db", ProbeStatus.HEALTHY)]
    assert "db" in registry._probe_last_run  # it ran here


@pytest.mark.asyncio
async def test_other_replicas_later_writes_do_not_change_this_process_results():
    shared = InMemoryProbeStorage()
    registry = HealthRegistry(FastAPI(), storage=shared, poll_interval_ms=60_000)
    registry.add(NoOpProbe(name="db"))
    await registry._get_results()

    await shared.set_latest(ProbeResult(name="db", status=ProbeStatus.UNHEALTHY, error="theirs"))
    results = await registry._get_results()
    assert results[0].status == ProbeStatus.HEALTHY


def test_readiness_reflects_this_process_not_the_shared_store():
    shared = InMemoryProbeStorage()
    healthy_app = FastAPI()
    HealthRegistry(healthy_app, storage=shared, poll_interval_ms=60_000).add(NoOpProbe(name="db"))
    failing_app = FastAPI()
    HealthRegistry(failing_app, storage=shared, poll_interval_ms=60_000).add(_Failing(name="db"))

    assert TestClient(failing_app).get("/health/ready").status_code == 503
    assert TestClient(healthy_app).get("/health/ready").status_code == 200
    assert TestClient(failing_app).get("/health/ready").status_code == 503


@pytest.mark.asyncio
async def test_duplicate_probe_names_are_reported_once():
    registry = HealthRegistry(FastAPI(), poll_interval_ms=None)
    registry._probes += [(NoOpProbe(name="db"), True), (NoOpProbe(name="db"), True)]
    assert [r.name for r in await registry._get_results()] == ["db"]


# ---------------------------------------------------------------------------
# #7 No storage read on the request path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("poll_interval_ms", [None, 60_000])
def test_requests_do_not_read_latest_results_from_storage(poll_interval_ms):
    storage = _CountingStorage()
    app = FastAPI()
    HealthRegistry(app, storage=storage, poll_interval_ms=poll_interval_ms).add(NoOpProbe(name="db"))
    client = TestClient(app)
    for path in ["/health/ready", "/health/status", "/health/metrics", "/health/ready"]:
        assert client.get(path).status_code == 200
    assert storage.reads == []


# ---------------------------------------------------------------------------
# #19 Circuit-open cached results keep their original checked_at
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_circuit_open_cached_result_keeps_its_checked_at():
    registry = HealthRegistry(
        FastAPI(), poll_interval_ms=None, circuit_breaker_threshold=2, circuit_breaker_cooldown_ms=60_000
    )
    probe = _Failing()
    registry.add(probe)
    await registry.run_all()
    (tripping,) = await registry.run_all()  # second failure opens the circuit
    last_checked = registry._last_checked_at
    await asyncio.sleep(0.01)
    (cached,) = await registry.run_all()

    assert probe.calls == 2
    assert cached.details["circuit_breaker"]["open"] is True
    assert cached.checked_at == tripping.checked_at
    assert registry._last_checked_at == last_checked  # nothing was checked


@pytest.mark.asyncio
async def test_fresh_results_still_get_a_new_checked_at():
    registry = HealthRegistry(FastAPI(), poll_interval_ms=None)
    registry.add(NoOpProbe(name="db"))
    (first,) = await registry.run_all()
    await asyncio.sleep(0.01)
    (second,) = await registry.run_all()
    assert second.checked_at > first.checked_at


# ---------------------------------------------------------------------------
# #31 Forgetting probes this process stopped registering
# ---------------------------------------------------------------------------

def _make_due(registry):
    registry._last_forget -= registry_module._FORGET_INTERVAL_S


def _without(registry, name):
    registry._probes = [(p, c) for p, c in registry._probes if p.name != name]


@pytest.mark.asyncio
async def test_requests_forget_probes_no_longer_registered():
    storage = _CountingStorage()
    registry = HealthRegistry(FastAPI(), storage=storage, poll_interval_ms=None)
    registry.add(NoOpProbe(name="db")).add(NoOpProbe(name="cache"))
    await registry._get_results()
    await asyncio.sleep(0)  # the pass runs in the background
    assert registry._last_forget is not None
    assert storage.forgotten == []  # every written name is still registered: no call
    assert registry._written_names == {"db", "cache"}

    _without(registry, "cache")
    await registry._get_results()
    await asyncio.sleep(0)
    assert storage.forgotten == []  # at most every _FORGET_INTERVAL_S

    _make_due(registry)
    await registry._get_results()
    await asyncio.sleep(0)
    assert storage.forgotten == [{"cache"}]
    assert registry._written_names == {"db"}
    assert set(await storage.get_all_latest()) == {"db"}

    _make_due(registry)
    await registry._get_results()
    await asyncio.sleep(0)
    assert storage.forgotten == [{"cache"}]  # dropped from tracking: never sent again


@pytest.mark.asyncio
async def test_poll_loop_forgets():
    storage = _CountingStorage()
    registry = HealthRegistry(FastAPI(), storage=storage, poll_interval_ms=1000)
    registry.add(NoOpProbe(name="db"))
    registry._written_names.add("old")  # written earlier, no longer registered
    task = asyncio.create_task(registry._poll_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    assert storage.forgotten == [{"old"}]


@pytest.mark.asyncio
async def test_never_forgets_names_this_process_did_not_write():
    storage = _CountingStorage()
    for name in ("other-app", "old-pod-probe"):  # written by other processes
        await storage.set_latest(ProbeResult(name=name, status=ProbeStatus.HEALTHY))
        await storage.append_history(ProbeResult(name=name, status=ProbeStatus.HEALTHY))
    registry = HealthRegistry(FastAPI(), storage=storage, poll_interval_ms=None)
    registry.add(NoOpProbe(name="db"))
    await registry.run_all()
    for _ in range(3):
        await registry._forget_unregistered()
    assert storage.forgotten == []
    assert set(await storage.get_all_latest()) == {"db", "other-app", "old-pod-probe"}
    assert set(await storage.get_history()) == {"db", "other-app", "old-pod-probe"}


@pytest.mark.asyncio
async def test_shared_storage_two_apps_keep_each_others_data():
    storage = InMemoryProbeStorage()
    one = HealthRegistry(FastAPI(), storage=storage, poll_interval_ms=None)
    one.add(NoOpProbe(name="a")).add(NoOpProbe(name="b"))
    two = HealthRegistry(FastAPI(), storage=storage, poll_interval_ms=None)
    two.add(NoOpProbe(name="b")).add(NoOpProbe(name="c"))  # rolling deploy: overlapping sets
    await one.run_all()
    await two.run_all()
    for registry in (one, two, one, two):
        await registry._forget_unregistered()
    assert set(await storage.get_all_latest()) == {"a", "b", "c"}
    assert set(await storage.get_history()) == {"a", "b", "c"}


@pytest.mark.asyncio
async def test_result_named_differently_from_its_probe_is_not_tracked():
    class _Renaming(BaseProbe):
        name = "mine"

        async def check(self):
            return ProbeResult(name="someone-elses", status=ProbeStatus.HEALTHY)

    registry = HealthRegistry(FastAPI(), poll_interval_ms=None)
    registry.add(_Renaming())
    await registry.run_all()
    assert registry._written_names == set()


@pytest.mark.asyncio
async def test_discover_routes_refresh_forgets_the_replaced_probe_name():
    app = FastAPI()

    @app.get("/items")
    async def items():
        return []

    storage = _CountingStorage()
    registry = HealthRegistry(app, storage=storage, poll_interval_ms=None)
    registry.discover_routes(name_fn=lambda route: "items-v1")
    await registry.run_all()
    registry.discover_routes(refresh=True, name_fn=lambda route: "items-v2")
    await registry.run_all()
    assert {p.name for p, _ in registry._probes} == {"items-v2"}
    await registry._forget_unregistered()
    assert storage.forgotten == [{"items-v1"}]
    assert set(await storage.get_all_latest()) == {"items-v2"}


@pytest.mark.asyncio
async def test_forget_drops_local_state_of_removed_probes():
    registry = HealthRegistry(FastAPI(), poll_interval_ms=None)
    old = NoOpProbe(name="old")
    registry.add(old).add(NoOpProbe(name="db"))
    await registry.run_all()
    registry._probes = [(p, c) for p, c in registry._probes if p is not old]
    await registry._forget_unregistered()
    assert set(registry._last_results) == {"db"}
    assert set(registry._probe_last_run) == {"db"}
    assert await registry._storage.get_latest("old") is None


@pytest.mark.asyncio
async def test_forget_failure_is_contained_and_retried():
    registry = HealthRegistry(FastAPI(), storage=_BrokenStorage(), poll_interval_ms=None)
    registry.add(NoOpProbe(name="db"))
    await registry.run_all()  # writes fail, but may have landed: the name is tracked
    _without(registry, "db")
    await registry._forget_unregistered()  # must not raise
    assert "forget" in registry._storage.calls
    assert registry._written_names == {"db"}  # kept for the next pass


@pytest.mark.asyncio
async def test_storage_without_forget_is_fine():
    class _NoForget:
        def __init__(self):
            self._inner = InMemoryProbeStorage()

        def __getattr__(self, name):
            if name == "forget":
                raise AttributeError(name)
            return getattr(self._inner, name)

    registry = HealthRegistry(FastAPI(), storage=_NoForget(), poll_interval_ms=None)
    registry.add(NoOpProbe(name="db"))
    await registry._get_results()
    await asyncio.sleep(0)
    assert registry._last_forget is not None
    _without(registry, "db")
    await registry._forget_unregistered()
    assert registry._written_names == set()  # tracking stays bounded


# ---------------------------------------------------------------------------
# Shutdown closes probes
# ---------------------------------------------------------------------------

class _Closable(NoOpProbe):
    def __init__(self, name, behaviour="ok"):
        super().__init__(name=name)
        self.behaviour = behaviour
        self.closed = 0

    async def aclose(self):
        self.closed += 1
        if self.behaviour == "raise":
            raise RuntimeError("close failed")
        if self.behaviour == "hang":
            await asyncio.sleep(30)


@pytest.mark.asyncio
async def test_shutdown_closes_every_probe_despite_failures(monkeypatch, caplog):
    monkeypatch.setattr(registry_module, "_ACLOSE_TIMEOUT_S", 0.05)
    ok, raises, hangs, startup = _Closable("ok"), _Closable("raises", "raise"), _Closable("hangs", "hang"), _Closable("s")
    registry = HealthRegistry(FastAPI(), startup_probes=[startup, ok])
    registry.add(ok).add(raises).add(hangs).add(NoOpProbe(name="plain"))

    with caplog.at_level(logging.WARNING, logger="fastapi_watch"):
        await asyncio.wait_for(registry._shutdown(), timeout=1)

    assert (ok.closed, raises.closed, hangs.closed, startup.closed) == (1, 1, 1, 1)
    messages = [r.getMessage() for r in caplog.records]
    assert any("'raises' aclose() failed" in m for m in messages)
    assert any("'hangs' aclose() timed out" in m for m in messages)


@pytest.mark.asyncio
async def test_shutdown_cancels_a_pending_forget():
    class _SlowForget(InMemoryProbeStorage):
        async def forget(self, names):
            await asyncio.sleep(30)

    registry = HealthRegistry(FastAPI(), storage=_SlowForget(), poll_interval_ms=None, storage_timeout=None)
    registry.add(NoOpProbe(name="db"))
    registry._written_names.add("old")
    await registry._get_results()
    await asyncio.sleep(0)
    assert registry._forget_task is not None and not registry._forget_task.done()
    await asyncio.wait_for(registry._shutdown(), timeout=1)
    assert registry._forget_task is None
