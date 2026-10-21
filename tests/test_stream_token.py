"""The authenticated dashboard hands its SSE stream a short-lived signed token.

A browser EventSource cannot send headers, so with header-based auth the
dashboard's live updates were always rejected.
"""
import asyncio
import base64
import hashlib
import re
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import fastapi_watch.registry as registry_module
from fastapi_watch import HealthRegistry
from fastapi_watch.probes.noop import NoOpProbe
from fastapi_watch.registry import _mint_stream_token, _stream_token_key, _valid_stream_token

_APIKEY = {"type": "apikey", "key": "s3cret-key"}
_HEADER = {"X-API-Key": "s3cret-key"}


def _app(auth) -> FastAPI:
    app = FastAPI()
    HealthRegistry(app, auth=auth, poll_interval_ms=None)  # single-fetch: streams end after one event
    return app


def _stream_url(html: str) -> str:
    return re.search(r'data-stream-url="([^"]+)"', html).group(1).replace("&amp;", "&")


def test_apikey_dashboard_stream_authenticates_with_its_token():
    client = TestClient(_app(_APIKEY))
    url = _stream_url(client.get("/health/dashboard", headers=_HEADER).text)

    assert "token=" in url
    resp = client.get(url)  # no header, as from EventSource
    assert resp.status_code == 200
    assert "\ndata: " in resp.text or resp.text.startswith("data: ")


def test_stream_without_token_or_header_is_rejected():
    assert TestClient(_app(_APIKEY)).get("/health/status/stream").status_code == 403


def test_stream_with_header_still_works():
    assert TestClient(_app(_APIKEY)).get("/health/status/stream", headers=_HEADER).status_code == 200


def test_readiness_stream_accepts_the_token_too():
    client = TestClient(_app(_APIKEY))
    token = _stream_url(client.get("/health/dashboard", headers=_HEADER).text).split("token=")[1]
    assert client.get(f"/health/ready/stream?token={token}").status_code == 200


def test_token_does_not_authorize_other_endpoints():
    client = TestClient(_app(_APIKEY))
    token = _stream_url(client.get("/health/dashboard", headers=_HEADER).text).split("token=")[1]
    assert client.get(f"/health/status?token={token}").status_code == 403


def test_api_key_never_appears_in_the_page():
    html = TestClient(_app(_APIKEY)).get("/health/dashboard", headers=_HEADER).text
    assert "s3cret-key" not in html


@pytest.mark.parametrize("token", ["", "abc", "123.", "notdigits.deadbeef", "99999999999.deadbeef"])
def test_malformed_or_forged_tokens_are_rejected(token):
    assert not _valid_stream_token(_stream_token_key(_APIKEY), token)


def test_expired_token_is_rejected(monkeypatch):
    key = _stream_token_key(_APIKEY)
    token = _mint_stream_token(key)
    assert _valid_stream_token(key, token)
    monkeypatch.setattr(time, "time", lambda: int(token.split(".")[0]) + 1)
    assert not _valid_stream_token(key, token)


def test_tokens_are_shared_across_workers_with_the_same_credentials():
    """Separate registries model separate worker processes behind one load balancer."""
    page = TestClient(_app(_APIKEY)).get("/health/dashboard", headers=_HEADER).text
    other_worker = TestClient(_app(dict(_APIKEY)))
    assert other_worker.get(_stream_url(page)).status_code == 200


def test_token_from_different_credentials_is_rejected():
    page = TestClient(_app(_APIKEY)).get("/health/dashboard", headers=_HEADER).text
    other = TestClient(_app({"type": "apikey", "key": "different"}))
    assert other.get(_stream_url(page)).status_code == 403


def test_basic_auth_gets_a_working_token():
    auth = {"type": "basic", "username": "u", "password": "p"}
    client = TestClient(_app(auth))
    creds = {"Authorization": "Basic " + base64.b64encode(b"u:p").decode()}
    url = _stream_url(client.get("/health/dashboard", headers=creds).text)
    assert client.get(url).status_code == 200


def test_callable_auth_gets_a_working_token():
    client = TestClient(_app(lambda request: request.headers.get("x-ok") == "1"))
    url = _stream_url(client.get("/health/dashboard", headers={"x-ok": "1"}).text)
    assert client.get(url).status_code == 200


def test_no_auth_means_no_token():
    html = TestClient(_app(None)).get("/health/dashboard").text
    assert _stream_url(html) == "/health/status/stream"


# ---------------------------------------------------------------------------
# Key derivation: slow to brute-force, computed once, optional explicit secret
# ---------------------------------------------------------------------------

def test_credential_key_is_derived_with_scrypt():
    """A logged token must not let passwords be ground offline at hash speed."""
    expected = hashlib.scrypt(
        b"apikey\x00s3cret-key", salt=b"fastapi-watch/stream-token", n=2**14, r=8, p=1, dklen=32
    )
    assert _stream_token_key(_APIKEY) == expected
    fast = hashlib.sha256(b"fastapi-watch/stream-token\x00s3cret-key").digest()
    assert _stream_token_key(_APIKEY) != fast


def test_basic_and_apikey_material_do_not_collide():
    basic = {"type": "basic", "username": "u", "password": "p"}
    assert _stream_token_key(basic) != _stream_token_key({"type": "apikey", "key": "u:p"})


def test_key_is_derived_once_at_init(monkeypatch):
    app = FastAPI()
    HealthRegistry(app, auth=_APIKEY, poll_interval_ms=None)
    calls = []
    monkeypatch.setattr(registry_module, "_stream_token_key", lambda *a: calls.append(a) or b"x")
    client = TestClient(app)
    for _ in range(3):
        assert client.get("/health/dashboard", headers=_HEADER).status_code == 200
    assert calls == []


def _callable_app(secret) -> FastAPI:
    app = FastAPI()
    HealthRegistry(
        app,
        auth=lambda request: request.headers.get("x-ok") == "1",
        poll_interval_ms=None,
        stream_token_secret=secret,
    )
    return app


def test_stream_token_secret_shares_callable_auth_tokens_across_workers():
    page = TestClient(_callable_app("shared")).get("/health/dashboard", headers={"x-ok": "1"}).text
    assert TestClient(_callable_app(b"shared")).get(_stream_url(page)).status_code == 200
    assert TestClient(_callable_app("other")).get(_stream_url(page)).status_code == 403


def test_callable_auth_without_secret_is_per_process():
    page = TestClient(_callable_app(None)).get("/health/dashboard", headers={"x-ok": "1"}).text
    assert TestClient(_callable_app(None)).get(_stream_url(page)).status_code == 403


def test_stream_token_secret_overrides_credential_derivation():
    app = FastAPI()
    registry = HealthRegistry(app, auth=_APIKEY, stream_token_secret="explicit")
    assert registry._stream_token_key == b"explicit"


@pytest.mark.parametrize("secret", ["", b""])
def test_empty_stream_token_secret_is_rejected(secret):
    with pytest.raises(ValueError, match="stream_token_secret"):
        HealthRegistry(FastAPI(), auth=_APIKEY, stream_token_secret=secret)


# ---------------------------------------------------------------------------
# A token-authorized stream ends when its token expires
# ---------------------------------------------------------------------------

def _capture_stream_expiry(monkeypatch, registry) -> list:
    seen = []

    async def _fake_stream(request, make_report, filter_tags=None, filter_probes=None, expires_at=None):
        seen.append(expires_at)
        yield "data: {}\n\n"

    monkeypatch.setattr(registry, "_event_stream", _fake_stream)
    return seen


@pytest.mark.parametrize("path", ["/health/status/stream", "/health/ready/stream"])
def test_token_stream_carries_the_token_expiry(monkeypatch, path):
    app = FastAPI()
    registry = HealthRegistry(app, auth=_APIKEY, poll_interval_ms=None)
    seen = _capture_stream_expiry(monkeypatch, registry)
    client = TestClient(app)
    token = _stream_url(client.get("/health/dashboard", headers=_HEADER).text).split("token=")[1]

    assert client.get(f"{path}?token={token}").status_code == 200
    assert seen == [int(token.split(".")[0])]


def test_header_authenticated_stream_has_no_expiry(monkeypatch):
    app = FastAPI()
    registry = HealthRegistry(app, auth=_APIKEY, poll_interval_ms=None)
    seen = _capture_stream_expiry(monkeypatch, registry)

    assert TestClient(app).get("/health/status/stream", headers=_HEADER).status_code == 200
    assert seen == [None]


class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_stream_stops_at_token_expiry():
    registry = HealthRegistry(FastAPI(), poll_interval_ms=60_000)
    registry.add(NoOpProbe(name="a"))
    events = []

    async def _consume():
        stream = registry._event_stream(_ConnectedRequest(), str, expires_at=time.time() + 0.3)
        async for event in stream:
            events.append(event)

    await asyncio.wait_for(_consume(), timeout=3)  # would otherwise run for 60 s+
    assert len(events) == 1
    assert registry._active_connections == 0


@pytest.mark.asyncio
async def test_expired_stream_sends_nothing():
    registry = HealthRegistry(FastAPI(), poll_interval_ms=60_000)
    registry.add(NoOpProbe(name="a"))
    events = [e async for e in registry._event_stream(_ConnectedRequest(), str, expires_at=time.time() - 1)]
    assert events == []
