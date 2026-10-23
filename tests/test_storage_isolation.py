"""Apps sharing a storage namespace see only their own probes.

Two applications on one Redis prefix (the default prefix is the same for
every app) each served the other's probe history and alerts on
``/health/history``, ``/health/alerts`` and the dashboard.  Replicas of one
app register the same probe names, so they still share them.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi_watch import HealthRegistry, InMemoryProbeStorage
from fastapi_watch.models import ProbeResult, ProbeStatus
from fastapi_watch.probes.base import BaseProbe


class _Flips(BaseProbe):
    def __init__(self, name: str) -> None:
        self.name = name
        self._calls = 0

    async def check(self) -> ProbeResult:
        self._calls += 1
        status = ProbeStatus.UNHEALTHY if self._calls % 2 else ProbeStatus.HEALTHY
        return ProbeResult(name=self.name, status=status, details={"secret_detail": self.name})


class _LegacyStorage:
    """A custom backend written before get_history/get_alerts took names."""

    def __init__(self, inner: InMemoryProbeStorage) -> None:
        self._inner = inner

    async def get_latest(self, name): return await self._inner.get_latest(name)
    async def get_all_latest(self): return await self._inner.get_all_latest()
    async def set_latest(self, result): await self._inner.set_latest(result)
    def clear_latest(self): self._inner.clear_latest()
    async def append_history(self, result): await self._inner.append_history(result)
    async def get_history(self): return await self._inner.get_history()
    async def append_alert(self, alert): await self._inner.append_alert(alert)
    async def get_alerts(self): return await self._inner.get_alerts()


class _KeywordOnlyNamesStorage(InMemoryProbeStorage):
    """A custom backend whose names argument is keyword-only."""

    async def get_history(self, *, names=None):
        return await super().get_history(names)

    async def get_alerts(self, *, names=None):
        return await super().get_alerts(names)


def _two_apps(storage_factory):
    shared = InMemoryProbeStorage()
    apps = []
    for name in ("billing", "search"):
        app = FastAPI()
        HealthRegistry(app, storage=storage_factory(shared), poll_interval_ms=None).add(_Flips(f"{name}-db"))
        apps.append(app)
    return apps


@pytest.mark.parametrize("storage_factory", [lambda s: s, _LegacyStorage], ids=["builtin", "legacy-backend"])
def test_history_alerts_and_dashboard_show_only_own_probes(storage_factory):
    billing, search = _two_apps(storage_factory)
    with TestClient(billing) as b, TestClient(search) as s:
        for _ in range(3):
            b.get("/health/status")
            s.get("/health/status")
        assert set(b.get("/health/history").json()["probes"]) == {"billing-db"}
        assert set(s.get("/health/history").json()["probes"]) == {"search-db"}
        assert {a["probe"] for a in b.get("/health/alerts").json()["alerts"]} == {"billing-db"}
        assert {a["probe"] for a in s.get("/health/alerts").json()["alerts"]} == {"search-db"}
        assert "search-db" not in b.get("/health/dashboard").text
        assert "billing-db" not in s.get("/health/dashboard").text


def test_backend_with_keyword_only_names_is_filtered_after_the_read():
    # names was passed positionally: every read raised TypeError, so history
    # and alerts answered 503 for as long as the app ran.
    app = FastAPI()
    HealthRegistry(app, storage=_KeywordOnlyNamesStorage(), poll_interval_ms=None).add(_Flips("api-db"))
    with TestClient(app) as client:
        client.get("/health/status")
        client.get("/health/status")
        history = client.get("/health/history")
        alerts = client.get("/health/alerts")
    assert history.status_code == 200 and set(history.json()["probes"]) == {"api-db"}
    assert alerts.status_code == 200 and {a["probe"] for a in alerts.json()["alerts"]} == {"api-db"}


def test_replicas_still_share_history():
    shared = InMemoryProbeStorage()
    replicas = []
    for _ in range(2):
        app = FastAPI()
        HealthRegistry(app, storage=shared, poll_interval_ms=None).add(_Flips("api-db"))
        replicas.append(app)
    with TestClient(replicas[0]) as first, TestClient(replicas[1]) as second:
        first.get("/health/status")
        second.get("/health/status")
        assert len(first.get("/health/history").json()["probes"]["api-db"]) == 2


async def test_inmemory_reads_by_name():
    store = InMemoryProbeStorage()
    for name in ("a", "b"):
        await store.append_history(ProbeResult(name=name, status=ProbeStatus.HEALTHY))
    assert set(await store.get_history(["a", "missing"])) == {"a"}
    assert set(await store.get_history()) == {"a", "b"}
