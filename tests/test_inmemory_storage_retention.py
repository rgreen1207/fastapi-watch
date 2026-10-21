"""InMemoryProbeStorage.configure() and prune()."""
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import fastapi_watch.storage as storage_module
from fastapi_watch.models import AlertRecord, ProbeResult, ProbeStatus
from fastapi_watch.storage import InMemoryProbeStorage


def _result(name="db", latency=0.0):
    return ProbeResult(name=name, status=ProbeStatus.HEALTHY, latency_ms=latency)


def _alert(probe):
    return AlertRecord(
        probe=probe,
        old_status=ProbeStatus.HEALTHY,
        new_status=ProbeStatus.UNHEALTHY,
        timestamp=datetime.now(timezone.utc),
    )


async def test_configure_shrinks_keeping_newest():
    store = InMemoryProbeStorage(max_results=10, max_alerts=10)
    for i in range(10):
        await store.append_history(_result(latency=float(i)))
        await store.append_alert(_alert(f"p{i}"))
    store.configure(max_results=3, result_ttl_seconds=0, alert_ttl_seconds=0, max_alerts=2)
    assert [r.latency_ms for r in (await store.get_history())["db"]] == [7.0, 8.0, 9.0]
    assert [a.probe for a in await store.get_alerts()] == ["p8", "p9"]
    await store.append_history(_result(latency=10.0))
    await store.append_alert(_alert("p10"))
    assert [r.latency_ms for r in (await store.get_history())["db"]] == [8.0, 9.0, 10.0]
    assert [a.probe for a in await store.get_alerts()] == ["p9", "p10"]


async def test_configure_grows_and_new_probes_use_new_cap():
    store = InMemoryProbeStorage(max_results=2)
    store.configure(max_results=5, result_ttl_seconds=7200, alert_ttl_seconds=259200, max_alerts=120)
    for name in ("old", "new"):
        for i in range(8):
            await store.append_history(_result(name=name, latency=float(i)))
    history = await store.get_history()
    assert len(history["old"]) == 5 and len(history["new"]) == 5


async def test_configure_applies_ttls(monkeypatch):
    store = InMemoryProbeStorage(result_ttl_seconds=0)
    await store.set_latest(_result())
    await store.append_history(_result())
    store.configure(max_results=120, result_ttl_seconds=10, alert_ttl_seconds=10, max_alerts=120)
    real = time.monotonic
    # Patch only the storage module's clock; the event loop keeps the real one.
    monkeypatch.setattr(storage_module, "time", SimpleNamespace(monotonic=lambda: real() + 60))
    assert await store.get_history() == {}
    assert await store.get_all_latest() == {}


async def test_configure_clamps_counts():
    store = InMemoryProbeStorage()
    store.configure(max_results=0, result_ttl_seconds=0, alert_ttl_seconds=0, max_alerts=0)
    await store.append_history(_result())
    await store.append_history(_result(latency=1.0))
    assert [r.latency_ms for r in (await store.get_history())["db"]] == [1.0]


async def test_prune_drops_unlisted_probes():
    store = InMemoryProbeStorage()
    for name in ("keep", "drop"):
        await store.set_latest(_result(name=name))
        await store.append_history(_result(name=name))
    await store.append_alert(_alert("drop"))
    await store.prune({"keep"})
    assert list(await store.get_all_latest()) == ["keep"]
    assert list(await store.get_history()) == ["keep"]
    assert await store.get_latest("drop") is None
    assert [a.probe for a in await store.get_alerts()] == ["drop"]  # alert log is left alone
    await store.prune(set())
    assert await store.get_all_latest() == {} and await store.get_history() == {}


async def test_claim_alert_first_caller_wins_until_expiry(monkeypatch):
    store = InMemoryProbeStorage()
    assert await store.claim_alert("db:unhealthy", 10) is True
    assert await store.claim_alert("db:unhealthy", 10) is False
    assert await store.claim_alert("db:healthy", 10) is True
    real = time.monotonic
    monkeypatch.setattr(storage_module, "time", SimpleNamespace(monotonic=lambda: real() + 60))
    assert await store.claim_alert("db:unhealthy", 10) is True
    assert list(store._claims) == ["db:unhealthy"]  # expired claims purged: stays bounded


async def test_claim_alert_non_positive_ttl_always_true_and_stores_nothing():
    store = InMemoryProbeStorage()
    for _ in range(3):
        assert await store.claim_alert("db:unhealthy", 0) is True
        assert await store.claim_alert("db:unhealthy", -1) is True
    assert store._claims == {}
