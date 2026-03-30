import pytest
from fastapi import FastAPI
from fastapi_watch import HealthRegistry, ProbeRouter
from fastapi_watch.probes import MemoryProbe
from fastapi_watch.models import ProbeStatus


# ---------------------------------------------------------------------------
# add()
# ---------------------------------------------------------------------------

def test_add_single_probe():
    router = ProbeRouter()
    probe = MemoryProbe(name="db")
    router.add(probe)
    assert len(router._probes) == 1
    assert router._probes[0] == (probe, True)


def test_add_non_critical():
    router = ProbeRouter()
    probe = MemoryProbe(name="cache")
    router.add(probe, critical=False)
    assert router._probes[0] == (probe, False)


def test_add_returns_self_for_chaining():
    router = ProbeRouter()
    result = router.add(MemoryProbe(name="a"))
    assert result is router


def test_add_deduplicates_by_identity():
    router = ProbeRouter()
    probe = MemoryProbe(name="a")
    router.add(probe)
    router.add(probe)
    assert len(router._probes) == 1


def test_add_allows_different_instances_same_name():
    router = ProbeRouter()
    router.add(MemoryProbe(name="x"))
    router.add(MemoryProbe(name="x"))
    assert len(router._probes) == 2


# ---------------------------------------------------------------------------
# add_probes()
# ---------------------------------------------------------------------------

def test_add_probes_adds_all():
    router = ProbeRouter()
    probes = [MemoryProbe(name="a"), MemoryProbe(name="b"), MemoryProbe(name="c")]
    router.add_probes(probes)
    assert len(router._probes) == 3


def test_add_probes_critical_flag_applied_to_all():
    router = ProbeRouter()
    probes = [MemoryProbe(name="a"), MemoryProbe(name="b")]
    router.add_probes(probes, critical=False)
    for _, critical in router._probes:
        assert critical is False


def test_add_probes_returns_self():
    router = ProbeRouter()
    result = router.add_probes([MemoryProbe(name="a")])
    assert result is router


def test_add_probes_deduplicates():
    router = ProbeRouter()
    probe = MemoryProbe(name="a")
    router.add_probes([probe, probe])
    assert len(router._probes) == 1


# ---------------------------------------------------------------------------
# include_router()
# ---------------------------------------------------------------------------

def test_include_router_merges_probes():
    sub = ProbeRouter()
    sub.add(MemoryProbe(name="a"))
    sub.add(MemoryProbe(name="b"))

    parent = ProbeRouter()
    parent.include_router(sub)
    assert len(parent._probes) == 2


def test_include_router_preserves_criticality():
    sub = ProbeRouter()
    probe_crit = MemoryProbe(name="critical-svc")
    probe_opt = MemoryProbe(name="optional-svc")
    sub.add(probe_crit, critical=True)
    sub.add(probe_opt, critical=False)

    parent = ProbeRouter()
    parent.include_router(sub)

    by_name = {p.name: c for p, c in parent._probes}
    assert by_name["critical-svc"] is True
    assert by_name["optional-svc"] is False


def test_include_router_returns_self():
    parent = ProbeRouter()
    sub = ProbeRouter()
    result = parent.include_router(sub)
    assert result is parent


def test_include_router_deduplicates_by_identity():
    probe = MemoryProbe(name="shared")
    sub = ProbeRouter()
    sub.add(probe)

    parent = ProbeRouter()
    parent.add(probe)         # already registered
    parent.include_router(sub)  # should not add again
    assert len(parent._probes) == 1


def test_include_router_empty_sub_is_noop():
    parent = ProbeRouter()
    parent.add(MemoryProbe(name="a"))
    parent.include_router(ProbeRouter())
    assert len(parent._probes) == 1


# ---------------------------------------------------------------------------
# Nesting
# ---------------------------------------------------------------------------

def test_nested_routers_compose_correctly():
    leaf_a = ProbeRouter()
    leaf_a.add(MemoryProbe(name="db"))

    leaf_b = ProbeRouter()
    leaf_b.add(MemoryProbe(name="cache"), critical=False)

    mid = ProbeRouter()
    mid.include_router(leaf_a)
    mid.include_router(leaf_b)

    root = ProbeRouter()
    root.include_router(mid)

    assert len(root._probes) == 2
    by_name = {p.name: c for p, c in root._probes}
    assert by_name["db"] is True
    assert by_name["cache"] is False


def test_chaining_add_and_include_router():
    sub = ProbeRouter()
    sub.add(MemoryProbe(name="sub-probe"))

    router = ProbeRouter()
    router.add(MemoryProbe(name="a")).add(MemoryProbe(name="b")).include_router(sub)
    assert len(router._probes) == 3


# ---------------------------------------------------------------------------
# HealthRegistry integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_registry_routers_param_registers_probes():
    router = ProbeRouter()
    router.add(MemoryProbe(name="from-router"))

    app = FastAPI()
    registry = HealthRegistry(app, routers=[router])
    results = await registry.run_all()
    assert any(r.name == "from-router" for r in results)


@pytest.mark.asyncio
async def test_registry_routers_param_multiple_routers():
    router_a = ProbeRouter()
    router_a.add(MemoryProbe(name="a"))

    router_b = ProbeRouter()
    router_b.add(MemoryProbe(name="b"))

    app = FastAPI()
    registry = HealthRegistry(app, routers=[router_a, router_b])
    results = await registry.run_all()
    names = {r.name for r in results}
    assert "a" in names and "b" in names


@pytest.mark.asyncio
async def test_registry_routers_param_preserves_criticality():
    router = ProbeRouter()
    router.add(MemoryProbe(name="optional"), critical=False)

    app = FastAPI()
    registry = HealthRegistry(app, routers=[router])
    probe_entry = registry._probes[0]
    assert probe_entry[1] is False  # critical flag


@pytest.mark.asyncio
async def test_include_router_on_registry_adds_probes():
    router = ProbeRouter()
    router.add(MemoryProbe(name="via-include"))

    app = FastAPI()
    registry = HealthRegistry(app)
    registry.include_router(router)

    results = await registry.run_all()
    assert any(r.name == "via-include" for r in results)


def test_include_router_on_registry_returns_self():
    router = ProbeRouter()
    app = FastAPI()
    registry = HealthRegistry(app)
    result = registry.include_router(router)
    assert result is registry


@pytest.mark.asyncio
async def test_include_router_on_registry_preserves_criticality():
    router = ProbeRouter()
    router.add(MemoryProbe(name="opt"), critical=False)

    app = FastAPI()
    registry = HealthRegistry(app)
    registry.include_router(router)

    assert registry._probes[0][1] is False


@pytest.mark.asyncio
async def test_include_router_on_registry_deduplicates():
    probe = MemoryProbe(name="shared")
    router = ProbeRouter()
    router.add(probe)

    app = FastAPI()
    registry = HealthRegistry(app)
    registry.add(probe)
    registry.include_router(router)

    assert len(registry._probes) == 1


def test_include_router_on_registry_is_chainable():
    router_a = ProbeRouter()
    router_b = ProbeRouter()
    app = FastAPI()
    registry = HealthRegistry(app)
    result = registry.include_router(router_a).include_router(router_b)
    assert result is registry
