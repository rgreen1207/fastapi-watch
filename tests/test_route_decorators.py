"""Monitoring a route must never strip the application's own decorators.

discover_routes() and watch_router() used to follow ``__wrapped__`` on any
endpoint, so a ``functools.wraps`` auth decorator was removed and the bare
handler served: a route answering 403 answered 200 with its data.  Runs on
both FastAPI layouts (flat route copies before 0.137, _IncludedRouter after).
"""
import functools

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from fastapi_watch import HealthRegistry


def require_admin(fn):
    @functools.wraps(fn)
    async def guarded(*args, **kwargs):
        raise HTTPException(status_code=403, detail="forbidden")

    return guarded


def _app():
    app = FastAPI()

    @app.get("/admin/secret")
    @require_admin
    async def secret():
        return {"secret": "s3cr3t"}

    router = APIRouter()

    @router.get("/secret")
    @require_admin
    async def router_secret():
        return {"secret": "s3cr3t"}

    app.include_router(router, prefix="/r")
    return app, router


def _assert_guarded(app):
    client = TestClient(app)
    for path in ("/admin/secret", "/r/secret"):
        resp = client.get(path)
        assert resp.status_code == 403, path
        assert "s3cr3t" not in resp.text


def _probe(registry, name):
    return next(p for p, _ in registry._probes if p.name == name)


@pytest.mark.parametrize(
    "monitor",
    [
        lambda reg, router: reg.discover_routes(),
        lambda reg, router: reg.watch_router(router),
        lambda reg, router: reg.discover_routes().discover_routes(refresh=True),
        lambda reg, router: reg.discover_routes().watch_router(router),  # router replaces discover
        lambda reg, router: reg.watch_router(router).discover_routes(refresh=True),
    ],
    ids=["discover", "watch_router", "refresh", "discover_then_router", "router_then_refresh"],
)
def test_auth_decorator_survives_monitoring(monitor):
    app, router = _app()
    _assert_guarded(app)
    registry = HealthRegistry(app, poll_interval_ms=None)
    monitor(registry, router)
    _assert_guarded(app)
    # ...and the traffic is still recorded, as 403s.
    probe = _probe(registry, "router_secret")
    assert probe._error_count == 0 and probe._call_count >= 1


def test_refresh_does_not_double_wrap():
    app, _ = _app()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.discover_routes()
    registry.discover_routes(refresh=True)
    TestClient(app).get("/admin/secret")
    assert _probe(registry, "secret")._call_count == 1


def test_watch_router_does_not_claim_a_route_sharing_only_the_inner_function():
    async def handler():
        return {"ok": True}

    def public(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            return await fn(*args, **kwargs)

        return wrapper

    app = FastAPI()
    app.get("/admin/x", name="admin_x")(require_admin(handler))
    router = APIRouter()
    router.get("/x", name="public_x")(public(handler))
    app.include_router(router, prefix="/r")

    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.watch_router(router)
    assert [p.name for p, _ in registry._probes] == ["public_x"]
    assert TestClient(app).get("/admin/x").status_code == 403
