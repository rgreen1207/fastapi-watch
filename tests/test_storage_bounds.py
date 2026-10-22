"""What the registry writes to storage stays within the probes it registers.

These run against the default in-memory backend; tests/test_redis_storage.py
repeats the Redis-specific ones against a real server.
"""
from fastapi import FastAPI

from fastapi_watch import HealthRegistry
from fastapi_watch.models import ProbeResult, ProbeStatus
from fastapi_watch.probes import NoOpProbe


class _Renaming(NoOpProbe):
    """Returns a result under a new name on every check, failing every other one."""

    def __init__(self, name: str) -> None:
        super().__init__(name=name)
        self.calls = 0

    async def check(self) -> ProbeResult:
        self.calls += 1
        status = ProbeStatus.UNHEALTHY if self.calls % 2 else ProbeStatus.HEALTHY
        return ProbeResult(name=f"inner-{self.calls}", status=status)


async def test_result_named_differently_is_filed_under_the_probe_name():
    registry = HealthRegistry(FastAPI(), poll_interval_ms=None, result_ttl_seconds=0)
    registry.add(_Renaming("composite"))
    for _ in range(50):
        await registry.run_all()
    storage = registry._storage
    # One probe registered, so one name stored: not one per check.
    assert set(await storage.get_history()) == {"composite"}
    assert set(await storage.get_all_latest()) == {"composite"}
    assert {a.probe for a in await storage.get_alerts()} == {"composite"}
    assert [r.name for r in await registry._get_results()] == ["composite"]
    assert registry._written_names == {"composite"}
    assert set(registry._probe_states) == {"composite"}
