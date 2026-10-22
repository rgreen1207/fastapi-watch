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


def test_non_ascii_token_is_rejected_not_an_error():
    client = TestClient(_app(_APIKEY), raise_server_exceptions=False)
    assert client.get("/health/status/stream?token=1.%C3%A9").status_code == 403


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


@pytest.mark.parametrize("token", ["", "abc", "123.", "notdigits.deadbeef", "99999999999.deadbeef", "1.\u00e9", "\u00b2.ab"])
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
    assert _stream_url(html) == "/health/status/stream?view=dashboard"


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


# ---------------------------------------------------------------------------
# Dashboard hardening: headers, off-loop render, token refresh
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("auth", [None, _APIKEY])
@pytest.mark.parametrize("dashboard", [True, lambda report: "<html>custom</html>"])
def test_dashboard_is_never_cached_sniffed_or_framed(auth, dashboard):
    app = FastAPI()
    HealthRegistry(app, auth=auth, poll_interval_ms=None, dashboard=dashboard)
    resp = TestClient(app).get("/health/dashboard", headers=_HEADER)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]


def test_custom_dashboard_keeps_the_base_policy():
    # A custom page's scripts and assets are unknown: only framing is restricted.
    app = FastAPI()
    HealthRegistry(app, poll_interval_ms=None, dashboard=lambda report: "<html>custom</html>")
    resp = TestClient(app).get("/health/dashboard")
    assert resp.headers["content-security-policy"] == "frame-ancestors 'none'"


@pytest.mark.parametrize("auth", [None, _APIKEY])
def test_builtin_dashboard_runs_only_its_own_script(auth):
    # Defense in depth behind the escaping: a strict CSP whose nonce is fresh
    # per page and carried only by the page's own script, so markup injected
    # by a probe value could not run script, load resources, or send data out.
    app = FastAPI()
    HealthRegistry(app, auth=auth, poll_interval_ms=None).add(NoOpProbe(name="a"))
    client = TestClient(app)
    nonces = []
    for _ in range(2):
        resp = client.get("/health/dashboard", headers=_HEADER)
        csp = dict(
            (part.split(" ", 1) + [""])[:2] for part in (p.strip() for p in resp.headers["content-security-policy"].split(";"))
        )
        assert csp["default-src"] == "'none'"
        assert csp["connect-src"] == "'self'"
        assert csp["base-uri"] == "'none'" and csp["form-action"] == "'none'"
        assert csp["frame-ancestors"] == "'none'"
        nonce = re.fullmatch(r"'nonce-([A-Za-z0-9_-]{16,})'", csp["script-src"]).group(1)
        executable = re.findall(r"<script(?![^>]*application/json)([^>]*)>", resp.text)
        assert executable == [f' nonce="{nonce}"']
        nonces.append(nonce)
    assert nonces[0] != nonces[1]


def test_builtin_dashboard_renders_off_the_event_loop(monkeypatch):
    import threading

    import fastapi_watch.registry as registry_module

    threads = []
    real = registry_module.render_dashboard

    def _recording(*args, **kwargs):
        threads.append(threading.current_thread().name)
        return real(*args, **kwargs)

    monkeypatch.setattr(registry_module, "render_dashboard", _recording)
    resp = TestClient(_app(None)).get("/health/dashboard")
    assert resp.status_code == 200 and "data-stream-url" in resp.text
    assert threads and threads[0].startswith("fastapi-watch")


def test_stream_url_endpoint_returns_a_fresh_working_url():
    client = TestClient(_app(_APIKEY))
    resp = client.get("/health/dashboard/stream-url", headers=_HEADER)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert body["expires_at"] > time.time()
    assert "view=dashboard" in body["stream_url"] and "token=" in body["stream_url"]
    assert client.get(body["stream_url"]).status_code == 200  # no header, as from EventSource


def test_stream_url_endpoint_needs_credentials_not_a_token():
    client = TestClient(_app(_APIKEY))
    token = _stream_url(client.get("/health/dashboard", headers=_HEADER).text).split("token=")[1]
    assert client.get("/health/dashboard/stream-url").status_code == 403
    # A leaked token must not be able to renew itself.
    assert client.get(f"/health/dashboard/stream-url?token={token}").status_code == 403


def test_stream_url_endpoint_absent_without_auth():
    assert TestClient(_app(None)).get("/health/dashboard/stream-url").status_code in (404, 405)


@pytest.mark.parametrize("prefix", ["/health", "/ops/health"])
def test_page_stream_url_maps_to_the_renewal_endpoint(prefix):
    """dashboard.js renewUrl() swaps ``/status/stream?...`` for
    ``/dashboard/stream-url``; that must be the endpoint the server mounts."""
    app = FastAPI()
    HealthRegistry(app, prefix=prefix, auth=_APIKEY, poll_interval_ms=None)
    client = TestClient(app)
    url = _stream_url(client.get(f"{prefix}/dashboard", headers=_HEADER).text)
    renew = re.match(r"^(.*)/status/stream\?", url).group(1) + "/dashboard/stream-url"

    resp = client.get(renew, headers=_HEADER)
    assert resp.status_code == 200
    fresh = resp.json()["stream_url"]
    assert fresh.startswith(f"{prefix}/status/stream?") and "token=" in fresh
    assert client.get(fresh).status_code == 200
