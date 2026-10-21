"""POST/DELETE /health/maintenance refuse cross-site browser requests.

Browsers cache Basic credentials and attach them to any request to the site,
so an empty-body ``<form method=post>`` on another origin used to switch on
indefinite maintenance (pinning /ready at 200 and silencing alerts).
"""
import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi_watch import HealthRegistry
from fastapi_watch.registry import _origin_matches

_BASIC = {"type": "basic", "username": "ops", "password": "pw"}
_CREDS = {"Authorization": "Basic " + base64.b64encode(b"ops:pw").decode()}
_EVIL = "https://evil.example"


def _setup():
    app = FastAPI()
    registry = HealthRegistry(app, auth=_BASIC, poll_interval_ms=None)
    return registry, TestClient(app)


def test_cross_site_form_post_with_cached_credentials_is_refused():
    registry, client = _setup()
    resp = client.post(
        "/health/maintenance",
        headers={
            **_CREDS,
            "Origin": _EVIL,
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "navigate",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        content=b"",
    )
    assert resp.status_code == 403
    assert registry._in_maintenance() is False


def test_cross_site_form_post_with_fields_is_refused():
    registry, client = _setup()
    resp = client.post(
        "/health/maintenance",
        headers={**_CREDS, "Origin": _EVIL, "Content-Type": "application/x-www-form-urlencoded"},
        content=b"minutes=5",
    )
    assert resp.status_code == 403
    assert registry._in_maintenance() is False


def test_no_cors_fetch_is_refused():
    """fetch(url, {method: "POST", mode: "no-cors", credentials: "include", body: "{...}"})."""
    registry, client = _setup()
    resp = client.post(
        "/health/maintenance",
        headers={
            **_CREDS,
            "Origin": _EVIL,
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "no-cors",
            "Content-Type": "text/plain;charset=UTF-8",
        },
        content=b'{"minutes": 30}',
    )
    assert resp.status_code == 403
    assert registry._in_maintenance() is False


def test_cross_site_delete_is_refused():
    registry, client = _setup()
    registry.set_maintenance()
    resp = client.delete("/health/maintenance", headers={**_CREDS, "Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 403
    assert registry._in_maintenance() is True


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "cross-site"},  # no Origin
    {"Origin": _EVIL},  # browsers without Fetch Metadata
    {"Origin": "null"},  # sandboxed iframe, data: URL
    {"Origin": "http://testserver.evil.example"},
    {"Origin": "http://testserver:8080"},  # same host, different port
    {"Origin": "not a url"},
])
def test_cross_origin_signals_are_refused(headers):
    registry, client = _setup()
    resp = client.post("/health/maintenance", headers={**_CREDS, **headers})
    assert resp.status_code == 403
    assert registry._in_maintenance() is False


def test_cross_site_check_runs_before_auth():
    _, client = _setup()
    assert client.post("/health/maintenance", headers={"Origin": _EVIL}).status_code == 403


def test_same_origin_json_request_is_allowed():
    registry, client = _setup()
    resp = client.post(
        "/health/maintenance",
        headers={**_CREDS, "Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
        json={"minutes": 5},
    )
    assert resp.status_code == 200
    assert resp.json()["until"] is not None
    assert registry._in_maintenance() is True
    resp = client.delete(
        "/health/maintenance",
        headers={**_CREDS, "Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    )
    assert resp.status_code == 200
    assert registry._in_maintenance() is False


def test_origin_matching_x_forwarded_host_is_allowed():
    registry, client = _setup()
    resp = client.post(
        "/health/maintenance",
        headers={**_CREDS, "Origin": "https://ops.example", "X-Forwarded-Host": "ops.example"},
        json={},
    )
    assert resp.status_code == 200


def test_curl_style_empty_post_is_allowed():
    """curl -X POST -H 'Authorization: ...' — no Origin, no Content-Type, no body."""
    registry, client = _setup()
    resp = client.post("/health/maintenance", headers=_CREDS)
    assert resp.status_code == 200
    assert resp.json() == {"active": True, "until": None}
    assert client.delete("/health/maintenance", headers=_CREDS).status_code == 200
    assert registry._in_maintenance() is False


def test_curl_style_json_post_is_allowed():
    registry, client = _setup()
    resp = client.post("/health/maintenance", headers=_CREDS, json={"minutes": 30})
    assert resp.status_code == 200
    assert registry._in_maintenance() is True


@pytest.mark.parametrize("content_type", [
    "text/plain",
    "application/x-www-form-urlencoded",
    "multipart/form-data; boundary=x",
    None,
])
def test_non_json_body_is_rejected_with_415(content_type):
    registry, client = _setup()
    headers = dict(_CREDS)
    if content_type is not None:
        headers["Content-Type"] = content_type
    resp = client.post("/health/maintenance", headers=headers, content=b'{"minutes": 30}')
    assert resp.status_code == 415
    assert registry._in_maintenance() is False


def test_json_with_charset_parameter_is_accepted():
    _, client = _setup()
    resp = client.post(
        "/health/maintenance",
        headers={**_CREDS, "Content-Type": "application/json; charset=utf-8"},
        content=b'{"minutes": 30}',
    )
    assert resp.status_code == 200


@pytest.mark.parametrize("origin, hosts, expected", [
    ("https://example.com", ["example.com"], True),
    ("https://example.com", ["example.com:443"], True),
    ("http://example.com:8000", ["example.com:8000"], True),
    ("http://EXAMPLE.com", ["example.COM"], True),
    ("http://[::1]:8000", ["[::1]:8000"], True),
    ("https://example.com", ["example.com:80"], False),
    ("https://example.com", ["other.com"], False),
    ("null", ["example.com"], False),
    ("ftp://example.com", ["example.com"], False),
    ("https://example.com", [""], False),
])
def test_origin_matches(origin, hosts, expected):
    assert _origin_matches(origin, hosts) is expected
