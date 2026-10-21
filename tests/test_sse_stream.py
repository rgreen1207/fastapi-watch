"""SSE stream behaviour: keepalives, event cadence, single-fetch, cancellation."""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import fastapi_watch.registry as registry_module
from fastapi_watch import HealthRegistry
from fastapi_watch.probes.noop import NoOpProbe


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
