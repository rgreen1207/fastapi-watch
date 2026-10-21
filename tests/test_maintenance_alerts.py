"""Alerters catch up on state changes that happened during maintenance.

Maintenance suppresses alerters but not state tracking, so a dependency that
failed during maintenance and stayed down used to never alert.
"""
import asyncio

import pytest
from fastapi import FastAPI

from fastapi_watch import HealthRegistry
from fastapi_watch.alerts import BaseAlerter
from fastapi_watch.models import AlertRecord, ProbeResult, ProbeStatus
from fastapi_watch.probes.base import BaseProbe

H, D, U = ProbeStatus.HEALTHY, ProbeStatus.DEGRADED, ProbeStatus.UNHEALTHY


class _Recorder(BaseAlerter):
    def __init__(self):
        self.sent: list[tuple[str, ProbeStatus, ProbeStatus]] = []

    async def notify(self, alert: AlertRecord) -> None:
        self.sent.append((alert.probe, alert.old_status, alert.new_status))


class _Scripted(BaseProbe):
    """Returns the statuses it is given, one per check."""

    name = "db"

    def __init__(self):
        self.status = H

    async def check(self) -> ProbeResult:
        return ProbeResult(name=self.name, status=self.status)


async def _setup():
    alerter = _Recorder()
    callbacks: list[tuple[str, ProbeStatus, ProbeStatus]] = []
    registry = HealthRegistry(FastAPI(), poll_interval_ms=None, alerters=[alerter])
    registry.on_state_change(lambda name, old, new: callbacks.append((name, old, new)))
    probe = _Scripted()
    registry.add(probe)
    return registry, probe, alerter, callbacks


async def _check(registry, probe, status):
    probe.status = status
    await registry.run_all()
    await asyncio.sleep(0.02)  # let the alert worker drain


async def _stop(registry):
    await registry._shutdown()


@pytest.mark.asyncio
async def test_failure_during_maintenance_alerts_after_it_ends():
    registry, probe, alerter, callbacks = await _setup()
    try:
        await _check(registry, probe, H)
        registry.set_maintenance()
        await _check(registry, probe, U)  # fails during maintenance
        await _check(registry, probe, U)
        assert alerter.sent == []

        registry.clear_maintenance()
        await _check(registry, probe, U)  # still down: first check after maintenance
        assert alerter.sent == [("db", H, U)]

        await _check(registry, probe, U)  # no repeat
        assert alerter.sent == [("db", H, U)]
        # Callbacks and stored records follow real transitions only.
        assert callbacks == [("db", H, U)]
        assert [(a.old_status, a.new_status) for a in await registry._storage.get_alerts()] == [(H, U)]
    finally:
        await _stop(registry)


@pytest.mark.asyncio
async def test_failure_that_recovers_during_maintenance_sends_nothing_after():
    registry, probe, alerter, callbacks = await _setup()
    try:
        await _check(registry, probe, H)
        registry.set_maintenance()
        await _check(registry, probe, U)
        await _check(registry, probe, H)  # recovered within the window
        registry.clear_maintenance()
        await _check(registry, probe, H)
        await _check(registry, probe, H)

        assert alerter.sent == []  # no stale alert
        assert callbacks == [("db", H, U), ("db", U, H)]
    finally:
        await _stop(registry)


@pytest.mark.asyncio
async def test_outage_alerted_before_maintenance_resolves_after_recovery_inside_it():
    """The incident opened before maintenance must still be resolved."""
    registry, probe, alerter, _ = await _setup()
    try:
        await _check(registry, probe, U)
        assert alerter.sent == [("db", H, U)]
        registry.set_maintenance()
        await _check(registry, probe, H)  # recovered during maintenance
        registry.clear_maintenance()
        await _check(registry, probe, H)

        assert alerter.sent == [("db", H, U), ("db", U, H)]
    finally:
        await _stop(registry)


@pytest.mark.asyncio
async def test_change_during_maintenance_reports_from_last_sent_status():
    registry, probe, alerter, _ = await _setup()
    try:
        await _check(registry, probe, D)
        registry.set_maintenance()
        await _check(registry, probe, U)
        registry.clear_maintenance()
        await _check(registry, probe, U)

        assert alerter.sent == [("db", H, D), ("db", D, U)]
    finally:
        await _stop(registry)


@pytest.mark.asyncio
async def test_timed_maintenance_expiry_catches_up():
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    registry, probe, alerter, _ = await _setup()
    try:
        await _check(registry, probe, H)
        registry.set_maintenance(until=datetime.now(ZoneInfo("UTC")) + timedelta(seconds=0.2))
        await _check(registry, probe, U)
        assert alerter.sent == []
        await asyncio.sleep(0.25)  # window elapses on its own
        await _check(registry, probe, U)
        assert alerter.sent == [("db", H, U)]
    finally:
        await _stop(registry)


@pytest.mark.asyncio
async def test_alert_dropped_on_full_queue_is_retried_next_check():
    registry, probe, alerter, _ = await _setup()
    registry._alert_queue = asyncio.Queue(maxsize=1)
    registry._alert_queue.put_nowait(AlertRecord(probe="x", old_status=H, new_status=U, timestamp=registry._start_time))
    registry._ensure_alert_worker = lambda: None  # keep the queue full
    probe.status = U
    await registry.run_all()
    assert "db" not in registry._alerted_states

    registry._alert_queue.get_nowait()
    await registry.run_all()  # same status, but the alert never went out
    queued = registry._alert_queue.get_nowait()
    assert (queued.probe, queued.old_status, queued.new_status) == ("db", H, U)
    assert registry._alerted_states["db"] == U
