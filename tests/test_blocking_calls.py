"""Blocking checks run in fastapi-watch's own bounded pool, one call in flight per probe."""
import asyncio
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from fastapi_watch import HealthRegistry, alerts
from fastapi_watch._executor import (
    _MAX_WORKERS,
    SingleFlight,
    StillRunning,
    _DaemonThreadPool,
    _get_pool,
    run_blocking,
)
from fastapi_watch.alerts import OpsGenieAlerter, WebhookAlerter
from fastapi_watch.models import AlertRecord, ProbeStatus
from fastapi_watch.probes import CeleryProbe, TCPProbe


def _threads(prefix: str) -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith(prefix)]


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# The pool
# ---------------------------------------------------------------------------

async def test_run_blocking_uses_a_bounded_pool_of_named_daemon_threads():
    release = threading.Event()
    names: list[str] = []

    def work():
        names.append(threading.current_thread().name)
        release.wait(5)

    tasks = [asyncio.ensure_future(run_blocking(work)) for _ in range(_MAX_WORKERS * 3)]
    await _wait_until(lambda: len(names) >= _MAX_WORKERS)
    await asyncio.sleep(0.05)
    try:
        assert len(names) == _MAX_WORKERS  # the rest wait their turn
        pool_threads = _threads("fastapi-watch")
        assert len(pool_threads) <= _MAX_WORKERS
        assert all(t.daemon for t in pool_threads)
    finally:
        release.set()
    await asyncio.gather(*tasks)
    assert len(names) == _MAX_WORKERS * 3
    assert all(name.startswith("fastapi-watch") for name in names)


async def test_pool_is_shared_and_created_lazily():
    assert _get_pool() is _get_pool()


async def test_run_blocking_returns_results_and_raises_errors():
    assert await run_blocking(sum, [1, 2, 3]) == 6
    with pytest.raises(ZeroDivisionError):
        await run_blocking(lambda: 1 / 0)


def test_pool_shutdown_cancels_queued_work():
    pool = _DaemonThreadPool(1, "fastapi-watch-test")
    release = threading.Event()
    running = pool.submit(release.wait, 5)
    queued = pool.submit(lambda: "never")
    pool.shutdown(wait=False, cancel_futures=True)
    release.set()
    assert running.result(timeout=5) is True
    assert queued.cancelled()
    with pytest.raises(RuntimeError):
        pool.submit(lambda: None)


def test_a_hung_call_does_not_block_interpreter_exit():
    code = (
        "import asyncio, time\n"
        "from fastapi_watch._executor import run_blocking\n"
        "async def main():\n"
        "    try:\n"
        "        await asyncio.wait_for(run_blocking(time.sleep, 60), 0.1)\n"
        "    except asyncio.TimeoutError:\n"
        "        pass\n"
        "asyncio.run(main())\n"
    )
    start = time.monotonic()
    subprocess.run([sys.executable, "-c", code], check=True, timeout=20)
    assert time.monotonic() - start < 15


async def test_single_flight_refuses_new_work_while_the_previous_call_runs():
    flight = SingleFlight()
    release = threading.Event()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(flight.run(release.wait, 5), 0.05)
    assert flight.busy
    calls = []
    with pytest.raises(StillRunning):
        await flight.run(calls.append, 1)
    assert calls == []
    release.set()
    await _wait_until(lambda: not flight.busy)
    await flight.run(calls.append, 1)
    assert calls == [1]


async def test_single_flight_call_cancelled_while_queued_is_not_busy():
    release = threading.Event()
    blockers = [_get_pool().submit(release.wait, 5) for _ in range(_MAX_WORKERS)]
    try:
        flight = SingleFlight()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(flight.run(lambda: None), 0.05)
        assert not flight.busy
    finally:
        release.set()
    for blocker in blockers:
        blocker.result(timeout=5)


# ---------------------------------------------------------------------------
# Probes: a hung dependency holds one thread, and the default executor is untouched
# ---------------------------------------------------------------------------

def _hung_celery_app(release: threading.Event):
    inspector = MagicMock()
    inspector.ping.side_effect = lambda: release.wait(10) and {"celery@host": {"ok": "pong"}}
    app = MagicMock()
    app.control.inspect.return_value = inspector
    return app, inspector


async def test_hung_celery_broker_holds_at_most_one_thread():
    release = threading.Event()
    app, inspector = _hung_celery_app(release)
    probe = CeleryProbe(app, min_workers=1).disable_circuit_breaker()  # run every check
    probe.timeout = 0.05
    registry = HealthRegistry(FastAPI(), poll_interval_ms=None)
    registry.add(probe)
    default_executor_threads = len(_threads("asyncio"))
    try:
        results = [(await registry.run_all())[0] for _ in range(10)]
        assert results[0].error == "probe timed out after 0.05 s"
        assert all(r.status == ProbeStatus.UNHEALTHY for r in results)
        assert {r.error for r in results[1:]} == {"previous check still running"}
        assert inspector.ping.call_count == 1  # no new work was submitted
        assert len(_threads("asyncio")) == default_executor_threads
    finally:
        release.set()
    await _wait_until(lambda: not probe._inspect_call.busy)
    [result] = await registry.run_all()
    assert result.status == ProbeStatus.HEALTHY, result.error


async def test_hung_dns_holds_at_most_one_thread():
    release = threading.Event()
    lookups = []

    def slow_getaddrinfo(*args, **kwargs):
        lookups.append(args)
        release.wait(10)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 6379))]

    writer = MagicMock()
    writer.wait_closed = AsyncMock()
    probe = TCPProbe("cache.internal", 6379, timeout=1.0)
    with (
        patch("socket.getaddrinfo", side_effect=slow_getaddrinfo),
        patch("asyncio.open_connection", new=AsyncMock(return_value=(MagicMock(), writer))),
    ):
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(probe.check(), 0.05)
            for _ in range(5):
                result = await probe.check()
                assert result.status == ProbeStatus.UNHEALTHY
                assert result.error == "previous check still running"
                assert result.details == {"host": "cache.internal", "port": 6379}
            assert len(lookups) == 1
        finally:
            release.set()
        await _wait_until(lambda: not probe._resolve_call.busy)
        assert (await probe.check()).status == ProbeStatus.HEALTHY


# ---------------------------------------------------------------------------
# TCPProbe resolves once and connects to the resolved addresses
# ---------------------------------------------------------------------------

async def test_tcp_probe_connects_to_resolved_addresses_without_resolving_again():
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    with socket.create_server(("127.0.0.1", 0)) as closed:
        refused_port = closed.getsockname()[1]
    loop = asyncio.get_running_loop()
    run_in_executor = loop.run_in_executor

    def no_default_executor(executor, *args):
        assert executor is not None, "default executor used"
        return run_in_executor(executor, *args)

    resolved = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", refused_port)),  # nothing listens here
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
    ]
    try:
        with (
            patch("socket.getaddrinfo", return_value=resolved) as getaddrinfo,
            patch.object(loop, "getaddrinfo", side_effect=AssertionError("resolved twice")),
            patch.object(loop, "run_in_executor", side_effect=no_default_executor),
        ):
            result = await TCPProbe("db.internal", port, timeout=2.0).check()
    finally:
        server.close()
        await server.wait_closed()

    assert result.status == ProbeStatus.HEALTHY, result.error
    assert getaddrinfo.call_count == 1
    assert result.details == {
        "host": "db.internal",
        "port": port,
        "resolved_ips": ["127.0.0.1"],
        "connect_ms": result.details["connect_ms"],
    }


async def test_tcp_probe_tries_each_address_and_reports_the_last_failure():
    resolved = [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 80, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80)),
    ]
    connect = AsyncMock(side_effect=[ConnectionRefusedError("v6"), ConnectionResetError("v4")])
    with patch("socket.getaddrinfo", return_value=resolved), patch("asyncio.open_connection", new=connect):
        result = await TCPProbe("db.internal", 80).check()

    assert result.status == ProbeStatus.UNHEALTHY
    assert result.error == "probe check failed: ConnectionResetError"
    assert [c.args[0] for c in connect.call_args_list] == ["::1", "127.0.0.1"]
    assert [c.kwargs["family"] for c in connect.call_args_list] == [socket.AF_INET6, socket.AF_INET]


# ---------------------------------------------------------------------------
# Alerters post from the pool, bounded by their own timeout
# ---------------------------------------------------------------------------

def _alert(new_status: ProbeStatus = ProbeStatus.UNHEALTHY) -> AlertRecord:
    return AlertRecord(probe="db", old_status=ProbeStatus.HEALTHY, new_status=new_status,
                       timestamp=datetime.now(timezone.utc))


async def test_alerters_post_from_the_watch_pool(monkeypatch):
    threads: list[str] = []
    monkeypatch.setattr(alerts, "_post_json", lambda *a, **k: threads.append(threading.current_thread().name))

    await WebhookAlerter("https://example.com/hook").notify(_alert())
    await OpsGenieAlerter(api_key="k").notify(_alert())
    await OpsGenieAlerter(api_key="k").notify(_alert(ProbeStatus.HEALTHY))

    assert len(threads) == 3
    assert all(name.startswith("fastapi-watch") for name in threads)


@pytest.fixture
def real_opener(monkeypatch):
    # conftest refuses alerter requests by default; these tests talk to a local socket.
    monkeypatch.setattr(alerts._OPENER, "open", urllib.request.OpenerDirector.open.__get__(alerts._OPENER))


def _trickling_server(stop: threading.Event) -> int:
    """Accept one request and answer it one byte every 50 ms (each read is quick)."""
    listener = socket.create_server(("127.0.0.1", 0))

    def serve():
        conn, _ = listener.accept()
        with conn, listener:
            conn.recv(65536)
            for byte in b"HTTP/1.1 200 OK\r\nX-Pad: " + b"a" * 1000:
                if stop.is_set():
                    break
                try:
                    conn.sendall(bytes([byte]))
                except OSError:
                    break
                time.sleep(0.05)

    threading.Thread(target=serve, daemon=True).start()
    return listener.getsockname()[1]


def test_slow_webhook_is_cut_off_at_its_timeout(real_opener):
    stop = threading.Event()
    port = _trickling_server(stop)
    start = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="within 0.5 s"):
            alerts._post_json(f"http://127.0.0.1:{port}/hook", b"{}", timeout=0.5)
    finally:
        stop.set()
    # Without an overall deadline every byte resets urllib's per-read timeout (~50 s here).
    assert time.monotonic() - start < 2.0


def test_fast_webhook_is_not_affected_by_the_deadline(real_opener):
    listener = socket.create_server(("127.0.0.1", 0))

    def serve():
        conn, _ = listener.accept()
        with conn, listener:
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")

    threading.Thread(target=serve, daemon=True).start()
    alerts._post_json(f"http://127.0.0.1:{listener.getsockname()[1]}/hook", b"{}", timeout=2)


def test_unreachable_addresses_share_the_webhook_deadline(real_opener, monkeypatch):
    # socket.create_connection gives every resolved address the full timeout,
    # so a host with several blackholed addresses took N x timeout.
    addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (f"192.0.2.{i}", 80)) for i in range(1, 5)]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: addresses)
    attempts: list[float] = []

    def blackholed_connect(sock, address):
        attempts.append(sock.gettimeout())
        time.sleep(sock.gettimeout())
        raise TimeoutError("timed out")

    monkeypatch.setattr(socket.socket, "connect", blackholed_connect)
    start = time.monotonic()
    with pytest.raises(TimeoutError, match="within 0.5 s"):
        alerts._post_json("http://hooks.example.com/hook", b"{}", timeout=0.5)
    assert time.monotonic() - start < 1.0  # was 4 x 0.5 s
    assert attempts and attempts[0] <= 0.5


def test_webhook_timeout_none_still_posts(real_opener):
    listener = socket.create_server(("127.0.0.1", 0))

    def serve():
        conn, _ = listener.accept()
        with conn, listener:
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")

    threading.Thread(target=serve, daemon=True).start()
    alerts._post_json(f"http://127.0.0.1:{listener.getsockname()[1]}/hook", b"{}", timeout=None)


async def test_slow_webhooks_do_not_starve_blocking_probe_checks(monkeypatch):
    # Alert posts shared the 4-thread pool with CeleryProbe / TCPProbe, so four
    # slow posts left those checks queued until they timed out.
    release = threading.Event()
    monkeypatch.setattr(alerts, "_post_json", lambda *a, **k: release.wait(5))
    posts = [asyncio.ensure_future(WebhookAlerter("https://example.com/hook").notify(_alert()))
             for _ in range(_MAX_WORKERS)]
    try:
        await asyncio.sleep(0.05)
        await asyncio.wait_for(run_blocking(lambda: None), 1.0)
    finally:
        release.set()
        await asyncio.gather(*posts)
