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


# ---------------------------------------------------------------------------
# Feature: HTTP method in description
# ---------------------------------------------------------------------------

def test_discover_routes_description_includes_method():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)

    @app.get("/items")
    async def list_items():
        return {}

    registry.discover_routes()

    probe = next(p for p, _ in registry._probes if p.name == "list_items")
    assert probe.description == "GET /items"


def test_discover_routes_description_excludes_head():
    """HEAD is auto-added by FastAPI for GET routes and must not appear in the description."""
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)

    @app.get("/items")
    async def list_items():
        return {}

    registry.discover_routes()

    probe = next(p for p, _ in registry._probes if p.name == "list_items")
    assert "HEAD" not in probe.description


def test_discover_routes_description_ws_prefix():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)

    @app.websocket("/ws/chat")
    async def chat(websocket: WebSocket):
        await websocket.accept()
        await websocket.close()

    registry.discover_routes()

    probe = next(p for p, _ in registry._probes if p.name == "chat")
    assert probe.description == "WS /ws/chat"


def test_watch_router_description_includes_method():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    router = APIRouter()

    @router.post("/orders")
    async def create_order():
        return {}

    app.include_router(router)
    registry.watch_router(router)

    probe = next(p for p, _ in registry._probes if p.name == "create_order")
    assert probe.description == "POST /orders"


# ---------------------------------------------------------------------------
# Feature: FastAPI route tags → probe tags
# ---------------------------------------------------------------------------

def test_discover_routes_merges_fastapi_route_tags():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)

    @app.get("/items", tags=["store", "v2"])
    async def list_items():
        return {}

    registry.discover_routes()

    probe = next(p for p, _ in registry._probes if p.name == "list_items")
    assert "store" in probe.tags
    assert "v2" in probe.tags


def test_discover_routes_user_tags_appended_after_route_tags():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)

    @app.get("/items", tags=["store"])
    async def list_items():
        return {}

    registry.discover_routes(tags=["monitored"])

    probe = next(p for p, _ in registry._probes if p.name == "list_items")
    assert "store" in probe.tags
    assert "monitored" in probe.tags


def test_discover_routes_tags_deduplicated():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)

    @app.get("/items", tags=["api"])
    async def list_items():
        return {}

    registry.discover_routes(tags=["api"])  # "api" appears in both route and user tags

    probe = next(p for p, _ in registry._probes if p.name == "list_items")
    assert probe.tags.count("api") == 1


def test_watch_router_merges_fastapi_route_tags():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    router = APIRouter()

    @router.get("/products", tags=["catalog"])
    async def list_products():
        return {}

    app.include_router(router)
    registry.watch_router(router, tags=["store"])

    probe = next(p for p, _ in registry._probes if p.name == "list_products")
    assert "catalog" in probe.tags
    assert "store" in probe.tags


# ---------------------------------------------------------------------------
# Feature: include_methods
# ---------------------------------------------------------------------------

def test_discover_routes_include_methods_filters_routes():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)

    @app.get("/items")
    async def list_items():
        return {}

    @app.post("/items")
    async def create_item():
        return {}

    @app.delete("/items/{id}")
    async def delete_item(id: int):
        return {}

    registry.discover_routes(include_methods=["GET"])

    names = {p.name for p, _ in registry._probes}
    assert "list_items" in names
    assert "create_item" not in names
    assert "delete_item" not in names


def test_discover_routes_include_methods_case_insensitive():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)

    @app.get("/items")
    async def list_items():
        return {}

    registry.discover_routes(include_methods=["get"])

    names = {p.name for p, _ in registry._probes}
    assert "list_items" in names


def test_discover_routes_include_methods_websocket_always_included():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)

    @app.get("/items")
    async def list_items():
        return {}

    @app.websocket("/ws/chat")
    async def chat(websocket: WebSocket):
        await websocket.accept()
        await websocket.close()

    registry.discover_routes(include_methods=["POST"])  # GET excluded, but WS should pass

    names = {p.name for p, _ in registry._probes}
    assert "list_items" not in names
    assert "chat" in names


def test_watch_router_include_methods_filters_routes():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    router = APIRouter()

    @router.get("/products")
    async def list_products():
        return {}

    @router.post("/products")
    async def create_product():
        return {}

    app.include_router(router)
    registry.watch_router(router, include_methods=["POST"])

    names = {p.name for p, _ in registry._probes}
    assert "create_product" in names
    assert "list_products" not in names


# ---------------------------------------------------------------------------
# Feature: glob-pattern exclude_paths
# ---------------------------------------------------------------------------

def test_discover_routes_exclude_glob_prefix():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)

    @app.get("/internal/ping")
    async def ping():
        return {}

    @app.get("/internal/metrics")
    async def metrics():
        return {}

    @app.get("/api/items")
    async def list_items():
        return {}

    registry.discover_routes(exclude_paths=["/internal/*"])

    names = {p.name for p, _ in registry._probes}
    assert "list_items" in names
    assert "ping" not in names
    assert "metrics" not in names


def test_discover_routes_exclude_glob_question_mark():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)

    @app.get("/v1/items")
    async def v1_items():
        return {}

    @app.get("/v2/items")
    async def v2_items():
        return {}

    @app.get("/api/items")
    async def api_items():
        return {}

    registry.discover_routes(exclude_paths=["/v?/items"])

    names = {p.name for p, _ in registry._probes}
    assert "api_items" in names
    assert "v1_items" not in names
    assert "v2_items" not in names


def test_watch_router_exclude_glob_pattern():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    router = APIRouter()

    @router.get("/admin/users")
    async def admin_users():
        return {}

    @router.get("/admin/settings")
    async def admin_settings():
        return {}

    @router.get("/api/status")
    async def api_status():
        return {}

    app.include_router(router)
    registry.watch_router(router, exclude_paths=["/admin/*"])

    names = {p.name for p, _ in registry._probes}
    assert "api_status" in names
    assert "admin_users" not in names
    assert "admin_settings" not in names


# ---------------------------------------------------------------------------
# Feature: refresh=True
# ---------------------------------------------------------------------------

def test_discover_routes_refresh_picks_up_new_routes():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)

    @app.get("/items")
    async def list_items():
        return {}

    registry.discover_routes()
    names_before = {p.name for p, _ in registry._probes}
    assert "list_items" in names_before

    # Add a new route after the first discover call
    @app.get("/users")
    async def list_users():
        return {}

    registry.discover_routes(refresh=True)

    names_after = {p.name for p, _ in registry._probes}
    assert "list_users" in names_after


def test_discover_routes_refresh_reinstruments_discover_probes():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)

    @app.get("/items")
    async def list_items():
        return {}

    registry.discover_routes(tags=["v1"])
    probe_before = next(p for p, _ in registry._probes if p.name == "list_items")
    assert "v1" in probe_before.tags

    registry.discover_routes(tags=["v2"], refresh=True)

    probes_after = {p.name: p for p, _ in registry._probes}
    assert "v2" in probes_after["list_items"].tags
    assert "v1" not in probes_after["list_items"].tags


def test_discover_routes_refresh_does_not_override_manual():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    manual_probe = FastAPIRouteProbe(name="manual-checkout")

    @app.post("/checkout")
    @manual_probe.watch
    async def checkout():
        return {}

    registry.add(manual_probe)
    registry.discover_routes(refresh=True)

    # Manual probe should still be the one in the registry
    names = [p.name for p, _ in registry._probes]
    assert names.count("manual-checkout") == 1
    assert "checkout" not in names


def test_discover_routes_refresh_does_not_override_watch_router():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    router = APIRouter()

    @router.get("/items")
    async def list_items():
        return {}

    app.include_router(router)
    registry.watch_router(router, tags=["router"])
    registry.discover_routes(tags=["discover"], refresh=True)

    probe = next(p for p, _ in registry._probes if p.name == "list_items")
    assert "router" in probe.tags
    assert "discover" not in probe.tags


def test_discover_routes_refresh_evicts_old_probe():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)

    @app.get("/items")
    async def list_items():
        return {}

    registry.discover_routes()
    count_before = len(registry._probes)

    registry.discover_routes(refresh=True)
    count_after = len(registry._probes)

    # Same count — old probe evicted, new one added
    assert count_after == count_before


# ---------------------------------------------------------------------------
# Feature: name_fn
# ---------------------------------------------------------------------------

def test_discover_routes_name_fn_customises_probe_name():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)

    @app.get("/items")
    async def list_items():
        return {}

    registry.discover_routes(name_fn=lambda r: r.path)

    names = {p.name for p, _ in registry._probes}
    assert "/items" in names
    assert "list_items" not in names


def test_discover_routes_name_fn_receives_route_object():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    seen_routes = []

    @app.get("/items")
    async def list_items():
        return {}

    def capture_name(route):
        seen_routes.append(route)
        return route.name

    registry.discover_routes(name_fn=capture_name)
    assert len(seen_routes) == 1


def test_watch_router_name_fn_customises_probe_name():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    router = APIRouter()

    @router.get("/products")
    async def list_products():
        return {}

    app.include_router(router)
    registry.watch_router(router, name_fn=lambda r: f"router:{r.name}")

    names = {p.name for p, _ in registry._probes}
    assert "router:list_products" in names


def test_discover_routes_name_fn_none_uses_route_name():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)

    @app.get("/items")
    async def list_items():
        return {}

    registry.discover_routes(name_fn=None)

    names = {p.name for p, _ in registry._probes}
    assert "list_items" in names


# ---------------------------------------------------------------------------
# include_paths — discover_routes whitelist
# ---------------------------------------------------------------------------

def test_discover_routes_include_paths_only_monitors_matching():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)

    @app.get("/api/items")
    async def api_items(): return {}

    @app.get("/internal/secret")
    async def internal_secret(): return {}

    registry.discover_routes(include_paths=["/api/*"])

    names = {p.name for p, _ in registry._probes}
    assert "api_items" in names
    assert "internal_secret" not in names


def test_discover_routes_include_paths_empty_list_monitors_all():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)

    @app.get("/a")
    async def route_a(): return {}

    @app.get("/b")
    async def route_b(): return {}

    registry.discover_routes(include_paths=[])

    names = {p.name for p, _ in registry._probes}
    assert "route_a" in names
    assert "route_b" in names


def test_discover_routes_exclude_takes_precedence_over_include():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)

    @app.get("/api/items")
    async def api_items(): return {}

    @app.get("/api/admin")
    async def api_admin(): return {}

    registry.discover_routes(include_paths=["/api/*"], exclude_paths=["/api/admin"])

    names = {p.name for p, _ in registry._probes}
    assert "api_items" in names
    assert "api_admin" not in names


def test_discover_routes_include_paths_glob_pattern():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)

    @app.get("/v1/users")
    async def v1_users(): return {}

    @app.get("/v2/users")
    async def v2_users(): return {}

    @app.get("/health-check")
    async def health_check(): return {}

    registry.discover_routes(include_paths=["/v1/*", "/v2/*"])

    names = {p.name for p, _ in registry._probes}
    assert "v1_users" in names
    assert "v2_users" in names
    assert "health_check" not in names


# ---------------------------------------------------------------------------
# include_paths — watch_router whitelist
# ---------------------------------------------------------------------------

def test_watch_router_include_paths_only_monitors_matching():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    router = APIRouter()

    @router.get("/users/profile")
    async def user_profile(): return {}

    @router.get("/users/settings")
    async def user_settings(): return {}

    app.include_router(router)
    registry.watch_router(router, include_paths=["/users/profile"])

    names = {p.name for p, _ in registry._probes}
    assert "user_profile" in names
    assert "user_settings" not in names


def test_watch_router_exclude_takes_precedence_over_include():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    router = APIRouter()

    @router.get("/api/items")
    async def api_items(): return {}

    @router.get("/api/admin")
    async def api_admin(): return {}

    app.include_router(router)
    registry.watch_router(router, include_paths=["/api/*"], exclude_paths=["/api/admin"])

    names = {p.name for p, _ in registry._probes}
    assert "api_items" in names
    assert "api_admin" not in names


# ---------------------------------------------------------------------------
# watch_router group=True
# ---------------------------------------------------------------------------

def test_watch_router_group_true_registers_probes():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    router = APIRouter()

    @router.get("/orders")
    async def list_orders(): return {}

    @router.get("/orders/{id}")
    async def get_order(): return {}

    app.include_router(router)
    registry.watch_router(router, group=True)

    names = {p.name for p, _ in registry._probes}
    assert "list_orders" in names
    assert "get_order" in names


def test_watch_router_group_true_propagates_tags():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    router = APIRouter()

    @router.get("/products")
    async def list_products(): return {}

    app.include_router(router)
    registry.watch_router(router, tags=["store"], group=True)

    probes = {p.name: p for p, _ in registry._probes}
    assert "store" in probes["list_products"].tags


def test_watch_router_group_false_default_registers_normally():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    router = APIRouter()

    @router.get("/things")
    async def list_things(): return {}

    app.include_router(router)
    registry.watch_router(router, group=False, tags=["things"])

    probes = {p.name: p for p, _ in registry._probes}
    assert "list_things" in probes
    assert "things" in probes["list_things"].tags


# ---------------------------------------------------------------------------
# Dashboard search box
# ---------------------------------------------------------------------------

def test_dashboard_contains_search_input():
    from fastapi_watch.dashboard import render_dashboard
    from fastapi_watch.models import HealthReport, ProbeResult

    report = HealthReport(
        status="healthy",
        probes=[
            ProbeResult(name="my-probe", status="healthy", latency_ms=1.0),
        ],
    )
    html = render_dashboard(report, stream_url="/health/status/stream", maintenance_banner=False)
    assert 'id="probe-search"' in html
    assert 'class="probe-search"' in html


def test_dashboard_search_uses_data_name_attribute():
    from fastapi_watch.dashboard import render_dashboard
    from fastapi_watch.models import HealthReport, ProbeResult

    report = HealthReport(
        status="healthy",
        probes=[
            ProbeResult(name="payments-api", status="healthy", latency_ms=1.0),
        ],
    )
    html = render_dashboard(report, stream_url="/health/status/stream", maintenance_banner=False)
    assert 'data-name="payments-api"' in html
