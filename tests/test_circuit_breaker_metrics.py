"""Tests for circuit breaker metrics injected into probe result details."""
import pytest
from fastapi import FastAPI
from fastapi_watch import HealthRegistry
from fastapi_watch.probes.noop import NoOpProbe
from fastapi_watch.models import ProbeResult, ProbeStatus


class AlwaysFail(NoOpProbe):
    async def check(self):
        return ProbeResult(name=self.name, status=ProbeStatus.UNHEALTHY, error="fail")


class AlwaysPass(NoOpProbe):
    async def check(self):
        return ProbeResult(name=self.name, status=ProbeStatus.HEALTHY)


@pytest.mark.asyncio
async def test_cb_metrics_in_details_when_passing():
    app = FastAPI()
    registry = HealthRegistry(app)
    probe = AlwaysPass(name="svc")
    registry.add(probe)

    result = await registry._safe_check(probe, critical=True)
    assert "circuit_breaker" in result.details
    cb = result.details["circuit_breaker"]
    assert cb["open"] is False
    assert cb["consecutive_failures"] == 0
    assert cb["trips_total"] == 0


@pytest.mark.asyncio
async def test_cb_consecutive_failures_increments():
    app = FastAPI()
    registry = HealthRegistry(app, circuit_breaker_threshold=10)
    probe = AlwaysFail(name="svc")
    registry.add(probe)

    await registry._safe_check(probe, critical=True)
    result = await registry._safe_check(probe, critical=True)
    cb = result.details["circuit_breaker"]
    assert cb["consecutive_failures"] == 2


@pytest.mark.asyncio
async def test_cb_trips_total_increments_on_trip():
    app = FastAPI()
    registry = HealthRegistry(app, circuit_breaker_threshold=2, circuit_breaker_cooldown_ms=60_000)
    probe = AlwaysFail(name="svc")
    registry.add(probe)

    # Trip the circuit (threshold=2)
    await registry._safe_check(probe, critical=True)
    result = await registry._safe_check(probe, critical=True)
    cb = result.details["circuit_breaker"]
    assert cb["trips_total"] == 1


@pytest.mark.asyncio
async def test_cb_open_true_when_circuit_open():
    app = FastAPI()
    registry = HealthRegistry(app, circuit_breaker_threshold=2, circuit_breaker_cooldown_ms=60_000)
    probe = AlwaysFail(name="svc")
    registry.add(probe)

    # Trip the circuit
    for _ in range(2):
        await registry._safe_check(probe, critical=True)

    # This call is served from the circuit open path
    result = await registry._safe_check(probe, critical=True)
    cb = result.details["circuit_breaker"]
    assert cb["open"] is True


@pytest.mark.asyncio
async def test_cb_resets_after_success():
    fail_count = 0

    class SometimesFail(NoOpProbe):
        async def check(self):
            nonlocal fail_count
            if fail_count < 2:
                fail_count += 1
                return ProbeResult(name=self.name, status=ProbeStatus.UNHEALTHY, error="fail")
            return ProbeResult(name=self.name, status=ProbeStatus.HEALTHY)

    app = FastAPI()
    registry = HealthRegistry(app, circuit_breaker_threshold=5)
    probe = SometimesFail(name="svc")
    registry.add(probe)

    # Two failures (does not trip at threshold=5)
    await registry._safe_check(probe, critical=True)
    await registry._safe_check(probe, critical=True)
    assert registry._circuit_err_count.get("svc") == 2

    # Success → reset
    result = await registry._safe_check(probe, critical=True)
    cb = result.details["circuit_breaker"]
    assert cb["consecutive_failures"] == 0
    assert "svc" not in registry._circuit_err_count


@pytest.mark.asyncio
async def test_cb_disabled_no_circuit_breaker_key():
    app = FastAPI()
    registry = HealthRegistry(app, circuit_breaker=False)
    probe = AlwaysFail(name="svc")
    registry.add(probe)

    result = await registry._safe_check(probe, critical=True)
    # When circuit breaker is disabled, the key must not be injected
    assert "circuit_breaker" not in (result.details or {})


@pytest.mark.asyncio
async def test_cb_trips_total_accumulates_across_trips():
    app = FastAPI()
    registry = HealthRegistry(
        app, circuit_breaker_threshold=1, circuit_breaker_cooldown_ms=0
    )
    probe = AlwaysFail(name="svc")
    registry.add(probe)

    # Trip once
    await registry._safe_check(probe, critical=True)
    assert registry._circuit_trips.get("svc") == 1

    # Force circuit open_until to 0 so it retries
    registry._circuit_open_until["svc"] = 0.0
    # Trip again
    await registry._safe_check(probe, critical=True)
    assert registry._circuit_trips.get("svc") == 2
