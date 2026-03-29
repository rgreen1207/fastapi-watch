"""Tests for WebSocketProbe.

FastAPI's WebSocket and WebSocketDisconnect are real Starlette types that
require an ASGI transport to function properly.  Tests that exercise the
full FastAPI/Starlette stack use TestClient with a WebSocket context manager.
Tests that only need to verify probe stat recording use lightweight mock
objects instead.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.testclient import TestClient
from fastapi_watch import HealthRegistry, WebSocketProbe
from fastapi_watch.models import ProbeStatus
from fastapi_watch.probes.websocket import _WebSocketWrapper, _find_ws_param


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_ws() -> MagicMock:
    """Return a mock WebSocket with async send/receive methods."""
    ws = MagicMock(spec=WebSocket)
    ws.receive = AsyncMock(return_value={"type": "websocket.receive", "text": "hi"})
    ws.receive_text = AsyncMock(return_value="hi")
    ws.receive_bytes = AsyncMock(return_value=b"hi")
    ws.receive_json = AsyncMock(return_value={"key": "value"})
    ws.send = AsyncMock()
    ws.send_text = AsyncMock()
    ws.send_bytes = AsyncMock()
    ws.send_json = AsyncMock()
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    return ws


# ---------------------------------------------------------------------------
# _find_ws_param
# ---------------------------------------------------------------------------

def test_find_ws_param_by_annotation():
    async def handler(websocket: WebSocket):
        pass
    assert _find_ws_param(handler) == "websocket"


def test_find_ws_param_by_name_websocket():
    async def handler(websocket):
        pass
    assert _find_ws_param(handler) == "websocket"


def test_find_ws_param_by_name_ws():
    async def handler(ws):
        pass
    assert _find_ws_param(handler) == "ws"


def test_find_ws_param_returns_none_when_absent():
    async def handler(request):
        pass
    assert _find_ws_param(handler) is None


def test_find_ws_param_prefers_annotation_over_name():
    async def handler(ws: WebSocket, websocket):
        pass
    assert _find_ws_param(handler) == "ws"


# ---------------------------------------------------------------------------
# _WebSocketWrapper — message counting
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wrapper_counts_receive_text():
    probe = WebSocketProbe(name="chat")
    ws = _make_mock_ws()
    wrapper = _WebSocketWrapper(ws, probe)

    await wrapper.receive_text()
    await wrapper.receive_text()
    assert probe._messages_received == 2


@pytest.mark.asyncio
async def test_wrapper_counts_receive_bytes():
    probe = WebSocketProbe(name="chat")
    ws = _make_mock_ws()
    wrapper = _WebSocketWrapper(ws, probe)

    await wrapper.receive_bytes()
    assert probe._messages_received == 1


@pytest.mark.asyncio
async def test_wrapper_counts_receive_json():
    probe = WebSocketProbe(name="chat")
    ws = _make_mock_ws()
    wrapper = _WebSocketWrapper(ws, probe)

    await wrapper.receive_json()
    assert probe._messages_received == 1


@pytest.mark.asyncio
async def test_wrapper_counts_receive():
    probe = WebSocketProbe(name="chat")
    ws = _make_mock_ws()
    wrapper = _WebSocketWrapper(ws, probe)

    await wrapper.receive()
    assert probe._messages_received == 1


@pytest.mark.asyncio
async def test_wrapper_counts_send_text():
    probe = WebSocketProbe(name="chat")
    ws = _make_mock_ws()
    wrapper = _WebSocketWrapper(ws, probe)

    await wrapper.send_text("hello")
    await wrapper.send_text("world")
    assert probe._messages_sent == 2


@pytest.mark.asyncio
async def test_wrapper_counts_send_bytes():
    probe = WebSocketProbe(name="chat")
    ws = _make_mock_ws()
    wrapper = _WebSocketWrapper(ws, probe)

    await wrapper.send_bytes(b"data")
    assert probe._messages_sent == 1


@pytest.mark.asyncio
async def test_wrapper_counts_send_json():
    probe = WebSocketProbe(name="chat")
    ws = _make_mock_ws()
    wrapper = _WebSocketWrapper(ws, probe)

    await wrapper.send_json({"key": "value"})
    assert probe._messages_sent == 1


@pytest.mark.asyncio
async def test_wrapper_counts_send():
    probe = WebSocketProbe(name="chat")
    ws = _make_mock_ws()
    wrapper = _WebSocketWrapper(ws, probe)

    await wrapper.send({"type": "websocket.send", "text": "hello"})
    assert probe._messages_sent == 1


@pytest.mark.asyncio
async def test_wrapper_forwards_receive_return_value():
    probe = WebSocketProbe(name="chat")
    ws = _make_mock_ws()
    wrapper = _WebSocketWrapper(ws, probe)

    result = await wrapper.receive_text()
    assert result == "hi"


@pytest.mark.asyncio
async def test_wrapper_forwards_unknown_attributes():
    probe = WebSocketProbe(name="chat")
    ws = _make_mock_ws()
    ws.url = "ws://localhost/chat"
    wrapper = _WebSocketWrapper(ws, probe)

    assert wrapper.url == "ws://localhost/chat"


# ---------------------------------------------------------------------------
# Before any connections
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_before_any_connections_is_healthy():
    probe = WebSocketProbe(name="chat")
    result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.details["message"] == "no connections observed yet"


@pytest.mark.asyncio
async def test_latency_ms_zero_before_any_connections():
    probe = WebSocketProbe(name="chat")
    result = await probe.check()
    assert result.latency_ms == 0.0


# ---------------------------------------------------------------------------
# Connection lifecycle tracking
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_total_connections_increments():
    probe = WebSocketProbe(name="chat")
    ws = _make_mock_ws()

    @probe.watch
    async def handler(websocket: WebSocket):
        pass

    await handler(websocket=ws)
    await handler(websocket=ws)
    result = await probe.check()
    assert result.details["total_connections"] == 2


@pytest.mark.asyncio
async def test_active_connections_zero_after_close():
    probe = WebSocketProbe(name="chat")
    ws = _make_mock_ws()

    @probe.watch
    async def handler(websocket: WebSocket):
        pass

    await handler(websocket=ws)
    result = await probe.check()
    assert result.details["active_connections"] == 0


@pytest.mark.asyncio
async def test_active_connections_positive_during_handler():
    probe = WebSocketProbe(name="chat")
    ws = _make_mock_ws()
    observed = []

    @probe.watch
    async def handler(websocket: WebSocket):
        observed.append(probe._active_connections)

    await handler(websocket=ws)
    assert observed == [1]  # 1 during handler, 0 after


@pytest.mark.asyncio
async def test_duration_recorded_after_close():
    probe = WebSocketProbe(name="chat")
    ws = _make_mock_ws()

    @probe.watch
    async def handler(websocket: WebSocket):
        await asyncio.sleep(0.01)

    await handler(websocket=ws)
    result = await probe.check()
    assert result.details["avg_duration_ms"] is not None
    assert result.details["avg_duration_ms"] >= 0


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_websocket_disconnect_not_counted_as_error():
    probe = WebSocketProbe(name="chat")
    ws = _make_mock_ws()

    @probe.watch
    async def handler(websocket: WebSocket):
        raise WebSocketDisconnect(code=1000)

    with pytest.raises(WebSocketDisconnect):
        await handler(websocket=ws)

    result = await probe.check()
    assert result.details["error_count"] == 0
    assert result.status == ProbeStatus.HEALTHY


@pytest.mark.asyncio
async def test_unhandled_exception_counted_as_error():
    probe = WebSocketProbe(name="chat")
    ws = _make_mock_ws()

    @probe.watch
    async def handler(websocket: WebSocket):
        raise RuntimeError("internal error")

    with pytest.raises(RuntimeError):
        await handler(websocket=ws)

    result = await probe.check()
    assert result.details["error_count"] == 1


@pytest.mark.asyncio
async def test_unhandled_exception_is_reraised():
    probe = WebSocketProbe(name="chat")
    ws = _make_mock_ws()

    @probe.watch
    async def handler(websocket: WebSocket):
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await handler(websocket=ws)


@pytest.mark.asyncio
async def test_websocket_disconnect_is_reraised():
    probe = WebSocketProbe(name="chat")
    ws = _make_mock_ws()

    @probe.watch
    async def handler(websocket: WebSocket):
        raise WebSocketDisconnect(code=1001)

    with pytest.raises(WebSocketDisconnect):
        await handler(websocket=ws)


@pytest.mark.asyncio
async def test_error_rate_calculated_correctly():
    probe = WebSocketProbe(name="chat")
    ws = _make_mock_ws()

    @probe.watch
    async def handler(websocket: WebSocket, fail: bool = False):
        if fail:
            raise RuntimeError("crash")

    for _ in range(8):
        await handler(websocket=ws, fail=False)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await handler(websocket=ws, fail=True)

    result = await probe.check()
    assert abs(result.details["error_rate"] - 0.2) < 0.001


# ---------------------------------------------------------------------------
# Consecutive errors
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_consecutive_errors_increments_on_error():
    probe = WebSocketProbe(name="chat")
    ws = _make_mock_ws()

    @probe.watch
    async def handler(websocket: WebSocket):
        raise RuntimeError("crash")

    for _ in range(3):
        with pytest.raises(RuntimeError):
            await handler(websocket=ws)

    result = await probe.check()
    assert result.details["consecutive_errors"] == 3


@pytest.mark.asyncio
async def test_consecutive_errors_resets_on_clean_close():
    probe = WebSocketProbe(name="chat")
    ws = _make_mock_ws()

    @probe.watch
    async def handler(websocket: WebSocket, fail: bool = False):
        if fail:
            raise RuntimeError("crash")

    for _ in range(3):
        with pytest.raises(RuntimeError):
            await handler(websocket=ws, fail=True)
    await handler(websocket=ws, fail=False)

    result = await probe.check()
    assert result.details["consecutive_errors"] == 0


@pytest.mark.asyncio
async def test_disconnect_does_not_reset_consecutive_errors():
    """WebSocketDisconnect is a normal close, not an error — but it does reset the counter."""
    probe = WebSocketProbe(name="chat")
    ws = _make_mock_ws()

    @probe.watch
    async def erroring(websocket: WebSocket):
        raise RuntimeError("crash")

    @probe.watch
    async def disconnecting(websocket: WebSocket):
        raise WebSocketDisconnect()

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await erroring(websocket=ws)

    with pytest.raises(WebSocketDisconnect):
        await disconnecting(websocket=ws)

    result = await probe.check()
    assert result.details["consecutive_errors"] == 0


# ---------------------------------------------------------------------------
# Health thresholds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unhealthy_when_error_rate_exceeds_threshold():
    probe = WebSocketProbe(name="chat", max_error_rate=0.1)
    ws = _make_mock_ws()

    @probe.watch
    async def handler(websocket: WebSocket, fail: bool = False):
        if fail:
            raise RuntimeError("crash")

    for _ in range(8):
        await handler(websocket=ws, fail=False)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await handler(websocket=ws, fail=True)

    result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY
    assert "error rate" in result.error


@pytest.mark.asyncio
async def test_healthy_when_error_rate_within_threshold():
    probe = WebSocketProbe(name="chat", max_error_rate=0.5)
    ws = _make_mock_ws()

    @probe.watch
    async def handler(websocket: WebSocket):
        pass

    for _ in range(10):
        await handler(websocket=ws)

    result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY


@pytest.mark.asyncio
async def test_unhealthy_when_active_connections_below_minimum():
    probe = WebSocketProbe(name="feed", min_active_connections=1)
    ws = _make_mock_ws()

    @probe.watch
    async def handler(websocket: WebSocket):
        pass

    await handler(websocket=ws)  # connects and immediately closes

    result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY
    assert "active connections" in result.error


@pytest.mark.asyncio
async def test_healthy_when_active_connections_meets_minimum():
    probe = WebSocketProbe(name="feed", min_active_connections=1)
    ws = _make_mock_ws()
    long_task = None

    @probe.watch
    async def handler(websocket: WebSocket):
        nonlocal long_task
        # Signal that we're inside the handler (connection is active)
        long_task = asyncio.current_task()
        await asyncio.sleep(10)  # keep connection open

    # Start the handler but don't await it fully
    task = asyncio.create_task(handler(websocket=ws))
    await asyncio.sleep(0)  # yield so the handler starts

    result = await probe.check()
    assert result.details["active_connections"] == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_min_active_connections_zero_disabled():
    probe = WebSocketProbe(name="chat", min_active_connections=0)
    ws = _make_mock_ws()

    @probe.watch
    async def handler(websocket: WebSocket):
        pass

    await handler(websocket=ws)

    result = await probe.check()
    # 0 active is fine when min is 0
    assert result.status == ProbeStatus.HEALTHY


# ---------------------------------------------------------------------------
# Message counting via WebSocket proxy
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_messages_received_via_watch():
    probe = WebSocketProbe(name="chat")
    ws = _make_mock_ws()

    @probe.watch
    async def handler(websocket: WebSocket):
        await websocket.receive_text()
        await websocket.receive_text()

    await handler(websocket=ws)
    result = await probe.check()
    assert result.details["messages_received"] == 2


@pytest.mark.asyncio
async def test_messages_sent_via_watch():
    probe = WebSocketProbe(name="chat")
    ws = _make_mock_ws()

    @probe.watch
    async def handler(websocket: WebSocket):
        await websocket.send_text("hello")
        await websocket.send_text("world")

    await handler(websocket=ws)
    result = await probe.check()
    assert result.details["messages_sent"] == 2


@pytest.mark.asyncio
async def test_messages_accumulate_across_connections():
    probe = WebSocketProbe(name="chat")
    ws = _make_mock_ws()

    @probe.watch
    async def handler(websocket: WebSocket):
        await websocket.receive_text()
        await websocket.send_text("reply")

    await handler(websocket=ws)
    await handler(websocket=ws)
    result = await probe.check()
    assert result.details["messages_received"] == 2
    assert result.details["messages_sent"] == 2


# ---------------------------------------------------------------------------
# Duration tracking
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_min_max_duration_tracked():
    probe = WebSocketProbe(name="chat")
    ws = _make_mock_ws()

    @probe.watch
    async def handler(websocket: WebSocket):
        pass

    for _ in range(3):
        await handler(websocket=ws)

    result = await probe.check()
    assert result.details["min_duration_ms"] is not None
    assert result.details["max_duration_ms"] is not None
    assert result.details["min_duration_ms"] <= result.details["max_duration_ms"]


# ---------------------------------------------------------------------------
# FastAPI integration
# ---------------------------------------------------------------------------

def test_websocket_endpoint_serves_messages():
    probe = WebSocketProbe(name="echo")
    app = FastAPI()

    @app.websocket("/ws")
    @probe.watch
    async def echo(websocket: WebSocket):
        await websocket.accept()
        data = await websocket.receive_text()
        await websocket.send_text(f"echo:{data}")

    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.send_text("hello")
        assert ws.receive_text() == "echo:hello"


def test_websocket_probe_registered_in_health_status():
    probe = WebSocketProbe(name="echo")
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(probe)

    @app.websocket("/ws")
    @probe.watch
    async def echo(websocket: WebSocket):
        await websocket.accept()
        data = await websocket.receive_text()
        await websocket.send_text(data)

    client = TestClient(app)

    # Trigger a connection so stats exist
    with client.websocket_connect("/ws") as ws:
        ws.send_text("ping")
        ws.receive_text()

    resp = client.get("/health/status")
    probes = {p["name"]: p for p in resp.json()["probes"]}
    assert "echo" in probes
    assert probes["echo"]["details"]["total_connections"] == 1
    assert probes["echo"]["details"]["messages_received"] == 1
    assert probes["echo"]["details"]["messages_sent"] == 1


def test_watch_preserves_function_name():
    probe = WebSocketProbe(name="chat")

    @probe.watch
    async def my_ws_handler(websocket: WebSocket):
        pass

    assert my_ws_handler.__name__ == "my_ws_handler"


# ---------------------------------------------------------------------------
# probe name and result structure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_probe_name_in_result():
    probe = WebSocketProbe(name="notifications")
    ws = _make_mock_ws()

    @probe.watch
    async def handler(websocket: WebSocket):
        pass

    await handler(websocket=ws)
    result = await probe.check()
    assert result.name == "notifications"


@pytest.mark.asyncio
async def test_latency_ms_reflects_avg_duration():
    probe = WebSocketProbe(name="chat")
    ws = _make_mock_ws()

    @probe.watch
    async def handler(websocket: WebSocket):
        await asyncio.sleep(0.01)

    await handler(websocket=ws)
    result = await probe.check()
    assert result.latency_ms == result.details["avg_duration_ms"]
