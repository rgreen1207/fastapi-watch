"""SSE stream behaviour: keepalives, event cadence, single-fetch, cancellation."""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import fastapi_watch.registry as registry_module
from fastapi_watch import HealthRegistry
from fastapi_watch.probes.noop import NoOpProbe


def _leaf_exceptions(exc: BaseException) -> list[BaseException]:
    """*exc* itself, or every exception nested inside it if it is an exception group."""
    nested = getattr(exc, "exceptions", None)
    if nested is None:
        return [exc]
    return [leaf for sub in nested for leaf in _leaf_exceptions(sub)]


class _FakeRequest:
    """Stands in for a Starlette Request: connected until told otherwise."""

    def __init__(self):
        self.gone = False

    async def is_disconnected(self) -> bool:
        return self.gone


def _make_report(results) -> str:
    return str(len(results))


def test_single_fetch_sends_retry_hint_with_its_one_event():
    app = FastAPI()
    HealthRegistry(app, poll_interval_ms=None).add(NoOpProbe(name="a"))
    body = TestClient(app).get("/health/status/stream").text

    assert body.startswith("retry: 30000\ndata: ")
    assert body.count("data: ") == 1


def test_stream_interval_is_the_shortest_probe_interval():
    registry = HealthRegistry(FastAPI(), poll_interval_ms=60_000)
    fast = NoOpProbe(name="fast", poll_interval_ms=5_000)
    registry.add(NoOpProbe(name="slow")).add(fast)
    assert registry._stream_interval_s() == 5.0


def test_custom_intervals_keep_the_stream_open_without_a_global_interval():
    registry = HealthRegistry(FastAPI(), poll_interval_ms=None)
    registry.add(NoOpProbe(name="a", poll_interval_ms=2_000))
    assert registry._stream_interval_s() == 2.0


def test_all_single_fetch_has_no_stream_interval():
    registry = HealthRegistry(FastAPI(), poll_interval_ms=None)
    registry.add(NoOpProbe(name="a"))
    assert registry._stream_interval_s() is None


def test_probe_interval_below_minimum_is_clamped():
    registry = HealthRegistry(FastAPI(), poll_interval_ms=60_000)
    registry.add(NoOpProbe(name="a", poll_interval_ms=10))
    assert registry._stream_interval_s() == 1.0


@pytest.mark.asyncio
async def test_keepalive_comment_sent_between_events(monkeypatch):
    monkeypatch.setattr(registry_module, "_SSE_KEEPALIVE_S", 0.01)
    registry = HealthRegistry(FastAPI(), poll_interval_ms=60_000)
    registry.add(NoOpProbe(name="a"))
    request = _FakeRequest()
    stream = registry._event_stream(request, _make_report)
    try:
        assert (await stream.__anext__()).startswith("data: ")
        assert await asyncio.wait_for(stream.__anext__(), 2) == ": keepalive\n\n"
    finally:
        request.gone = True
        await stream.aclose()


@pytest.mark.asyncio
async def test_disconnect_ends_the_stream_and_cleans_up():
    registry = HealthRegistry(FastAPI(), poll_interval_ms=60_000)
    registry.add(NoOpProbe(name="a"))
    request = _FakeRequest()
    chunks = []

    async def consume():
        async for chunk in registry._event_stream(request, _make_report):
            chunks.append(chunk)
            request.gone = True

    await asyncio.wait_for(consume(), 2)
    assert len(chunks) == 1
    assert registry._active_connections == 0
    registry._cancel_poll_task()


@pytest.mark.asyncio
async def test_cancellation_propagates_after_cleanup():
    registry = HealthRegistry(FastAPI(), poll_interval_ms=60_000)
    registry.add(NoOpProbe(name="a"))
    started = asyncio.Event()

    async def consume():
        async for _ in registry._event_stream(_FakeRequest(), _make_report):
            started.set()

    task = asyncio.create_task(consume())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert registry._active_connections == 0
    assert not registry._sse_tasks
    registry._cancel_poll_task()


# ---------------------------------------------------------------------------
# max_stream_connections
# ---------------------------------------------------------------------------

def _single_fetch_app(**kwargs):
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None, **kwargs)
    registry.add(NoOpProbe(name="a"))
    return app, registry


def test_default_stream_cap_is_100():
    _, registry = _single_fetch_app()
    assert registry._max_stream_connections == 100


@pytest.mark.parametrize("path", ["/health/status/stream", "/health/ready/stream"])
def test_streams_beyond_the_cap_get_503_with_retry_after(path):
    app, registry = _single_fetch_app(max_stream_connections=2)
    held = [registry._reserve_stream_slot(), registry._reserve_stream_slot()]
    client = TestClient(app)

    resp = client.get(path)
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "30"
    assert resp.json() == {"error": "too many stream connections"}
    assert registry._active_connections == 0  # the generator never started

    held[0]()
    assert client.get(path).status_code == 200


def test_slot_is_held_while_streaming_and_released_after():
    app, registry = _single_fetch_app(max_stream_connections=5)
    seen = []

    async def _fake_stream(request, make_report, **kwargs):
        seen.append(registry._stream_slots)
        yield "data: {}\n\n"

    registry._event_stream = _fake_stream
    client = TestClient(app)
    for _ in range(3):
        assert client.get("/health/status/stream").status_code == 200
    assert seen == [1, 1, 1]
    assert registry._stream_slots == 0


def test_unlimited_streams_with_none():
    app, registry = _single_fetch_app(max_stream_connections=None)
    for _ in range(150):
        registry._reserve_stream_slot()
    assert TestClient(app).get("/health/status/stream").status_code == 200


def test_release_is_idempotent():
    _, registry = _single_fetch_app(max_stream_connections=1)
    release = registry._reserve_stream_slot()
    release()
    release()
    assert registry._stream_slots == 0


@pytest.mark.asyncio
async def test_slot_released_when_the_response_fails_before_streaming():
    released = []

    async def _never_iterated():
        yield "data: {}\n\n"

    response = registry_module._SlotStreamingResponse(_never_iterated(), release=lambda: released.append(1))

    async def _send(message):
        raise OSError("client gone")

    async def _receive():
        return {"type": "http.disconnect"}

    with pytest.raises(Exception) as raised:
        await response({"type": "http", "asgi": {"spec_version": "2.0"}}, _receive, _send)
    # Starlette < 0.28 streams in an anyio task group, which wraps the error in an ExceptionGroup.
    assert any(isinstance(e, OSError) for e in _leaf_exceptions(raised.value))
    assert released == [1]


def test_cap_applies_after_auth():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None, auth={"type": "apikey", "key": "k"}, max_stream_connections=1)
    registry._reserve_stream_slot()
    client = TestClient(app)
    assert client.get("/health/status/stream").status_code == 403
    assert client.get("/health/status/stream", headers={"X-API-Key": "k"}).status_code == 503


@pytest.mark.parametrize("value", [0, -5])
def test_invalid_stream_cap_is_rejected(value):
    with pytest.raises(ValueError, match="max_stream_connections"):
        HealthRegistry(FastAPI(), max_stream_connections=value)


@pytest.mark.asyncio
async def test_streams_share_single_fetch_results_within_an_interval():
    # Each open stream used to re-run every single-fetch probe at its own
    # event tick: N streams, N checks of that dependency per interval.
    class _Counting(NoOpProbe):
        calls = 0

        async def check(self):
            self.calls += 1
            return await super().check()

    registry = HealthRegistry(FastAPI(), poll_interval_ms=None)
    single = _Counting(name="db")
    registry.add(single).add(NoOpProbe(name="fast", poll_interval_ms=1000))

    async def _client(delay):
        await asyncio.sleep(delay)
        async for _ in registry._event_stream(_FakeRequest(), _make_report):
            pass

    clients = [asyncio.create_task(_client(i * 0.05)) for i in range(10)]
    await asyncio.sleep(2.2)
    for task in clients:
        task.cancel()
    await asyncio.gather(*clients, return_exceptions=True)
    await registry._shutdown()
    assert single.calls <= 4  # about one per 1 s interval, not one per stream event (~30)


@pytest.mark.asyncio
async def test_requests_still_run_single_fetch_probes_fresh():
    class _Counting(NoOpProbe):
        calls = 0

        async def check(self):
            self.calls += 1
            return await super().check()

    registry = HealthRegistry(FastAPI(), poll_interval_ms=None)
    probe = _Counting(name="db")
    registry.add(probe).add(NoOpProbe(name="fast", poll_interval_ms=1000))
    for _ in range(3):
        await registry._get_results()
    assert probe.calls == 3
