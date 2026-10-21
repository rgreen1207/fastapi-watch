"""Passive probes must not publish exception messages (credentials, personal data) by default."""
import json
import logging

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from fastapi_watch import HealthRegistry
from fastapi_watch.probes import FastAPIRouteProbe, PassiveProbe, PostgreSQLProbe, SqlAlchemyProbe

# What a SQLAlchemy IntegrityError looks like: the bound parameters are in the message.
_SECRET_MESSAGE = (
    "(psycopg.errors.UniqueViolation) duplicate key value violates unique constraint "
    "[SQL: INSERT INTO users (email, ssn) VALUES (%(email)s, %(ssn)s)] "
    "[parameters: {'email': 'jane@example.com', 'ssn': '123-45-6789'}]"
)


class IntegrityError(Exception):
    pass


def _fail(probe: PassiveProbe):
    @probe.watch
    def insert_user():
        raise IntegrityError(_SECRET_MESSAGE)

    with pytest.raises(IntegrityError):
        insert_user()


async def test_last_error_names_only_the_exception_type_by_default():
    probe = PassiveProbe(name="db")
    _fail(probe)

    result = await probe.check()

    assert result.details["last_error"] == "IntegrityError"
    assert result.details["error_types"] == {"IntegrityError": 1}
    dumped = json.dumps(result.model_dump(mode="json"))
    assert "jane@example.com" not in dumped
    assert "123-45-6789" not in dumped


async def test_hidden_message_is_logged_at_debug_level(caplog):
    probe = PassiveProbe(name="db")
    with caplog.at_level(logging.DEBUG, logger="fastapi_watch"):
        _fail(probe)

    record = next(r for r in caplog.records if r.exc_info)
    assert record.levelno == logging.DEBUG
    assert "jane@example.com" in str(record.exc_info[1])


async def test_expose_error_messages_opts_in_to_the_full_message():
    probe = PassiveProbe(name="db", expose_error_messages=True)
    _fail(probe)

    result = await probe.check()

    assert result.details["last_error"] == f"IntegrityError: {_SECRET_MESSAGE}"


@pytest.mark.parametrize("cls", [PostgreSQLProbe, SqlAlchemyProbe])
async def test_passive_subclasses_accept_expose_error_messages(cls):
    hidden, shown = cls(), cls(expose_error_messages=True)
    _fail(hidden)
    _fail(shown)

    assert (await hidden.check()).details["last_error"] == "IntegrityError"
    assert "jane@example.com" in (await shown.check()).details["last_error"]


def _route_app(probe: FastAPIRouteProbe) -> TestClient:
    app = FastAPI()
    HealthRegistry(app, poll_interval_ms=None).add(probe)

    @app.get("/users")
    @probe.watch
    async def create_user():
        raise HTTPException(status_code=503, detail="db down: postgres://admin:hunter2@db.internal/users")

    @app.get("/status-only")
    @probe.watch
    async def status_only():
        return JSONResponse({}, status_code=503)

    @app.get("/boom")
    @probe.watch
    async def boom():
        raise IntegrityError(_SECRET_MESSAGE)

    return TestClient(app, raise_server_exceptions=False)


def _status_details(client: TestClient, name: str) -> dict:
    probes = {p["name"]: p for p in client.get("/health/status").json()["probes"]}
    return probes[name]["details"]


def test_http_exception_detail_is_not_exposed_on_the_public_endpoint():
    client = _route_app(FastAPIRouteProbe(name="users"))
    client.get("/users")

    details = _status_details(client, "users")

    assert details["last_error"] == "HTTPException"
    assert details["last_status_code"] == 503
    assert "hunter2" not in json.dumps(details)


def test_unhandled_exception_message_is_not_exposed_on_the_public_endpoint():
    client = _route_app(FastAPIRouteProbe(name="users"))
    client.get("/boom")

    details = _status_details(client, "users")

    assert details["last_error"] == "IntegrityError"
    assert "jane@example.com" not in json.dumps(details)


def test_status_only_error_keeps_the_http_status_form():
    client = _route_app(FastAPIRouteProbe(name="users"))
    client.get("/status-only")

    assert _status_details(client, "users")["last_error"] == "HTTP 503"


def test_route_probe_can_opt_in_to_http_exception_detail():
    client = _route_app(FastAPIRouteProbe(name="users", expose_error_messages=True))
    client.get("/users")

    assert "hunter2" in _status_details(client, "users")["last_error"]
