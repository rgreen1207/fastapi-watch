"""Tests for the bounded asyncio.Queue-based alert dispatch system."""
import asyncio
import pytest
from fastapi import FastAPI
from fastapi_watch import HealthRegistry
from fastapi_watch.alerts import BaseAlerter
from fastapi_watch.models import AlertRecord, ProbeStatus, ProbeResult
from fastapi_watch.probes import NoOpProbe


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _RecordingAlerter(BaseAlerter):
    def __init__(self):
        self.received: list[AlertRecord] = []

    async def notify(self, alert: AlertRecord) -> None:
        self.received.append(alert)


class _SlowAlerter(BaseAlerter):
    def __init__(self, delay: float = 0.05):
        self.delay = delay
        self.received: list[AlertRecord] = []

    async def notify(self, alert: AlertRecord) -> None:
        await asyncio.sleep(self.delay)
        self.received.append(alert)


class _ErrorAlerter(BaseAlerter):
    async def notify(self, alert: AlertRecord) -> None:
        raise RuntimeError("alerter exploded")


# ---------------------------------------------------------------------------
# Queue initialisation
# ---------------------------------------------------------------------------

def test_alert_queue_created_on_init():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    assert isinstance(registry._alert_queue, asyncio.Queue)


def test_alert_queue_maxsize_is_256():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    assert registry._alert_queue.maxsize == 256


def test_alert_worker_initially_none():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    assert registry._alert_worker is None


# ---------------------------------------------------------------------------
# _ensure_alert_worker
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_alert_worker_starts_task():
    app = FastAPI()
    alerter = _RecordingAlerter()
    registry = HealthRegistry(app, poll_interval_ms=None, alerters=[alerter])
    registry._ensure_alert_worker()
    assert registry._alert_worker is not None
    assert not registry._alert_worker.done()
    registry._alert_worker.cancel()
    try:
        await registry._alert_worker
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_ensure_alert_worker_idempotent():
    app = FastAPI()
    alerter = _RecordingAlerter()
    registry = HealthRegistry(app, poll_interval_ms=None, alerters=[alerter])
    registry._ensure_alert_worker()
    first_task = registry._alert_worker
    registry._ensure_alert_worker()
    assert registry._alert_worker is first_task  # same task, not a new one
    first_task.cancel()
    try:
        await first_task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_ensure_alert_worker_restarts_after_done():
    app = FastAPI()
    alerter = _RecordingAlerter()
    registry = HealthRegistry(app, poll_interval_ms=None, alerters=[alerter])
    registry._ensure_alert_worker()
    first_task = registry._alert_worker
    first_task.cancel()
    try:
        await first_task
    except (asyncio.CancelledError, Exception):
        pass
    assert first_task.done()
    registry._ensure_alert_worker()
    assert registry._alert_worker is not first_task
    registry._alert_worker.cancel()
    try:
        await registry._alert_worker
    except (asyncio.CancelledError, Exception):
        pass


# ---------------------------------------------------------------------------
# Alert delivery via queue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_alert_delivered_to_alerter():
    app = FastAPI()
    alerter = _RecordingAlerter()
    registry = HealthRegistry(app, poll_interval_ms=None, alerters=[alerter])

    from datetime import datetime, timezone
    alert = AlertRecord(
        probe="db",
        old_status=ProbeStatus.HEALTHY,
        new_status=ProbeStatus.UNHEALTHY,
        timestamp=datetime.now(timezone.utc),
    )
    registry._ensure_alert_worker()
    registry._alert_queue.put_nowait(alert)
    await asyncio.sleep(0.05)

    assert len(alerter.received) == 1
    assert alerter.received[0].probe == "db"

    registry._alert_worker.cancel()
    try:
        await registry._alert_worker
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_multiple_alerts_delivered_in_order():
    app = FastAPI()
    alerter = _RecordingAlerter()
    registry = HealthRegistry(app, poll_interval_ms=None, alerters=[alerter])

    from datetime import datetime, timezone
    registry._ensure_alert_worker()
    probes = ["db", "cache", "redis"]
    for name in probes:
        registry._alert_queue.put_nowait(AlertRecord(
            probe=name,
            old_status=ProbeStatus.HEALTHY,
            new_status=ProbeStatus.UNHEALTHY,
            timestamp=datetime.now(timezone.utc),
        ))
    await asyncio.sleep(0.1)

    assert [a.probe for a in alerter.received] == probes

    registry._alert_worker.cancel()
    try:
        await registry._alert_worker
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_failing_alerter_does_not_crash_worker():
    app = FastAPI()
    error_alerter = _ErrorAlerter()
    recording = _RecordingAlerter()
    registry = HealthRegistry(app, poll_interval_ms=None, alerters=[error_alerter, recording])

    from datetime import datetime, timezone
    alert = AlertRecord(
        probe="db",
        old_status=ProbeStatus.HEALTHY,
        new_status=ProbeStatus.UNHEALTHY,
        timestamp=datetime.now(timezone.utc),
    )
    registry._ensure_alert_worker()
    registry._alert_queue.put_nowait(alert)
    await asyncio.sleep(0.05)

    # Worker must still be alive and the recording alerter still received the alert
    assert not registry._alert_worker.done()
    assert len(recording.received) == 1

    registry._alert_worker.cancel()
    try:
        await registry._alert_worker
    except (asyncio.CancelledError, Exception):
        pass


# ---------------------------------------------------------------------------
# Queue full / drop behaviour
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_queue_full_drops_alert(caplog):
    import logging
    app = FastAPI()
    alerter = _SlowAlerter(delay=10.0)  # never completes during the test
    registry = HealthRegistry(app, poll_interval_ms=None, alerters=[alerter])

    from datetime import datetime, timezone

    def _make_alert(name: str) -> AlertRecord:
        return AlertRecord(
            probe=name,
            old_status=ProbeStatus.HEALTHY,
            new_status=ProbeStatus.UNHEALTHY,
            timestamp=datetime.now(timezone.utc),
        )

    # Fill the queue completely without starting the worker
    for i in range(256):
        registry._alert_queue.put_nowait(_make_alert(f"probe-{i}"))

    assert registry._alert_queue.full()

    # Attempt to enqueue one more via _ensure_alert_worker + put_nowait
    registry._ensure_alert_worker()
    with caplog.at_level(logging.WARNING, logger="fastapi_watch.registry"):
        try:
            registry._alert_queue.put_nowait(_make_alert("overflow"))
        except asyncio.QueueFull:
            pass  # expected; the warning path in the real code catches this

    # Queue size has not grown beyond 256
    assert registry._alert_queue.qsize() == 256

    registry._alert_worker.cancel()
    try:
        await registry._alert_worker
    except (asyncio.CancelledError, Exception):
        pass


# ---------------------------------------------------------------------------
# Shutdown cancels worker
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shutdown_cancels_alert_worker():
    app = FastAPI()
    alerter = _RecordingAlerter()
    registry = HealthRegistry(app, poll_interval_ms=None, alerters=[alerter])
    registry._ensure_alert_worker()
    worker = registry._alert_worker
    assert not worker.done()

    await registry._shutdown()

    assert worker.done()
    assert registry._alert_worker is None


@pytest.mark.asyncio
async def test_shutdown_with_no_worker_is_safe():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    assert registry._alert_worker is None
    # Should not raise
    await registry._shutdown()


# ---------------------------------------------------------------------------
# End-to-end: state change triggers alert via queue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_state_change_dispatches_via_queue():
    app = FastAPI()
    alerter = _RecordingAlerter()
    registry = HealthRegistry(app, poll_interval_ms=None, alerters=[alerter])
    registry.add(NoOpProbe(name="db"))

    # Seed a known previous state so the update triggers a transition
    registry._probe_states["db"] = ProbeStatus.HEALTHY

    unhealthy = ProbeResult(name="db", status=ProbeStatus.UNHEALTHY)
    await registry._fire_state_changes([unhealthy])
    await asyncio.sleep(0.1)

    assert len(alerter.received) == 1
    assert alerter.received[0].new_status == ProbeStatus.UNHEALTHY

    if registry._alert_worker and not registry._alert_worker.done():
        registry._alert_worker.cancel()
        try:
            await registry._alert_worker
        except (asyncio.CancelledError, Exception):
            pass
