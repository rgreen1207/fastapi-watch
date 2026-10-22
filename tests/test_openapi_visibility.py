"""Health endpoints stay out of the application's OpenAPI schema unless asked for."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi_watch import HealthRegistry


def _paths(app: FastAPI) -> set[str]:
    return set(TestClient(app).get("/openapi.json").json()["paths"])


@pytest.mark.parametrize("auth", [None, {"type": "apikey", "key": "k"}])
def test_health_routes_hidden_by_default(auth):
    app = FastAPI()

    @app.get("/items")
    async def items():
        return []

    HealthRegistry(app, auth=auth)
    assert _paths(app) == {"/items"}


def test_hidden_routes_still_served():
    app = FastAPI()
    HealthRegistry(app)
    assert TestClient(app).get("/health/live").status_code == 200


def test_include_in_schema_lists_every_health_route():
    app = FastAPI()
    registry = HealthRegistry(app, auth={"type": "apikey", "key": "k"}, include_in_schema=True)
    served = {r.path for r in app.routes if getattr(r, "path", "").startswith(registry.prefix)}
    assert served and served <= _paths(app)
    # The maintenance body stays documented when listed.
    assert "requestBody" in TestClient(app).get("/openapi.json").json()["paths"]["/health/maintenance"]["post"]
