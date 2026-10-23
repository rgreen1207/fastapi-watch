"""A probe raising TimeoutError itself is a failure, not the registry's deadline.

Since Python 3.11 ``asyncio.TimeoutError`` is the builtin ``TimeoutError``
that socket and client libraries raise.  With ``probe_timeout=None`` the
registry formatted the missing deadline and every health endpoint answered
500; with a deadline the error claimed the probe ran for its full timeout.
"""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi_watch import HealthRegistry
from fastapi_watch.models import ProbeResult
from fastapi_watch.probes.base import BaseProbe


class _RaisesTimeout(BaseProbe):
    name = "raises_timeout"

    async def check(self) -> ProbeResult:
        raise TimeoutError("connect timed out")


class _Hangs(BaseProbe):
    name = "hangs"

    async def check(self) -> ProbeResult:
        await asyncio.sleep(10)


@pytest.mark.parametrize("probe_timeout", [None, 30.0])
@pytest.mark.parametrize("path", ["/health/ready", "/health/status", "/health/metrics", "/health/dashboard"])
def test_endpoints_report_the_failure(probe_timeout, path):
    app = FastAPI()
    HealthRegistry(app, probe_timeout=probe_timeout).add(_RaisesTimeout())
    response = TestClient(app, raise_server_exceptions=False).get(path)
    assert response.status_code in (200, 207, 503)


@pytest.mark.parametrize("probe_timeout", [None, 30.0])
async def test_error_names_the_exception_not_the_deadline(probe_timeout):
    registry = HealthRegistry(FastAPI(), probe_timeout=probe_timeout)
    registry.add(_RaisesTimeout())
    [result] = await registry.run_all()
    assert result.status == "unhealthy"
    assert result.error == "probe check failed: TimeoutError"


async def test_a_real_deadline_is_still_reported_as_one():
    registry = HealthRegistry(FastAPI(), probe_timeout=0.05)
    registry.add(_Hangs())
    [result] = await registry.run_all()
    assert result.error == "probe timed out after 0.05 s"


async def test_startup_probe_raising_timeout_answers_503():
    app = FastAPI()
    registry = HealthRegistry(app, probe_timeout=None, startup_probes=[_RaisesTimeout()])
    registry.set_started()
    response = TestClient(app, raise_server_exceptions=False).get("/health/startup")
    assert response.status_code == 503


async def test_own_timeout_after_the_deadline_would_have_passed_is_not_the_deadline(monkeypatch):
    """Classified by whether the deadline fired, not by elapsed time.

    On a coarse clock (Windows: ~15.6 ms) elapsed time can disagree with the
    timer; here the loop clock jumps past the deadline before the probe
    raises its own TimeoutError, which must still be reported as its own.
    """
    class _SlowThenRaises(BaseProbe):
        name = "slow_then_raises"
        timeout = 5.0

        async def check(self) -> ProbeResult:
            loop = asyncio.get_running_loop()
            real_time = loop.time
            # The clock jumps past the deadline and stays there while the
            # registry classifies the error (restored at teardown).
            monkeypatch.setattr(loop, "time", lambda: real_time() + 10)
            raise TimeoutError("read timed out")

    registry = HealthRegistry(FastAPI())
    registry.add(_SlowThenRaises())
    [result] = await registry.run_all()
    assert result.error == "probe check failed: TimeoutError"


async def test_cancelling_the_caller_cancels_the_check():
    started, cancelled = asyncio.Event(), asyncio.Event()

    class _Waits(BaseProbe):
        name = "waits"
        timeout = 30.0

        async def check(self) -> ProbeResult:
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

    registry = HealthRegistry(FastAPI())
    task = asyncio.ensure_future(registry._guarded_check(_Waits(), True))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


async def test_cancelled_caller_returns_only_after_the_check_unwinds():
    """As wait_for: cleanup in the check finishes before the caller sees CancelledError."""
    started, events = asyncio.Event(), []

    class _SlowCleanup(BaseProbe):
        name = "slow_cleanup"
        timeout = 30.0

        async def check(self) -> ProbeResult:
            started.set()
            try:
                await asyncio.sleep(30)
            finally:
                await asyncio.sleep(0.05)  # e.g. closing a connection
                events.append("check unwound")

    registry = HealthRegistry(FastAPI())
    task = asyncio.ensure_future(registry._guarded_check(_SlowCleanup(), True))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    events.append("caller returned")
    assert events == ["check unwound", "caller returned"]
