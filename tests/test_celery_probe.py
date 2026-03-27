"""Tests for CeleryProbe."""
import pytest
from unittest.mock import MagicMock, patch
from fastapi_watch.models import ProbeStatus
from fastapi_watch.probes.celery import CeleryProbe, _clean_task, _clean_scheduled


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app(
    ping=None,
    active=None,
    reserved=None,
    scheduled=None,
    registered=None,
    stats=None,
    active_queues=None,
):
    """Build a mock Celery app whose inspect() returns the given data."""
    inspector = MagicMock()
    inspector.ping.return_value = ping
    inspector.active.return_value = active
    inspector.reserved.return_value = reserved
    inspector.scheduled.return_value = scheduled
    inspector.registered.return_value = registered
    inspector.stats.return_value = stats
    inspector.active_queues.return_value = active_queues

    app = MagicMock()
    app.control.inspect.return_value = inspector
    return app


_WORKER = "celery@host1"

_ACTIVE_TASK = {
    "id": "abc-123",
    "name": "myapp.tasks.send_email",
    "args": ["user@example.com"],
    "kwargs": {"subject": "Hello"},
    "time_start": 1700000000.0,
    "worker_pid": 42,
    "ignored_field": "drop_me",
}

_SCHEDULED_ENTRY = {
    "eta": "2024-01-01T12:00:00",
    "priority": 6,
    "request": {
        "id": "def-456",
        "name": "myapp.tasks.cleanup",
        "args": [],
        "kwargs": {},
        "time_start": None,
        "worker_pid": 42,
        "extra": "ignored",
    },
    "extra": "ignored",
}

_STATS = {
    "pool": {
        "implementation": "celery.concurrency.prefork:TaskPool",
        "max-concurrency": 4,
        "processes": [101, 102, 103, 104],
    },
    "total": {"myapp.tasks.send_email": 99},
}

_QUEUES = [{"name": "celery"}, {"name": "high_priority"}]


# ---------------------------------------------------------------------------
# _clean_task / _clean_scheduled
# ---------------------------------------------------------------------------

def test_clean_task_drops_unknown_fields():
    result = _clean_task(_ACTIVE_TASK)
    assert "ignored_field" not in result
    assert result["id"] == "abc-123"
    assert result["name"] == "myapp.tasks.send_email"


def test_clean_scheduled_cleans_nested_request():
    result = _clean_scheduled(_SCHEDULED_ENTRY)
    assert "extra" not in result
    assert result["eta"] == "2024-01-01T12:00:00"
    assert "extra" not in result["request"]
    assert result["request"]["id"] == "def-456"


# ---------------------------------------------------------------------------
# No workers — scale-to-zero (min_workers=0, default)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_workers_healthy_when_min_workers_zero():
    app = _make_app(ping=None)
    probe = CeleryProbe(app)

    result = await probe.check()

    assert result.status == ProbeStatus.HEALTHY
    assert result.error is None
    assert result.details["workers_online"] == 0
    assert "reason" in result.details
    assert "scaled down" in result.details["reason"]


@pytest.mark.asyncio
async def test_no_workers_healthy_with_empty_ping_dict():
    app = _make_app(ping={})
    probe = CeleryProbe(app)

    result = await probe.check()

    assert result.status == ProbeStatus.HEALTHY
    assert result.details["workers_online"] == 0


# ---------------------------------------------------------------------------
# No workers — min_workers > 0
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_workers_unhealthy_when_min_workers_required():
    app = _make_app(ping=None)
    probe = CeleryProbe(app, min_workers=1)

    result = await probe.check()

    assert result.status == ProbeStatus.UNHEALTHY
    assert "expected at least 1" in result.error
    assert result.details["workers_online"] == 0


@pytest.mark.asyncio
async def test_fewer_workers_than_required():
    app = _make_app(
        ping={_WORKER: {"ok": "pong"}},
        active={_WORKER: []},
        reserved={_WORKER: []},
        scheduled={_WORKER: []},
        registered={_WORKER: ["myapp.tasks.send_email"]},
        stats={_WORKER: _STATS},
        active_queues={_WORKER: _QUEUES},
    )
    probe = CeleryProbe(app, min_workers=3)

    result = await probe.check()

    assert result.status == ProbeStatus.UNHEALTHY
    assert "1 worker(s) online" in result.error
    assert "expected at least 3" in result.error
    assert result.details["workers_online"] == 1


# ---------------------------------------------------------------------------
# Workers online — healthy
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_workers_online_healthy():
    app = _make_app(
        ping={_WORKER: {"ok": "pong"}},
        active={_WORKER: [_ACTIVE_TASK]},
        reserved={_WORKER: []},
        scheduled={_WORKER: [_SCHEDULED_ENTRY]},
        registered={_WORKER: ["myapp.tasks.send_email", "myapp.tasks.cleanup"]},
        stats={_WORKER: _STATS},
        active_queues={_WORKER: _QUEUES},
    )
    probe = CeleryProbe(app)

    result = await probe.check()

    assert result.status == ProbeStatus.HEALTHY
    assert result.error is None
    assert result.details["workers_online"] == 1

    w = result.details["workers"][_WORKER]
    assert w["status"] == "online"
    assert w["active_tasks"] == 1
    assert w["reserved_tasks"] == 0
    assert w["scheduled_tasks"] == 1
    assert w["queues"] == ["celery", "high_priority"]
    assert w["pool"]["max_concurrency"] == 4
    assert w["pool"]["processes"] == [101, 102, 103, 104]
    assert w["total_tasks_executed"] == {"myapp.tasks.send_email": 99}
    assert "myapp.tasks.cleanup" in w["registered_tasks"]


@pytest.mark.asyncio
async def test_registered_tasks_sorted():
    app = _make_app(
        ping={_WORKER: {"ok": "pong"}},
        active={_WORKER: []},
        reserved={_WORKER: []},
        scheduled={_WORKER: []},
        registered={_WORKER: ["z.task", "a.task", "m.task"]},
        stats={_WORKER: _STATS},
        active_queues={_WORKER: _QUEUES},
    )
    probe = CeleryProbe(app)
    result = await probe.check()

    tasks = result.details["workers"][_WORKER]["registered_tasks"]
    assert tasks == ["a.task", "m.task", "z.task"]


@pytest.mark.asyncio
async def test_active_tasks_cleaned():
    app = _make_app(
        ping={_WORKER: {"ok": "pong"}},
        active={_WORKER: [_ACTIVE_TASK]},
        reserved={_WORKER: []},
        scheduled={_WORKER: []},
        registered={_WORKER: []},
        stats={_WORKER: _STATS},
        active_queues={_WORKER: _QUEUES},
    )
    probe = CeleryProbe(app)
    result = await probe.check()

    active = result.details["workers"][_WORKER]["active"]
    assert len(active) == 1
    assert "ignored_field" not in active[0]
    assert active[0]["id"] == "abc-123"


# ---------------------------------------------------------------------------
# Multiple workers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multiple_workers_all_reported():
    worker2 = "celery@host2"
    app = _make_app(
        ping={_WORKER: {"ok": "pong"}, worker2: {"ok": "pong"}},
        active={_WORKER: [], worker2: []},
        reserved={_WORKER: [], worker2: []},
        scheduled={_WORKER: [], worker2: []},
        registered={_WORKER: [], worker2: []},
        stats={_WORKER: _STATS, worker2: _STATS},
        active_queues={_WORKER: _QUEUES, worker2: _QUEUES},
    )
    probe = CeleryProbe(app, min_workers=2)
    result = await probe.check()

    assert result.status == ProbeStatus.HEALTHY
    assert result.details["workers_online"] == 2
    assert _WORKER in result.details["workers"]
    assert worker2 in result.details["workers"]


# ---------------------------------------------------------------------------
# Inspect returns None for some calls
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_none_inspect_results_handled_gracefully():
    app = _make_app(
        ping={_WORKER: {"ok": "pong"}},
        active=None,
        reserved=None,
        scheduled=None,
        registered=None,
        stats=None,
        active_queues=None,
    )
    probe = CeleryProbe(app)
    result = await probe.check()

    assert result.status == ProbeStatus.HEALTHY
    w = result.details["workers"][_WORKER]
    assert w["active_tasks"] == 0
    assert w["reserved_tasks"] == 0
    assert w["queues"] == []
    assert w["registered_tasks"] == []


# ---------------------------------------------------------------------------
# Exception handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inspect_exception_returns_unhealthy():
    app = MagicMock()
    app.control.inspect.side_effect = Exception("broker unreachable")
    probe = CeleryProbe(app)

    result = await probe.check()

    assert result.status == ProbeStatus.UNHEALTHY
    assert "broker unreachable" in result.error


# ---------------------------------------------------------------------------
# Probe attributes
# ---------------------------------------------------------------------------

def test_default_name():
    probe = CeleryProbe(MagicMock())
    assert probe.name == "celery"


def test_custom_name():
    probe = CeleryProbe(MagicMock(), name="worker-fleet")
    assert probe.name == "worker-fleet"


def test_default_min_workers():
    probe = CeleryProbe(MagicMock())
    assert probe.min_workers == 0


@pytest.mark.asyncio
async def test_latency_recorded():
    app = _make_app(ping={})
    probe = CeleryProbe(app)
    result = await probe.check()
    assert result.latency_ms >= 0.0


@pytest.mark.asyncio
async def test_inspect_called_with_configured_timeout():
    app = _make_app(ping={})
    probe = CeleryProbe(app, timeout=2.5)
    await probe.check()
    app.control.inspect.assert_called_once_with(timeout=2.5)
