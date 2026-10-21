"""error_rate is judged over the last ``window_size`` outcomes, not the lifetime.

A lifetime rate lets a long healthy history hide a fresh outage and keeps an
old burst of errors on the books forever.  Each passive probe type is checked
for both directions, plus the window boundaries.
"""
import pytest
from fastapi import HTTPException, WebSocket

from fastapi_watch import FastAPIRouteProbe, FastAPIWebSocketProbe, PassiveProbe
from fastapi_watch.middleware import _RouteStats
from fastapi_watch.models import ProbeStatus

from .test_fastapi_websocket_probe import _make_mock_ws


# ---------------------------------------------------------------------------
# Drivers: record N successes or failures through each probe's real path
# ---------------------------------------------------------------------------

def _passive_driver(probe: PassiveProbe):
    @probe.watch
    async def call(fail: bool):
        if fail:
            raise RuntimeError("boom")

    async def drive(fail: bool, times: int) -> None:
        for _ in range(times):
            try:
                await call(fail)
            except RuntimeError:
                pass

    return drive


def _route_driver(probe: FastAPIRouteProbe):
    @probe.watch
    async def handler(fail: bool):
        if fail:
            raise HTTPException(status_code=500)

    async def drive(fail: bool, times: int) -> None:
        for _ in range(times):
            try:
                await handler(fail)
            except HTTPException:
                pass

    return drive


def _ws_driver(probe: FastAPIWebSocketProbe):
    ws = _make_mock_ws()

    @probe.watch
    async def handler(websocket: WebSocket, fail: bool):
        if fail:
            raise RuntimeError("crash")

    async def drive(fail: bool, times: int) -> None:
        for _ in range(times):
            try:
                await handler(websocket=ws, fail=fail)
            except RuntimeError:
                pass

    return drive


PROBES = [
    pytest.param(lambda n: PassiveProbe("p", window_size=n), _passive_driver, id="passive"),
    pytest.param(lambda n: FastAPIRouteProbe("r", window_size=n), _route_driver, id="route"),
    pytest.param(lambda n: FastAPIWebSocketProbe("ws", window_size=n), _ws_driver, id="websocket"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("make_probe, make_driver", PROBES)
async def test_fresh_outage_is_not_masked_by_healthy_history(make_probe, make_driver):
    probe = make_probe(10)
    drive = make_driver(probe)
    await drive(fail=False, times=1000)
    await drive(fail=True, times=10)

    result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY
    assert result.details["error_rate"] == 1.0
    assert result.details["error_count"] == 10  # still the lifetime total


@pytest.mark.asyncio
@pytest.mark.parametrize("make_probe, make_driver", PROBES)
async def test_old_errors_roll_out_of_the_window(make_probe, make_driver):
    probe = make_probe(10)
    drive = make_driver(probe)
    await drive(fail=True, times=50)
    await drive(fail=False, times=10)

    result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.details["error_rate"] == 0.0
    assert result.details["error_count"] == 50


@pytest.mark.asyncio
@pytest.mark.parametrize("make_probe, make_driver", PROBES)
async def test_partially_filled_window_uses_observed_count(make_probe, make_driver):
    probe = make_probe(100)
    drive = make_driver(probe)
    await drive(fail=False, times=3)
    await drive(fail=True, times=1)

    assert (await probe.check()).details["error_rate"] == 0.25


@pytest.mark.asyncio
@pytest.mark.parametrize("make_probe, make_driver", PROBES)
async def test_window_of_one_tracks_only_the_latest_outcome(make_probe, make_driver):
    probe = make_probe(1)
    drive = make_driver(probe)
    await drive(fail=True, times=1)
    assert (await probe.check()).details["error_rate"] == 1.0
    await drive(fail=False, times=1)
    assert (await probe.check()).details["error_rate"] == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize("make_probe, make_driver", PROBES)
async def test_eviction_at_exact_boundary(make_probe, make_driver):
    probe = make_probe(4)
    drive = make_driver(probe)
    await drive(fail=True, times=1)
    await drive(fail=False, times=3)
    assert (await probe.check()).details["error_rate"] == 0.25  # window full, failure still in it
    await drive(fail=False, times=1)
    assert (await probe.check()).details["error_rate"] == 0.0  # failure just evicted


@pytest.mark.asyncio
async def test_websocket_open_connections_do_not_dilute_the_rate():
    probe = FastAPIWebSocketProbe("ws")
    await _ws_driver(probe)(fail=True, times=1)
    probe._active_connections += 5  # five sockets still open, outcome unknown

    assert (await probe.check()).details["error_rate"] == 1.0


# ---------------------------------------------------------------------------
# RequestMetricsMiddleware stats bucket
# ---------------------------------------------------------------------------

def test_route_stats_fresh_outage_after_healthy_history():
    stats = _RouteStats(window_size=10, ema_alpha=0.1)
    for _ in range(1000):
        stats.record(200, 1.0)
    for _ in range(10):
        stats.record(500, 1.0)

    snap = stats.snapshot()
    assert snap["error_rate"] == 1.0
    assert snap["error_count"] == 10


def test_route_stats_old_errors_roll_out():
    stats = _RouteStats(window_size=10, ema_alpha=0.1)
    for _ in range(50):
        stats.record(503, 1.0)
    for _ in range(10):
        stats.record(200, 1.0)

    snap = stats.snapshot()
    assert snap["error_rate"] == 0.0
    assert snap["error_count"] == 50


def test_route_stats_respects_min_error_status():
    stats = _RouteStats(window_size=10, ema_alpha=0.1, min_error_status=400)
    stats.record(404, 1.0)
    stats.record(200, 1.0)

    assert stats.snapshot()["error_rate"] == 0.5


def test_route_stats_empty_window_reports_zero():
    assert _RouteStats(window_size=10, ema_alpha=0.1).snapshot()["error_rate"] == 0.0
