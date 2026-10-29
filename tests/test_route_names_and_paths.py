"""Route discovery: unique probe names and full (prefixed) path matching.

Runs on both FastAPI layouts — flat route copies (<0.137) and _IncludedRouter
wrappers (0.137+), where routes keep their un-prefixed path.
"""
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRouter
from fastapi.testclient import TestClient

from fastapi_watch import HealthRegistry
from fastapi_watch.probes.noop import NoOpProbe


def _two_routers_same_handler_name():
    app = FastAPI()
    users, orders = APIRouter(), APIRouter()

    @users.get("/")
    async def list_items():
        return ["user"]

    @orders.get("/")
    async def list_items():  # noqa: F811 — the same handler name in two routers is ordinary
        return ["order"]

    app.include_router(users, prefix="/users")
    app.include_router(orders, prefix="/orders")
    return app


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def test_colliding_handler_names_get_distinct_probes_and_results():
    app = _two_routers_same_handler_name()
    registry = HealthRegistry(app)
    registry.discover_routes()
    client = TestClient(app)
    client.get("/users/")
    client.get("/orders/")

    names = [p.name for p, _ in registry._probes]
    assert names == ["list_items", "list_items:GET_orders"]
    report = client.get("/health/status").json()
    assert {p["name"]: p["details"]["request_count"] for p in report["probes"]} == {
        "list_items": 1,
        "list_items:GET_orders": 1,
    }


def test_unique_names_are_left_alone():
    app = FastAPI()

    @app.get("/a")
    async def alpha():
        return 1

    registry = HealthRegistry(app)
    registry.discover_routes()
    assert [p.name for p, _ in registry._probes] == ["alpha"]


def test_refresh_keeps_a_routes_own_name():
    app = _two_routers_same_handler_name()
    registry = HealthRegistry(app)
    registry.discover_routes()
    registry.discover_routes(refresh=True)
    assert sorted(p.name for p, _ in registry._probes) == ["list_items", "list_items:GET_orders"]


def test_invalid_name_from_name_fn_leaves_route_unpatched():
    app = FastAPI()

    @app.get("/a")
    async def alpha():
        return 1

    registry = HealthRegistry(app)
    with pytest.raises(ValueError, match="invalid characters"):
        registry.discover_routes(name_fn=lambda route: "has spaces")
    route = next(r for r in app.routes if getattr(r, "path", None) == "/a")
    assert getattr(route.dependant.call, "_fastapi_watch", None) is None
    assert registry._probes == []


def test_add_warns_on_a_different_probe_with_the_same_name(caplog):
    registry = HealthRegistry(FastAPI(), serve_routes=False)
    registry.add(NoOpProbe(name="db"))
    with caplog.at_level("WARNING", logger="fastapi_watch"):
        registry.add(NoOpProbe(name="db"))
    assert any("already registered as 'db'" in r.getMessage() for r in caplog.records)


def test_add_same_probe_twice_is_silent(caplog):
    registry = HealthRegistry(FastAPI(), serve_routes=False)
    probe = NoOpProbe(name="db")
    with caplog.at_level("WARNING", logger="fastapi_watch"):
        registry.add(probe).add(probe)
    assert len(registry._probes) == 1
    assert not caplog.records


def test_name_with_trailing_newline_is_rejected():
    registry = HealthRegistry(FastAPI(), serve_routes=False)
    with pytest.raises(ValueError):
        registry.add(NoOpProbe(name="db\n"))


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _app_with_internal_router():
    app = FastAPI()
    internal, public = APIRouter(), APIRouter()

    @internal.get("/secret")
    async def secret():
        return 1

    @public.get("/items")
    async def items():
        return 1

    app.include_router(internal, prefix="/internal")
    app.include_router(public, prefix="/api")
    return app


def test_exclude_paths_match_the_prefixed_path():
    registry = HealthRegistry(_app_with_internal_router())
    registry.discover_routes(exclude_paths=["/internal/*"])
    assert [p.name for p, _ in registry._probes] == ["items"]


def test_include_paths_match_the_prefixed_path():
    registry = HealthRegistry(_app_with_internal_router())
    registry.discover_routes(include_paths=["/api/*"])
    assert [p.name for p, _ in registry._probes] == ["items"]


def test_description_uses_the_prefixed_path():
    registry = HealthRegistry(_app_with_internal_router())
    registry.discover_routes()
    assert sorted(p.description for p, _ in registry._probes) == ["GET /api/items", "GET /internal/secret"]


def test_watch_router_matches_prefixed_paths():
    app = FastAPI()
    router = APIRouter()

    @router.get("/secret")
    async def secret():
        return 1

    @router.get("/open")
    async def open_route():
        return 1

    app.include_router(router, prefix="/internal")
    registry = HealthRegistry(app)
    registry.watch_router(router, exclude_paths=["/internal/secret"])
    assert [(p.name, p.description) for p, _ in registry._probes] == [("open_route", "GET /internal/open")]


def _app_with_nested_router():
    """A router whose routes all arrive through its own include_router()."""
    app = FastAPI()
    inner = APIRouter(prefix="/import")

    @inner.get("/db")
    async def import_db():
        return 1

    outer, top = APIRouter(), APIRouter()
    outer.include_router(inner)
    top.include_router(outer)
    app.include_router(top, prefix="/api")
    return app, outer


@pytest.mark.parametrize("warm", [False, True])
def test_watch_router_monitors_routes_from_nested_includes(warm):
    app, outer = _app_with_nested_router()
    client = TestClient(app)
    if warm:  # FastAPI 0.137+ builds its route cache on the first request
        client.get("/api/import/db")
    registry = HealthRegistry(app)
    registry.watch_router(outer)
    client.get("/api/import/db")

    assert [(p.name, p.description) for p, _ in registry._probes] == [("import_db", "GET /api/import/db")]
    report = client.get("/health/status").json()
    assert {p["name"]: p["details"]["request_count"] for p in report["probes"]} == {"import_db": 1}


def test_include_router_tags_reach_the_probe():
    app = FastAPI()
    inner = APIRouter(prefix="/i")

    @inner.get("/x", tags=["route"])
    async def x():
        return 1

    outer = APIRouter()
    outer.include_router(inner, tags=["inner"])
    app.include_router(outer, prefix="/api", tags=["outer"])
    registry = HealthRegistry(app)
    registry.discover_routes(tags=["extra"])
    [(probe, _)] = registry._probes
    assert sorted(probe.tags) == ["extra", "inner", "outer", "route"]


def test_router_included_twice_is_monitored_under_the_prefix_not_excluded():
    app = FastAPI()
    router = APIRouter()

    @router.get("/x")
    async def x():
        return 1

    app.include_router(router, prefix="/v1")
    app.include_router(router, prefix="/v2")
    registry = HealthRegistry(app)
    registry.discover_routes(exclude_paths=["/v1/*"])
    assert [(p.name, p.description) for p, _ in registry._probes] == [("x", "GET /v2/x")]


def test_watch_router_on_a_nested_router_not_in_the_app_uses_its_inner_prefix():
    _, outer = _app_with_nested_router()
    registry = HealthRegistry(FastAPI())
    registry.watch_router(outer)
    assert [(p.name, p.description) for p, _ in registry._probes] == [("import_db", "GET /import/db")]


def test_route_sharing_the_health_prefix_is_still_monitored():
    app = FastAPI()

    @app.get("/healthcheck")
    async def healthcheck():
        return 1

    registry = HealthRegistry(app)  # prefix "/health"
    registry.discover_routes()
    assert [p.name for p, _ in registry._probes] == ["healthcheck"]


@pytest.mark.parametrize("prefix", ["/ops", ""])
def test_registry_routes_are_never_monitored(prefix):
    app = FastAPI()

    @app.get("/work")
    async def work():
        return 1

    registry = HealthRegistry(app, prefix=prefix)
    registry.discover_routes()
    assert [p.name for p, _ in registry._probes] == ["work"]


# ---------------------------------------------------------------------------
# Mounted sub-applications
# ---------------------------------------------------------------------------

def _app_with_mounted_sub_app(tmp_path):
    from fastapi.staticfiles import StaticFiles

    sub = FastAPI()
    items = APIRouter(prefix="/items")

    @items.get("/{item_id}")
    async def get_item(item_id: int):
        return item_id

    sub.include_router(items, prefix="/api", tags=["sub"])
    HealthRegistry(sub)  # the sub-app's own health routes are never monitored

    app = FastAPI()
    app.mount("/t/{tenant}", sub)
    app.mount("/static", StaticFiles(directory=tmp_path))
    return app, items


@pytest.mark.parametrize("warm", [False, True])
def test_discover_routes_monitors_a_mounted_sub_app(tmp_path, warm):
    app, _ = _app_with_mounted_sub_app(tmp_path)
    client = TestClient(app)
    if warm:  # FastAPI 0.137+ builds the sub-app's route cache on the first request
        client.get("/t/acme/api/items/1")
    registry = HealthRegistry(app)
    registry.discover_routes()
    client.get("/t/acme/api/items/1")

    [(probe, _)] = registry._probes
    assert (probe.name, probe.description, probe.tags) == ("get_item", "GET /t/{tenant}/api/items/{item_id}", ["sub"])
    report = client.get("/health/status").json()
    assert {p["name"]: p["details"]["request_count"] for p in report["probes"]} == {"get_item": 1}


def test_path_filters_match_the_mount_path(tmp_path):
    app, _ = _app_with_mounted_sub_app(tmp_path)
    registry = HealthRegistry(app)
    registry.discover_routes(exclude_paths=["/t/*"])
    assert registry._probes == []


def test_watch_router_finds_a_router_inside_a_mounted_sub_app(tmp_path):
    app, items = _app_with_mounted_sub_app(tmp_path)
    registry = HealthRegistry(app)
    registry.watch_router(items)
    assert [(p.name, p.description) for p, _ in registry._probes] == [("get_item", "GET /t/{tenant}/api/items/{item_id}")]
