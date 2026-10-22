"""Opt-in alert flap limiting (alert_min_interval_s) and cross-replica dedupe (alert_dedupe_window_s)."""
import asyncio
import logging
import time

import pytest
from fastapi import FastAPI

from fastapi_watch import HealthRegistry
from fastapi_watch.alerts import BaseAlerter
from fastapi_watch.models import AlertRecord, ProbeResult, ProbeStatus
from fastapi_watch.probes.base import BaseProbe
from fastapi_watch.storage import InMemoryProbeStorage

H, D, U = ProbeStatus.HEALTHY, ProbeStatus.DEGRADED, ProbeStatus.UNHEALTHY
_INTERVAL = 0.3


class _Recorder(BaseAlerter):
    def __init__(self):
        self.sent: list[tuple[ProbeStatus, ProbeStatus]] = []

    async def notify(self, alert: AlertRecord) -> None:
        self.sent.append((alert.old_status, alert.new_status))


class _Scripted(BaseProbe):
    name = "db"

    def __init__(self):
        self.status = H

    async def check(self) -> ProbeResult:
        return ProbeResult(name=self.name, status=self.status)


class _ClaimStorage(InMemoryProbeStorage):
    """A stand-in for a shared backend: the last status announced per key, for ttl_seconds.

    A claim fails only when the key already holds the same value (compare-and-set).
    """

    def __init__(self):
        super().__init__()
        self.claims: dict[str, tuple[str, float]] = {}
        self.calls: list[tuple[str, str, float]] = []

    async def claim_alert(self, key: str, value: str, ttl_seconds: float) -> bool:
        self.calls.append((key, value, ttl_seconds))
        now = time.monotonic()
        held, expires = self.claims.get(key, (None, 0.0))
        if held == value and expires > now:
            return False
        self.claims[key] = (value, now + ttl_seconds)
        return True


class _BrokenClaimStorage(InMemoryProbeStorage):
    async def claim_alert(self, key, value, ttl_seconds):
        raise ConnectionError("storage down")


class _HangingClaimStorage(InMemoryProbeStorage):
    async def claim_alert(self, key, value, ttl_seconds):
        await asyncio.sleep(30)


def _setup(**kwargs):
    alerter = _Recorder()
    registry = HealthRegistry(FastAPI(), poll_interval_ms=None, alerters=[alerter], **kwargs)
    changes = []
    registry.on_state_change(lambda name, old, new: changes.append((old, new)))
    probe = _Scripted()
    registry.add(probe)
    return registry, probe, alerter, changes


async def _check(registry, probe, status, settle=0.02):
    probe.status = status
    await registry.run_all()
    await asyncio.sleep(settle)


# ---------------------------------------------------------------------------
# Flap limiting
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flap_back_to_the_sent_state_within_the_interval_sends_nothing_extra():
    registry, probe, alerter, changes = _setup(alert_min_interval_s=_INTERVAL)
    try:
        await _check(registry, probe, U)  # first alert: immediate
        await asyncio.sleep(_INTERVAL + 0.05)
        await _check(registry, probe, H)  # quiet interval passed: immediate, opens a new one
        assert alerter.sent == [(H, U), (U, H)]

        await _check(registry, probe, U)  # flap inside the interval ...
        await _check(registry, probe, H)  # ... back where alerters last saw it
        await asyncio.sleep(_INTERVAL + 0.1)

        assert alerter.sent == [(H, U), (U, H)]
        assert registry._alert_pending == {}
        # Callbacks and stored records see every transition.
        assert changes == [(H, U), (U, H), (H, U), (U, H)]
        assert len(await registry._storage.get_alerts()) == 4
    finally:
        await registry._shutdown()


@pytest.mark.asyncio
async def test_changes_within_the_interval_send_only_the_final_state_after_it():
    registry, probe, alerter, _ = _setup(alert_min_interval_s=_INTERVAL)
    try:
        await _check(registry, probe, U)
        await asyncio.sleep(_INTERVAL + 0.05)
        await _check(registry, probe, H)  # opens an interval
        await _check(registry, probe, U)
        await _check(registry, probe, D)
        assert alerter.sent == [(H, U), (U, H)]  # coalesced so far

        await asyncio.sleep(_INTERVAL + 0.1)  # no further checks: the worker sends it
        assert alerter.sent == [(H, U), (U, H), (H, D)]
    finally:
        await registry._shutdown()


@pytest.mark.asyncio
async def test_first_alert_is_immediate_and_later_change_is_coalesced():
    registry, probe, alerter, _ = _setup(alert_min_interval_s=_INTERVAL)
    try:
        await _check(registry, probe, U)
        await _check(registry, probe, D)
        assert alerter.sent == [(H, U)]
        await asyncio.sleep(_INTERVAL + 0.1)
        assert alerter.sent == [(H, U), (U, D)]
    finally:
        await registry._shutdown()


@pytest.mark.asyncio
async def test_coalesced_alert_is_dropped_if_it_falls_due_during_maintenance():
    registry, probe, alerter, _ = _setup(alert_min_interval_s=_INTERVAL)
    try:
        await _check(registry, probe, U)
        await _check(registry, probe, H)
        registry.set_maintenance()
        await asyncio.sleep(_INTERVAL + 0.1)
        assert alerter.sent == [(H, U)]
        registry.clear_maintenance()
        await _check(registry, probe, H)  # the first check after maintenance catches up
        assert alerter.sent == [(H, U), (U, H)]
    finally:
        await registry._shutdown()


@pytest.mark.asyncio
async def test_without_flap_limiting_every_change_alerts():
    registry, probe, alerter, _ = _setup()
    try:
        for status in (U, H, U, D):
            await _check(registry, probe, status)
        assert alerter.sent == [(H, U), (U, H), (H, U), (U, D)]
    finally:
        await registry._shutdown()


@pytest.mark.asyncio
async def test_one_pending_marker_per_probe():
    registry, probe, alerter, _ = _setup(alert_min_interval_s=60)
    try:
        await _check(registry, probe, U)
        for status in (D, U, D, U, D):
            await _check(registry, probe, status, settle=0)
        assert list(registry._alert_pending) == ["db"]
        assert alerter.sent == [(H, U)]
    finally:
        await registry._shutdown()


@pytest.mark.parametrize("value", [-1, float("inf"), float("nan")])
def test_negative_or_infinite_min_interval_is_rejected(value):
    with pytest.raises(ValueError, match="alert_min_interval_s"):
        HealthRegistry(FastAPI(), alert_min_interval_s=value)


# ---------------------------------------------------------------------------
# Cross-replica dedupe
# ---------------------------------------------------------------------------

def _replicas(storage, n=2, **kwargs):
    return [_setup(storage=storage, alert_dedupe_window_s=60, **kwargs) for _ in range(n)]


@pytest.mark.asyncio
async def test_replicas_sharing_storage_send_one_alert_total():
    storage = _ClaimStorage()
    replicas = _replicas(storage)
    try:
        for registry, probe, _, _ in replicas:
            await _check(registry, probe, U)
        assert sum(len(alerter.sent) for _, _, alerter, _ in replicas) == 1
        assert storage.calls == [("db", "unhealthy", 60), ("db", "unhealthy", 60)]

        # The losers count the state as sent: no repeat on later checks.
        for registry, probe, _, _ in replicas:
            await _check(registry, probe, U)
            assert registry._alerted_states["db"] == U
        assert sum(len(alerter.sent) for _, _, alerter, _ in replicas) == 1

        for registry, probe, _, _ in replicas:
            await _check(registry, probe, H)  # recovery is its own key
        assert sum(len(alerter.sent) for _, _, alerter, _ in replicas) == 2
        # Every replica still records the transitions.
        assert all(changes == [(H, U), (U, H)] for _, _, _, changes in replicas)
    finally:
        for registry, *_ in replicas:
            await registry._shutdown()


@pytest.mark.asyncio
async def test_refailure_inside_the_dedupe_window_is_alerted_once():
    # unhealthy -> healthy -> unhealthy within the window: a claim keyed on
    # "<probe>:<status>" refused the second failure on every replica.
    storage = _ClaimStorage()
    replicas = _replicas(storage)
    try:
        for status in (U, H, U):
            for registry, probe, _, _ in replicas:
                await _check(registry, probe, status)
        sent = [t for _, _, alerter, _ in replicas for t in alerter.sent]
        assert sorted(sent) == sorted([(H, U), (U, H), (H, U)])
    finally:
        for registry, *_ in replicas:
            await registry._shutdown()


@pytest.mark.parametrize("storage_cls", [_BrokenClaimStorage, _HangingClaimStorage])
@pytest.mark.asyncio
async def test_claim_failure_fails_open_and_sends(storage_cls):
    storage = storage_cls()
    replicas = _replicas(storage, storage_timeout=0.05)
    try:
        for registry, probe, _, _ in replicas:
            await _check(registry, probe, U, settle=0.15)
        assert [alerter.sent for _, _, alerter, _ in replicas] == [[(H, U)], [(H, U)]]
    finally:
        for registry, *_ in replicas:
            await registry._shutdown()


@pytest.mark.parametrize("storage", [None, InMemoryProbeStorage()])
def test_dedupe_without_claim_alert_warns_once_at_init(storage, caplog):
    with caplog.at_level(logging.WARNING, logger="fastapi_watch"):
        registry = HealthRegistry(FastAPI(), storage=storage, alert_dedupe_window_s=60, auth=lambda r: True)
    messages = [r.getMessage() for r in caplog.records if "alert_dedupe_window_s" in r.getMessage()]
    assert len(messages) == 1 and "shared storage backend" in messages[0]
    assert registry._alert_dedupe_window_s is None


@pytest.mark.asyncio
async def test_dedupe_off_by_default_never_claims():
    storage = _ClaimStorage()
    registry, probe, alerter, _ = _setup(storage=storage)
    try:
        await _check(registry, probe, U)
        assert storage.calls == []
        assert alerter.sent == [(H, U)]
    finally:
        await registry._shutdown()


@pytest.mark.parametrize("value", [0, -5, float("inf"), float("nan")])
def test_invalid_dedupe_window_is_rejected(value):
    with pytest.raises(ValueError, match="alert_dedupe_window_s"):
        HealthRegistry(FastAPI(), alert_dedupe_window_s=value)


@pytest.mark.asyncio
async def test_flap_limit_and_dedupe_combine():
    storage = _ClaimStorage()
    replicas = _replicas(storage, alert_min_interval_s=_INTERVAL)
    try:
        for registry, probe, _, _ in replicas:
            await _check(registry, probe, U, settle=0)
        for registry, probe, _, _ in replicas:
            await _check(registry, probe, D, settle=0)
        await asyncio.sleep(_INTERVAL + 0.15)
        sent = sorted(a for _, _, alerter, _ in replicas for a in alerter.sent)
        assert sent == sorted([(H, U), (U, D)])
    finally:
        for registry, *_ in replicas:
            await registry._shutdown()
