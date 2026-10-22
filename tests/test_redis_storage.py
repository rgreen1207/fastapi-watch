"""RedisProbeStorage against a real Redis.

Set FASTAPI_WATCH_REDIS_URL (e.g. redis://localhost:6379/0) to run these; they
skip when it is unset or the server is unreachable.  Every test uses its own
random key prefix and deletes its keys afterwards.
"""
import asyncio
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from fastapi_watch import HealthRegistry, RedisProbeStorage
from fastapi_watch.probes import NoOpProbe
from fastapi_watch.models import AlertRecord, ProbeResult, ProbeStatus
from fastapi_watch.storage import ProbeStorage

redis_asyncio = pytest.importorskip("redis.asyncio")

REDIS_URL = os.environ.get("FASTAPI_WATCH_REDIS_URL")
pytestmark = pytest.mark.skipif(not REDIS_URL, reason="FASTAPI_WATCH_REDIS_URL not set")

FORBIDDEN = {"keys", "scan", "scan_iter"}


class _Spy:
    """Wraps a redis.asyncio client: fails on KEYS/SCAN and counts round trips.

    Direct commands and pipeline executions each count as one round trip;
    commands queued on a pipeline are checked against FORBIDDEN too.
    """

    def __init__(self, client):
        self._client = client
        self.round_trips = 0
        self.transactions = 0

    def __getattr__(self, name):
        if name in FORBIDDEN:
            raise AssertionError(f"storage called {name.upper()}")
        attr = getattr(self._client, name)
        if name == "connection_pool" or not callable(attr):
            return attr

        async def call(*args, **kwargs):
            self.round_trips += 1
            return await attr(*args, **kwargs)

        return call

    def pipeline(self, transaction=True):
        spy = self
        pipe = self._client.pipeline(transaction=transaction)

        class _Pipe:
            async def __aenter__(self):
                await pipe.__aenter__()
                return self

            async def __aexit__(self, *exc):
                return await pipe.__aexit__(*exc)

            async def execute(self):
                spy.round_trips += 1
                spy.transactions += bool(transaction)
                return await pipe.execute()

            def __getattr__(self, name):
                if name in FORBIDDEN:
                    raise AssertionError(f"storage queued {name.upper()}")
                return getattr(pipe, name)

        return _Pipe()


@pytest.fixture(params=[False, True], ids=["bytes", "decode_responses"])
async def client(request):
    redis = redis_asyncio.from_url(REDIS_URL, decode_responses=request.param)
    try:
        await redis.ping()
    except Exception as exc:  # unreachable server: skip, don't fail
        await redis.aclose()
        pytest.skip(f"Redis at {REDIS_URL} unreachable: {exc}")
    yield redis
    await redis.aclose()


@pytest.fixture
async def prefix(client):
    value = f"fw-test-{uuid.uuid4().hex[:8]}"
    yield value
    keys = [k async for k in client.scan_iter(match=f"{value}:*")]  # test cleanup only
    if keys:
        await client.delete(*keys)


@pytest.fixture
def spy(client):
    return _Spy(client)


def _store(spy, prefix, **kwargs):
    return RedisProbeStorage(spy, prefix=prefix, **kwargs)


def _result(name="db", status=ProbeStatus.HEALTHY, at=None, **kwargs):
    return ProbeResult(name=name, status=status, checked_at=at or datetime.now(timezone.utc), **kwargs)


def _alert(probe="db", at=None):
    return AlertRecord(
        probe=probe,
        old_status=ProbeStatus.HEALTHY,
        new_status=ProbeStatus.UNHEALTHY,
        timestamp=at or datetime.now(timezone.utc),
    )


async def _keys(client, prefix):
    found = [k async for k in client.scan_iter(match=f"{prefix}:*")]  # test inspection only
    return sorted(k.decode() if isinstance(k, bytes) else k for k in found)


async def test_satisfies_protocol_and_round_trips(spy, prefix):
    store = _store(spy, prefix)
    assert isinstance(store, ProbeStorage)
    result = _result(details={"p95_rtt_ms": 3.5})
    await store.set_latest(result)
    await store.append_history(result)
    await store.append_alert(_alert())
    assert await store.get_latest("db") == result
    assert await store.get_all_latest() == {"db": result}
    assert (await store.get_history())["db"] == [result]
    assert [a.probe for a in await store.get_alerts()] == ["db"]
    assert await store.get_latest("missing") is None


# --- #2 alerts --------------------------------------------------------------

async def test_alerts_capped_at_max_alerts(client, spy, prefix):
    store = _store(spy, prefix, max_alerts=10)
    base = datetime.now(timezone.utc)
    for i in range(50):
        await store.append_alert(_alert(probe=f"p{i}", at=base + timedelta(milliseconds=i)))
    assert await client.zcard(f"{prefix}:alerts") == 10
    assert [a.probe for a in await store.get_alerts()] == [f"p{i}" for i in range(40, 50)]


async def test_alerts_aged_by_alert_ttl(client, spy, prefix):
    store = _store(spy, prefix, alert_ttl_seconds=60)
    now = datetime.now(timezone.utc)
    await store.append_alert(_alert(probe="old", at=now - timedelta(seconds=120)))
    assert await store.get_alerts() == []  # filtered on read
    await store.append_alert(_alert(probe="new", at=now))
    assert await client.zcard(f"{prefix}:alerts") == 1  # pruned on write
    assert [a.probe for a in await store.get_alerts()] == ["new"]
    # The key expires shortly after the TTL even if alerts keep arriving.
    assert 0 < await client.pttl(f"{prefix}:alerts") <= 60_000 + 60_000


async def test_identical_alerts_are_not_collapsed(client, spy, prefix):
    store = _store(spy, prefix)
    alert = _alert()
    for _ in range(3):
        await store.append_alert(alert)
    assert len(await store.get_alerts()) == 3


# --- #3 history age ---------------------------------------------------------

async def test_history_entries_aged_individually(client, spy, prefix):
    store = _store(spy, prefix, result_ttl_seconds=60)
    now = datetime.now(timezone.utc)
    await store.append_history(_result(status=ProbeStatus.UNHEALTHY, at=now - timedelta(seconds=120)))
    await store.append_history(_result(at=now))
    assert await client.zcard(f"{prefix}:history:db") == 1
    assert [r.status for r in (await store.get_history())["db"]] == [ProbeStatus.HEALTHY]


async def test_history_expires_in_real_time(spy, prefix):
    store = _store(spy, prefix, result_ttl_seconds=0.3)
    await store.set_latest(_result())
    await store.append_history(_result())
    assert "db" in await store.get_history()
    await asyncio.sleep(0.5)
    assert await store.get_history() == {}
    assert await store.get_all_latest() == {}
    assert await store.get_latest("db") is None


async def test_history_capped_at_max_results_with_identical_payloads(client, spy, prefix):
    store = _store(spy, prefix, max_results=5)
    result = _result()
    for _ in range(20):
        await store.append_history(result)  # same payload and timestamp every time
    assert await client.zcard(f"{prefix}:history:db") == 5
    assert len((await store.get_history())["db"]) == 5


async def test_history_is_oldest_first(spy, prefix):
    store = _store(spy, prefix)
    base = datetime.now(timezone.utc)
    for i in range(5):
        await store.append_history(_result(latency_ms=float(i), at=base + timedelta(seconds=i - 5)))
    assert [r.latency_ms for r in (await store.get_history())["db"]] == [0.0, 1.0, 2.0, 3.0, 4.0]


# --- #4 ttl=0 ---------------------------------------------------------------

async def test_zero_ttl_keeps_data_and_never_errors(client, spy, prefix):
    store = _store(spy, prefix, result_ttl_seconds=0, alert_ttl_seconds=0, max_results=3, max_alerts=3)
    ancient = datetime.now(timezone.utc) - timedelta(days=365)
    for i in range(5):
        await store.set_latest(_result(latency_ms=float(i), at=ancient))
        await store.append_history(_result(latency_ms=float(i), at=ancient + timedelta(seconds=i)))
        await store.append_alert(_alert(at=ancient + timedelta(seconds=i)))
    assert (await store.get_latest("db")).latency_ms == 4.0
    assert [r.latency_ms for r in (await store.get_history())["db"]] == [2.0, 3.0, 4.0]
    assert len(await store.get_alerts()) == 3
    keys = await _keys(client, prefix)
    assert len(keys) == 4
    for key in keys:
        assert await client.pttl(key) == -1  # present, no expiry


# --- #5 atomicity -----------------------------------------------------------

async def test_every_key_has_a_ttl_and_writes_are_single_transactions(client, spy, prefix):
    store = _store(spy, prefix, result_ttl_seconds=30, alert_ttl_seconds=30)
    before = spy.round_trips
    await store.set_latest(_result())
    await store.append_history(_result())
    await store.append_alert(_alert())
    assert spy.round_trips - before == 3
    assert spy.transactions == 3
    keys = await _keys(client, prefix)
    assert {k.rsplit(":", 1)[-1] for k in keys} >= {"probes", "alerts", "db"}
    for key in keys:
        assert 0 < await client.pttl(key) <= 30_000 + 60_000, key


# --- #6 bytes ---------------------------------------------------------------

def _big_details():
    routes = {f"GET /route/{i}": {"p95_rtt_ms": 1.0, "errors": 0, "note": "x" * 120} for i in range(150)}
    return {"routes": routes, "p95_rtt_ms": 12.5, "error_rate": 0.01}


async def test_history_entries_truncated_to_max_entry_bytes(client, spy, prefix):
    store = _store(spy, prefix, max_entry_bytes=2048)
    result = _result(details=_big_details())
    await store.set_latest(result)
    await store.append_history(result)

    members = await client.zrange(f"{prefix}:history:db", 0, -1)
    assert len(members[0]) <= 2048
    entry = (await store.get_history())["db"][0]
    assert entry.details == {"p95_rtt_ms": 12.5, "error_rate": 0.01, "_truncated": True}
    assert entry.status == result.status and entry.checked_at == result.checked_at
    assert (await store.get_latest("db")).details == result.details  # latest kept in full


async def test_truncation_handles_oversized_text_fields(client, spy, prefix):
    store = _store(spy, prefix, max_entry_bytes=1024)
    await store.append_history(_result(error="é" * 5000, description="d" * 5000, tags=["t" * 3000]))
    members = await client.zrange(f"{prefix}:history:db", 0, -1)
    assert len(members[0] if isinstance(members[0], bytes) else members[0].encode()) <= 1024
    entry = (await store.get_history())["db"][0]
    assert entry.details == {"_truncated": True}
    assert entry.error.startswith("é") and entry.error.endswith("...")


async def test_small_entries_untouched(spy, prefix):
    store = _store(spy, prefix, max_entry_bytes=1024)
    result = _result(details={"p95_rtt_ms": 1.0})
    await store.append_history(result)
    assert (await store.get_history())["db"] == [result]


async def test_total_history_bytes_bounded(client, spy, prefix):
    store = _store(spy, prefix, max_results=20, max_entry_bytes=1024)
    details = _big_details()
    for i in range(100):
        for name in ("a", "b", "c"):
            await store.append_history(_result(name=name, details=details))
    total = 0
    for name in ("a", "b", "c"):
        members = await client.zrange(f"{prefix}:history:{name}", 0, -1)
        assert len(members) == 20
        total += sum(len(m if isinstance(m, bytes) else m.encode()) for m in members)
    assert total <= 3 * 20 * 1024


def test_rejects_tiny_entry_budget_and_negative_ttl():
    with pytest.raises(ValueError):
        RedisProbeStorage(object(), max_entry_bytes=100)
    with pytest.raises(ValueError):
        RedisProbeStorage(object(), result_ttl_seconds=-1)
    with pytest.raises(ValueError):
        RedisProbeStorage(object(), prefix="")


# --- #7 no KEYS/SCAN, bounded round trips -----------------------------------

async def test_never_uses_keys_or_scan(client, spy, prefix):
    def calls(stats):
        return sum(stats.get(f"cmdstat_{c}", {}).get("calls", 0) for c in ("keys", "scan"))

    store = _store(spy, prefix, result_ttl_seconds=0.2)
    before = calls(await client.info("commandstats"))
    for name in ("a", "b"):
        await store.set_latest(_result(name=name))
        await store.append_history(_result(name=name))
    await store.append_alert(_alert())
    await store.get_latest("a")
    await store.get_all_latest()
    await store.get_history()
    await store.get_alerts()
    store.clear_latest()
    await asyncio.gather(*store._pending)
    await asyncio.sleep(0.3)  # let everything expire so the lazy cleanup paths run
    await store.get_all_latest()
    await store.get_history()
    await store.forget({"a", "b"})
    # Server-side check too: covers any path the spy could miss.
    assert calls(await client.info("commandstats")) == before


async def test_reads_take_constant_round_trips(spy, prefix):
    store = _store(spy, prefix)
    for i in range(25):
        await store.set_latest(_result(name=f"p{i}"))
        await store.append_history(_result(name=f"p{i}"))
    before = spy.round_trips
    assert len(await store.get_history()) == 25
    assert spy.round_trips - before == 2  # SMEMBERS + one pipeline
    before = spy.round_trips
    assert len(await store.get_all_latest()) == 25
    assert spy.round_trips - before == 2  # SMEMBERS + MGET


async def test_index_drops_probes_whose_data_expired(client, spy, prefix):
    store = _store(spy, prefix, result_ttl_seconds=0.2)
    await store.set_latest(_result(name="gone"))
    await store.append_history(_result(name="gone"))
    await asyncio.sleep(0.35)
    await store.set_latest(_result(name="kept"))
    await store.append_history(_result(name="kept"))
    assert list(await store.get_all_latest()) == ["kept"]
    assert await store.get_history() != {}
    members = {m.decode() if isinstance(m, bytes) else m for m in await client.smembers(f"{prefix}:probes")}
    assert members == {"kept"}
    assert await client.exists(f"{prefix}:history:gone") == 0


async def test_cleared_latest_keeps_history_indexed_with_zero_ttl(spy, prefix):
    store = _store(spy, prefix, result_ttl_seconds=0)
    await store.set_latest(_result())
    await store.append_history(_result())
    store.clear_latest()
    await asyncio.gather(*store._pending)
    assert await store.get_all_latest() == {}  # lazy cleanup must not drop history
    assert len((await store.get_history())["db"]) == 1


# --- prefix isolation -------------------------------------------------------

async def test_prefixes_are_isolated(client, spy, prefix):
    other = f"{prefix}:other"  # cleaned up by the prefix fixture's pattern
    one, two = _store(spy, prefix), _store(spy, other)
    await one.set_latest(_result(name="a"))
    await one.append_history(_result(name="a"))
    await one.append_alert(_alert(probe="a"))
    await two.set_latest(_result(name="b"))
    await two.append_history(_result(name="b"))
    assert list(await one.get_all_latest()) == ["a"]
    assert list(await two.get_all_latest()) == ["b"]
    assert list(await two.get_history()) == ["b"]
    assert await two.get_alerts() == []
    two.clear_latest()
    await asyncio.gather(*two._pending)
    await two.forget({"a", "b"})
    assert list(await one.get_all_latest()) == ["a"]
    assert list(await one.get_history()) == ["a"]


# --- forget / configure -----------------------------------------------------

async def test_forget_deletes_only_the_named_probes(client, spy, prefix):
    store = _store(spy, prefix, result_ttl_seconds=0)
    for name in ("keep", "drop", "other"):
        await store.set_latest(_result(name=name))
        await store.append_history(_result(name=name))
    await store.append_alert(_alert(probe="drop"))
    await store.forget({"drop"})
    assert sorted(await store.get_all_latest()) == ["keep", "other"]
    assert sorted(await store.get_history()) == ["keep", "other"]
    assert await client.exists(f"{prefix}:latest:drop", f"{prefix}:history:drop") == 0
    assert await client.sismember(f"{prefix}:probes", "drop") in (0, False)
    assert [a.probe for a in await store.get_alerts()] == ["drop"]  # alert log is left alone
    before = spy.round_trips
    await store.forget(set())  # nothing named: no round trip
    assert spy.round_trips == before
    await store.forget({"never-written"})  # unknown name: no error
    assert sorted(await store.get_all_latest()) == ["keep", "other"]


def _redis_app(spy, prefix, *names):
    registry = HealthRegistry(FastAPI(), storage=_store(spy, prefix), poll_interval_ms=None)
    for name in names:
        registry.add(NoOpProbe(name=name))
    return registry


async def _stored_names(store):
    return set(await store.get_all_latest()) | set(await store.get_history())


async def test_apps_sharing_a_prefix_keep_each_others_data(spy, prefix):
    one = _redis_app(spy, prefix, "orders-db", "orders-cache")
    two = _redis_app(spy, prefix, "billing-db")
    await one.run_all()
    await two.run_all()
    await one._forget_unregistered()
    await two._forget_unregistered()
    assert await _stored_names(_store(spy, prefix)) == {"orders-db", "orders-cache", "billing-db"}


async def test_rolling_deploy_keeps_both_probe_sets(spy, prefix):
    old = _redis_app(spy, prefix, "a", "b")  # old pod
    new = _redis_app(spy, prefix, "b", "c")  # new pod, same prefix
    await old.run_all()
    await new.run_all()
    for registry in (old, new, old, new):
        await registry._forget_unregistered()
    assert await _stored_names(_store(spy, prefix)) == {"a", "b", "c"}


async def test_registry_forgets_a_probe_it_stopped_registering(spy, prefix):
    one = _redis_app(spy, prefix, "a", "gone")
    other = _redis_app(spy, prefix, "x")
    await one.run_all()
    await other.run_all()
    await one._flush_writes()  # writes to Redis land in the background
    await other._flush_writes()
    one._probes = [(p, c) for p, c in one._probes if p.name != "gone"]
    await one._forget_unregistered()
    assert await _stored_names(_store(spy, prefix)) == {"a", "x"}
    assert one._written_names == {"a"}


async def test_configure_applies_new_limits(client, spy, prefix):
    store = _store(spy, prefix, max_results=10, max_alerts=10)
    base = datetime.now(timezone.utc)
    for i in range(10):
        await store.append_history(_result(latency_ms=float(i), at=base + timedelta(milliseconds=i)))
        await store.append_alert(_alert(probe=f"p{i}", at=base + timedelta(milliseconds=i)))
    store.configure(max_results=3, result_ttl_seconds=0, alert_ttl_seconds=0, max_alerts=2)
    # Reads honour the new caps straight away ...
    assert [r.latency_ms for r in (await store.get_history())["db"]] == [7.0, 8.0, 9.0]
    assert [a.probe for a in await store.get_alerts()] == ["p8", "p9"]
    # ... and the next write trims storage to them, with no expiry for TTL 0.
    await store.append_history(_result(latency_ms=10.0, at=base + timedelta(seconds=1)))
    await store.append_alert(_alert(probe="p10", at=base + timedelta(seconds=1)))
    assert await client.zcard(f"{prefix}:history:db") == 3
    assert await client.zcard(f"{prefix}:alerts") == 2
    assert await client.pttl(f"{prefix}:history:db") > 0  # expiry from the earlier TTL stays until it lapses


async def test_configure_rejects_negative_ttl(spy, prefix):
    with pytest.raises(ValueError):
        _store(spy, prefix).configure(max_results=1, result_ttl_seconds=-1, alert_ttl_seconds=0, max_alerts=1)


# --- clear_latest -----------------------------------------------------------

async def test_clear_latest_inside_running_loop(spy, prefix):
    store = _store(spy, prefix)
    await store.set_latest(_result())
    await store.append_history(_result())
    store.clear_latest()  # sync call while the loop runs: must not raise
    assert store._pending  # strong ref held until done
    await asyncio.gather(*store._pending)
    await asyncio.sleep(0)
    assert not store._pending
    assert await store.get_all_latest() == {}
    assert "db" in await store.get_history()


def test_clear_latest_without_a_loop_then_reuse_the_client():
    if not REDIS_URL:
        pytest.skip("FASTAPI_WATCH_REDIS_URL not set")
    prefix = f"fw-test-{uuid.uuid4().hex[:8]}"
    client = redis_asyncio.from_url(REDIS_URL)

    async def setup():
        try:
            await client.ping()
        except Exception as exc:
            pytest.skip(f"Redis at {REDIS_URL} unreachable: {exc}")
        await store.set_latest(_result())
        await client.connection_pool.disconnect()

    async def check_and_cleanup():
        try:
            return await store.get_all_latest()
        finally:
            await store.forget({"db"})
            await client.aclose()

    store = RedisProbeStorage(client, prefix=prefix)
    asyncio.run(setup())
    store.clear_latest()  # no loop running: runs to completion
    # The client must still work on a new loop afterwards.
    assert asyncio.run(check_and_cleanup()) == {}


async def test_checked_at_none_falls_back_to_now(spy, prefix):
    store = _store(spy, prefix, result_ttl_seconds=60)
    result = ProbeResult(name="db", status=ProbeStatus.HEALTHY)
    started = time.time()
    await store.append_history(result)
    await store.set_latest(result)
    assert (await store.get_history())["db"] == [result]
    assert await store.get_latest("db") == result
    assert time.time() - started < 5


async def test_registry_serves_from_redis(spy, prefix):
    app = FastAPI()
    registry = HealthRegistry(app, storage=_store(spy, prefix), poll_interval_ms=None)
    registry.add(NoOpProbe(name="noop"))
    await registry.run_all()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        history = (await http.get("/health/history")).json()["probes"]
        status = (await http.get("/health/status")).json()
    assert [r["status"] for r in history["noop"]] == ["healthy"]
    assert status["status"] == "healthy"


# --- claim_alert (cross-replica alert dedupe) -------------------------------

async def test_claim_alert_only_one_replica_wins(client, prefix):
    one = RedisProbeStorage(client, prefix=prefix)
    two = RedisProbeStorage(client, prefix=prefix)
    results = await asyncio.gather(*(s.claim_alert("db:unhealthy", 30) for s in (one, two, one, two)))
    assert sorted(results) == [False, False, False, True]
    assert await two.claim_alert("db:healthy", 30) is True  # different key, own claim


async def test_claim_alert_expires_and_always_has_ttl(client, spy, prefix):
    store = _store(spy, prefix)
    assert await store.claim_alert("db:unhealthy", 0.2) is True
    pttl = await client.pttl(f"{prefix}:alert-claim:db:unhealthy")
    assert 0 < pttl <= 200
    assert await store.claim_alert("db:unhealthy", 0.2) is False
    await asyncio.sleep(0.3)
    assert await store.claim_alert("db:unhealthy", 0.2) is True
    for key in await _keys(client, prefix):
        assert await client.pttl(key) > 0, key


async def test_claim_alert_non_positive_ttl_stores_nothing(client, spy, prefix):
    store = _store(spy, prefix)
    for ttl in (0, -5):
        assert await store.claim_alert("db:unhealthy", ttl) is True
        assert await store.claim_alert("db:unhealthy", ttl) is True
    assert await _keys(client, prefix) == []
