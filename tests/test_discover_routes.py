"""Tests for registry.discover_routes() and registry.watch_router()."""
import pytest
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.routing import APIRouter
from fastapi.testclient import TestClient
from fastapi_watch import HealthRegistry, FastAPIRouteProbe, FastAPIWebSocketProbe
from fastapi_watch.models import ProbeStatus
from fastapi_watch.probes.noop import NoOpProbe


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app_with_routes():
    """Return (app, registry, client) with two routes pre-registered."""
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)

    @app.get("/items")
    async def list_items():
        return {"items": []}

    @app.get("/users")
    async def list_users():
        return {"users": []}

    return app, registry, TestClient(app)


# ---------------------------------------------------------------------------
# discover_routes — basic instrumentation
# ---------------------------------------------------------------------------

def test_discover_routes_creates_probes_for_each_route():
    app, registry, client = _make_app_with_routes()
    registry.discover_routes()

    names = {p.name for p, _ in registry._probes}
    assert "list_items" in names
    assert "list_users" in names


def test_discover_routes_routes_still_serve_responses():
    app, registry, client = _make_app_with_routes()
    registry.discover_routes()

    assert client.get("/items").status_code == 200
    assert client.get("/users").status_code == 200


def test_discover_routes_probes_appear_in_health_status():
    app, registry, client = _make_app_with_routes()
    registry.discover_routes()

    client.get("/items")

    resp = client.get("/health/status")
    names = {p["name"] for p in resp.json()["probes"]}
    assert "list_items" in names


def test_discover_routes_records_real_traffic():
    app, registry, client = _make_app_with_routes()
    registry.discover_routes()

    client.get("/items")
    client.get("/items")

    resp = client.get("/health/status")
    probes = {p["name"]: p for p in resp.json()["probes"]}
    assert probes["list_items"]["details"]["request_count"] == 2


# ---------------------------------------------------------------------------
# discover_routes — health prefix exclusion
# ---------------------------------------------------------------------------

def test_discover_routes_skips_health_prefix_routes():
    app, registry, client = _make_app_with_routes()
    registry.discover_routes()

    names = {p.name for p, _ in registry._probes}
    assert not any("health" in name for name in names)


def test_discover_routes_skips_custom_prefix_routes():
    app = FastAPI()
    registry = HealthRegistry(app, prefix="/ops/health", poll_interval_ms=None)

    @app.get("/api/items")
    async def list_items():
        return {}

    registry.discover_routes()

    names = {p.name for p, _ in registry._probes}
    assert "list_items" in names
    assert not any("ops" in name or "health" in name for name in names)


# ---------------------------------------------------------------------------
# discover_routes — exclude_paths
# ---------------------------------------------------------------------------

def test_discover_routes_respects_exclude_paths():
    app, registry, client = _make_app_with_routes()
    registry.discover_routes(exclude_paths=["/users"])

    names = {p.name for p, _ in registry._probes}
    assert "list_items" in names
    assert "list_users" not in names


def test_discover_routes_excluded_route_still_works():
    app, registry, client = _make_app_with_routes()
    registry.discover_routes(exclude_paths=["/users"])

    assert client.get("/users").status_code == 200


# ---------------------------------------------------------------------------
# discover_routes — probe_kwargs forwarding
# ---------------------------------------------------------------------------

def test_discover_routes_forwards_probe_kwargs():
    app, registry, client = _make_app_with_routes()
    registry.discover_routes(max_error_rate=0.01)

    for probe, _ in registry._probes:
        if hasattr(probe, "max_error_rate"):
            assert probe.max_error_rate == 0.01


def test_discover_routes_critical_flag():
    app, registry, client = _make_app_with_routes()
    registry.discover_routes(critical=False)

    for _, critical in registry._probes:
        assert critical is False


# ---------------------------------------------------------------------------
# discover_routes — tags
# ---------------------------------------------------------------------------

def test_discover_routes_tags_set_on_probes():
    app, registry, client = _make_app_with_routes()
    registry.discover_routes(tags=["api"])

    for probe, _ in registry._probes:
        assert "api" in probe.tags


def test_discover_routes_tags_filter_health_ready():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)

    @app.get("/items")
    async def list_items():
        return {}

    @app.get("/admin")
    async def admin():
        return {}

    registry.discover_routes(tags=["api"])
    client = TestClient(app)

    resp = client.get("/health/ready?tag=api")
    assert resp.status_code == 200


def test_discover_routes_tag_filter_excludes_untagged():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(NoOpProbe(name="infra"))  # no tags

    @app.get("/items")
    async def list_items():
        return {}

    registry.discover_routes(tags=["api"])
    client = TestClient(app)

    resp = client.get("/health/status?tag=api")
    names = {p["name"] for p in resp.json()["probes"]}
    assert "list_items" in names
    assert "infra" not in names


# ---------------------------------------------------------------------------
# discover_routes — idempotency
# ---------------------------------------------------------------------------

def test_discover_routes_idempotent():
    app, registry, client = _make_app_with_routes()
    registry.discover_routes()
    registry.discover_routes()  # second call should not double-add probes

    names = [p.name for p, _ in registry._probes]
    assert names.count("list_items") == 1
    assert names.count("list_users") == 1


# ---------------------------------------------------------------------------
# watch_router — basic instrumentation
# ---------------------------------------------------------------------------

def test_watch_router_instruments_all_router_routes():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    router = APIRouter()

    @router.get("/products")
    async def list_products():
        return {}

    @router.get("/products/{pid}")
    async def get_product(pid: int):
        return {"id": pid}

    app.include_router(router, prefix="/api")
    registry.watch_router(router)

    names = {p.name for p, _ in registry._probes}
    assert "list_products" in names
    assert "get_product" in names


def test_watch_router_routes_still_serve_responses():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    router = APIRouter()

    @router.get("/products")
    async def list_products():
        return {"items": []}

    app.include_router(router, prefix="/api")
    registry.watch_router(router)

    client = TestClient(app)
    assert client.get("/api/products").status_code == 200


def test_watch_router_records_real_traffic():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    router = APIRouter()

    @router.get("/products")
    async def list_products():
        return {}

    app.include_router(router, prefix="/api")
    registry.watch_router(router)

    client = TestClient(app)
    client.get("/api/products")
    client.get("/api/products")

    resp = client.get("/health/status")
    probes = {p["name"]: p for p in resp.json()["probes"]}
    assert probes["list_products"]["details"]["request_count"] == 2


def test_watch_router_only_instruments_own_routes():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)

    @app.get("/other")
    async def other():
        return {}

    router = APIRouter()

    @router.get("/products")
    async def list_products():
        return {}

    app.include_router(router)
    registry.watch_router(router)

    names = {p.name for p, _ in registry._probes}
    assert "list_products" in names
    assert "other" not in names


# ---------------------------------------------------------------------------
# watch_router — tags
# ---------------------------------------------------------------------------

def test_watch_router_tags_set_on_probes():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    router = APIRouter()

    @router.get("/products")
    async def list_products():
        return {}

    app.include_router(router)
    registry.watch_router(router, tags=["store"])

    for probe, _ in registry._probes:
        assert "store" in probe.tags


def test_watch_router_tags_filter_health_status():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(NoOpProbe(name="infra"))

    router = APIRouter()

    @router.get("/products")
    async def list_products():
        return {}

    app.include_router(router)
    registry.watch_router(router, tags=["store"])

    client = TestClient(app)
    resp = client.get("/health/status?tag=store")
    names = {p["name"] for p in resp.json()["probes"]}
    assert "list_products" in names
    assert "infra" not in names


def test_multiple_routers_different_tags():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)

    users_router = APIRouter()
    orders_router = APIRouter()

    @users_router.get("/users")
    async def list_users():
        return {}

    @orders_router.get("/orders")
    async def list_orders():
        return {}

    app.include_router(users_router)
    app.include_router(orders_router)
    registry.watch_router(users_router, tags=["users"])
    registry.watch_router(orders_router, tags=["orders"])

    client = TestClient(app)

    resp = client.get("/health/status?tag=users")
    names = {p["name"] for p in resp.json()["probes"]}
    assert "list_users" in names
    assert "list_orders" not in names

    resp = client.get("/health/status?tag=orders")
    names = {p["name"] for p in resp.json()["probes"]}
    assert "list_orders" in names
    assert "list_users" not in names


# ---------------------------------------------------------------------------
# watch_router — exclude_paths
# ---------------------------------------------------------------------------

def test_watch_router_respects_exclude_paths():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    router = APIRouter()

    @router.get("/products")
    async def list_products():
        return {}

    @router.get("/internal")
    async def internal():
        return {}

    app.include_router(router)
    registry.watch_router(router, exclude_paths=["/internal"])

    names = {p.name for p, _ in registry._probes}
    assert "list_products" in names
    assert "internal" not in names


# ---------------------------------------------------------------------------
# Priority system
# ---------------------------------------------------------------------------

def test_probe_watch_priority_over_discover_routes():
    """@probe.watch takes highest priority — discover_routes must not override it."""
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    manual_probe = FastAPIRouteProbe(name="manual-checkout")

    @app.post("/checkout")
    @manual_probe.watch
    async def checkout():
        return {}

    registry.add(manual_probe)
    registry.discover_routes()

    names = [p.name for p, _ in registry._probes]
    # manual probe should appear exactly once; no auto-discovered duplicate
    assert names.count("manual-checkout") == 1
    # discover_routes must not have created an additional probe for /checkout
    assert "checkout" not in names


def test_probe_watch_priority_over_watch_router():
    """@probe.watch takes highest priority — watch_router must not override it."""
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    router = APIRouter()
    manual_probe = FastAPIRouteProbe(name="manual-items")

    @router.get("/items")
    @manual_probe.watch
    async def list_items():
        return {}

    app.include_router(router)
    registry.add(manual_probe)
    registry.watch_router(router)

    names = [p.name for p, _ in registry._probes]
    assert names.count("manual-items") == 1
    assert "list_items" not in names


def test_watch_router_priority_over_discover_routes():
    """watch_router overrides discover_routes instrumentation."""
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    router = APIRouter()

    @router.get("/items")
    async def list_items():
        return {}

    app.include_router(router)
    registry.discover_routes(tags=["discover"])
    registry.watch_router(router, tags=["router"])

    # The router-level probe should be present; the discover probe should be gone
    probes_by_name = {p.name: p for p, _ in registry._probes}
    probe = probes_by_name.get("list_items")
    assert probe is not None
    assert "router" in probe.tags
    assert "discover" not in probe.tags

    # Exactly one probe for list_items — no ghost from discover_routes
    names = [p.name for p, _ in registry._probes]
    assert names.count("list_items") == 1


def test_discover_routes_skips_watch_router_routes():
    """discover_routes (called after watch_router) must not override watch_router."""
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    router = APIRouter()

    @router.get("/items")
    async def list_items():
        return {}

    app.include_router(router)
    registry.watch_router(router, tags=["router"])
    registry.discover_routes(tags=["discover"])

    probes_by_name = {p.name: p for p, _ in registry._probes}
    probe = probes_by_name.get("list_items")
    assert probe is not None
    assert "router" in probe.tags
    assert "discover" not in probe.tags

    names = [p.name for p, _ in registry._probes]
    assert names.count("list_items") == 1


def test_ghost_probe_evicted_when_watch_router_overrides_discover():
    """When watch_router replaces a discover probe, the old probe must be removed."""
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    router = APIRouter()

    @router.get("/items")
    async def list_items():
        return {}

    app.include_router(router)
    registry.discover_routes()

    count_before = len(registry._probes)
    registry.watch_router(router, tags=["router"])
    count_after = len(registry._probes)

    # Same number of probes — old one was evicted and new one added
    assert count_after == count_before


# ---------------------------------------------------------------------------
# WebSocket auto-discovery
# ---------------------------------------------------------------------------

def test_discover_routes_instruments_websocket_routes():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)

    @app.websocket("/ws/chat")
    async def chat(websocket: WebSocket):
        await websocket.accept()
        await websocket.close()

    registry.discover_routes()

    names = {p.name for p, _ in registry._probes}
    assert "chat" in names
    assert isinstance(next(p for p, _ in registry._probes if p.name == "chat"), FastAPIWebSocketProbe)


def test_watch_router_instruments_websocket_routes():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    router = APIRouter()

    @router.websocket("/ws/feed")
    async def feed(websocket: WebSocket):
        await websocket.accept()
        await websocket.close()

    app.include_router(router)
    registry.watch_router(router)

    names = {p.name for p, _ in registry._probes}
    assert "feed" in names
    assert isinstance(next(p for p, _ in registry._probes if p.name == "feed"), FastAPIWebSocketProbe)


def test_discover_routes_ws_probe_kwargs_forwarded():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)

    @app.websocket("/ws/stream")
    async def stream(websocket: WebSocket):
        await websocket.accept()
        await websocket.close()

    registry.discover_routes(ws_probe_kwargs={"min_active_connections": 1})

    probe = next(p for p, _ in registry._probes if p.name == "stream")
    assert isinstance(probe, FastAPIWebSocketProbe)
    assert probe.min_active_connections == 1


def test_discover_routes_websocket_probe_tracks_connections():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)

    @app.websocket("/ws/echo")
    async def echo(websocket: WebSocket):
        await websocket.accept()
        data = await websocket.receive_text()
        await websocket.send_text(data)

    registry.discover_routes()

    client = TestClient(app)
    with client.websocket_connect("/ws/echo") as ws:
        ws.send_text("ping")
        ws.receive_text()

    probe = next(p for p, _ in registry._probes if p.name == "echo")
    assert probe._total_connections == 1


# ---------------------------------------------------------------------------
# discover_routes — chaining
# ---------------------------------------------------------------------------

def test_discover_routes_returns_registry_for_chaining():
    app, registry, _ = _make_app_with_routes()
    result = registry.discover_routes()
    assert result is registry


def test_watch_router_returns_registry_for_chaining():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    router = APIRouter()

    @router.get("/items")
    async def list_items():
        return {}

    app.include_router(router)
    result = registry.watch_router(router)
    assert result is registry
