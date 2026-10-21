"""The authenticated dashboard hands its SSE stream a short-lived signed token.

A browser EventSource cannot send headers, so with header-based auth the
dashboard's live updates were always rejected.
"""
import base64
import re
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi_watch import HealthRegistry
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
    assert resp.text.startswith("data: ")


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
