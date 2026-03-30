import pytest
from fastapi_watch.probes.threshold import ThresholdProbe
from fastapi_watch.probes.noop import NoOpProbe
from fastapi_watch.models import ProbeResult, ProbeStatus


def _make_probe_with_details(details: dict, status: ProbeStatus = ProbeStatus.HEALTHY) -> NoOpProbe:
    class DetailsProbe(NoOpProbe):
        async def check(self):
            return ProbeResult(name=self.name, status=status, details=details)
    return DetailsProbe(name="inner")


@pytest.mark.asyncio
async def test_threshold_healthy_no_thresholds_triggered():
    inner = _make_probe_with_details({"connections": 50, "max_connections": 100})
    probe = ThresholdProbe(
        inner,
        warn_if=lambda d: d["connections"] / d["max_connections"] > 0.80,
        fail_if=lambda d: d["connections"] / d["max_connections"] > 0.95,
    )
    result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY


@pytest.mark.asyncio
async def test_threshold_degraded_when_warn_triggered():
    inner = _make_probe_with_details({"connections": 85, "max_connections": 100})
    probe = ThresholdProbe(
        inner,
        warn_if=lambda d: d["connections"] / d["max_connections"] > 0.80,
        fail_if=lambda d: d["connections"] / d["max_connections"] > 0.95,
    )
    result = await probe.check()
    assert result.status == ProbeStatus.DEGRADED


@pytest.mark.asyncio
async def test_threshold_unhealthy_when_fail_triggered():
    inner = _make_probe_with_details({"connections": 98, "max_connections": 100})
    probe = ThresholdProbe(
        inner,
        warn_if=lambda d: d["connections"] / d["max_connections"] > 0.80,
        fail_if=lambda d: d["connections"] / d["max_connections"] > 0.95,
    )
    result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY


@pytest.mark.asyncio
async def test_threshold_fail_takes_priority_over_warn():
    inner = _make_probe_with_details({"val": 99})
    probe = ThresholdProbe(
        inner,
        warn_if=lambda d: d["val"] > 50,
        fail_if=lambda d: d["val"] > 90,
    )
    result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY


@pytest.mark.asyncio
async def test_threshold_does_not_upgrade_unhealthy_inner():
    inner = _make_probe_with_details({"val": 0}, status=ProbeStatus.UNHEALTHY)
    probe = ThresholdProbe(
        inner,
        warn_if=lambda d: False,
        fail_if=lambda d: False,
    )
    result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY


@pytest.mark.asyncio
async def test_threshold_inherits_inner_name():
    inner = _make_probe_with_details({})
    inner.name = "postgresql"
    probe = ThresholdProbe(inner)
    result = await probe.check()
    assert result.name == "postgresql"


@pytest.mark.asyncio
async def test_threshold_custom_name_overrides_inner():
    inner = _make_probe_with_details({})
    probe = ThresholdProbe(inner, name="pg-connections")
    result = await probe.check()
    assert result.name == "pg-connections"


@pytest.mark.asyncio
async def test_threshold_callable_exception_does_not_crash():
    """A buggy threshold callable must not propagate — treat as not triggered."""
    inner = _make_probe_with_details({"val": 50})
    probe = ThresholdProbe(
        inner,
        warn_if=lambda d: 1 / 0,  # raises ZeroDivisionError
    )
    result = await probe.check()
    # Should remain HEALTHY since the callable failed silently
    assert result.status == ProbeStatus.HEALTHY


@pytest.mark.asyncio
async def test_threshold_inherits_inner_poll_interval():
    inner = _make_probe_with_details({})
    inner.poll_interval_ms = 5_000
    probe = ThresholdProbe(inner)
    assert probe.poll_interval_ms == 5_000


@pytest.mark.asyncio
async def test_threshold_poll_interval_override():
    inner = _make_probe_with_details({})
    inner.poll_interval_ms = 5_000
    probe = ThresholdProbe(inner, poll_interval_ms=10_000)
    assert probe.poll_interval_ms == 10_000
