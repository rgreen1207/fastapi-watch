"""Security hardening tests."""
import logging
import pytest
from pathlib import Path
from unittest.mock import MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi_watch import HealthRegistry
from fastapi_watch.alerts import (
    WebhookAlerter,
    SlackAlerter,
    TeamsAlerter,
    PagerDutyAlerter,
)
from fastapi_watch.alerts import _validate_webhook_url
from fastapi_watch.models import ProbeStatus
from fastapi_watch.probes import NoOpProbe


# ---------------------------------------------------------------------------
# _validate_webhook_url
# ---------------------------------------------------------------------------

def test_validate_accepts_https():
    _validate_webhook_url("https://hooks.slack.com/services/abc")


def test_validate_accepts_http():
    _validate_webhook_url("http://internal-relay.corp.example.com/hook")


def test_validate_rejects_file_scheme():
    with pytest.raises(ValueError, match="scheme"):
        _validate_webhook_url("file:///etc/passwd")


def test_validate_rejects_localhost():
    with pytest.raises(ValueError, match="localhost"):
        _validate_webhook_url("https://localhost/hook")


def test_validate_rejects_loopback_ip():
    with pytest.raises(ValueError, match="private"):
        _validate_webhook_url("https://127.0.0.1/hook")


def test_validate_rejects_private_10_net():
    with pytest.raises(ValueError, match="private"):
        _validate_webhook_url("https://10.0.0.1/hook")


def test_validate_rejects_private_172_net():
    with pytest.raises(ValueError, match="private"):
        _validate_webhook_url("https://172.16.5.1/hook")


def test_validate_rejects_private_192_168_net():
    with pytest.raises(ValueError, match="private"):
        _validate_webhook_url("https://192.168.1.1/hook")


def test_validate_rejects_link_local():
    with pytest.raises(ValueError, match="private"):
        _validate_webhook_url("https://169.254.169.254/latest/meta-data/")


# ---------------------------------------------------------------------------
# Alerter __init__ calls _validate_webhook_url
# ---------------------------------------------------------------------------

def test_webhook_alerter_rejects_private_url():
    with pytest.raises(ValueError):
        WebhookAlerter(url="https://192.168.0.10/hook")


def test_slack_alerter_rejects_private_url():
    with pytest.raises(ValueError):
        SlackAlerter(webhook_url="https://10.0.0.5/slack")


def test_teams_alerter_rejects_private_url():
    with pytest.raises(ValueError):
        TeamsAlerter(webhook_url="https://127.0.0.1/teams")


def test_webhook_alerter_accepts_public_https():
    a = WebhookAlerter(url="https://hooks.example.com/health")
    assert a.url == "https://hooks.example.com/health"


# ---------------------------------------------------------------------------
# __repr__ redaction
# ---------------------------------------------------------------------------

def test_webhook_alerter_repr_does_not_expose_headers():
    a = WebhookAlerter(
        url="https://hooks.example.com/health",
        headers={"Authorization": "Bearer super-secret-token"},
    )
    r = repr(a)
    assert "super-secret-token" not in r
    assert "Bearer" not in r


def test_slack_alerter_repr_redacts_url():
    a = SlackAlerter(webhook_url="https://hooks.slack.com/services/SECRET")
    r = repr(a)
    assert "SECRET" not in r
    assert "<redacted>" in r


def test_teams_alerter_repr_redacts_url():
    a = TeamsAlerter(webhook_url="https://outlook.office.com/webhook/SECRET")
    r = repr(a)
    assert "SECRET" not in r
    assert "<redacted>" in r


def test_pagerduty_alerter_repr_redacts_routing_key():
    a = PagerDutyAlerter(routing_key="supersecretroutingkey1234567890ab")
    r = repr(a)
    assert "supersecretroutingkey" not in r
    assert "<redacted>" in r


# ---------------------------------------------------------------------------
# Probe exception error is generic — no internal details exposed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_probe_exception_error_is_generic():
    from fastapi_watch.probes import NoOpProbe

    class LeakyProbe(NoOpProbe):
        name = "leaky"

        async def check(self):
            raise RuntimeError("db_password=hunter2 connection refused")

    app = FastAPI()
    registry = HealthRegistry(app)
    registry.add(LeakyProbe(name="leaky"))
    results = await registry.run_all()
    assert results[0].status == ProbeStatus.UNHEALTHY
    assert "hunter2" not in results[0].error
    assert results[0].error == "probe check failed"


@pytest.mark.asyncio
async def test_probe_exception_logged_with_details():
    class LeakyProbe(NoOpProbe):
        name = "leaky"

        async def check(self):
            raise RuntimeError("db_password=hunter2")

    mock_logger = MagicMock(spec=logging.Logger)
    app = FastAPI()
    registry = HealthRegistry(app, logger=mock_logger)
    registry.add(LeakyProbe(name="leaky"))
    await registry.run_all()
    # Full details go to the logger, not the HTTP response
    mock_logger.exception.assert_called_once()
    log_args = str(mock_logger.exception.call_args)
    assert "leaky" in log_args


# ---------------------------------------------------------------------------
# Custom auth callable — strict True check
# ---------------------------------------------------------------------------

def test_custom_auth_none_return_is_denied():
    """An auth callable returning None must deny access (not grant it)."""
    app = FastAPI()
    registry = HealthRegistry(app, auth=lambda request: None, poll_interval_ms=None)
    registry.add(NoOpProbe(name="mem"))
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/health/status")
    assert resp.status_code == 403


def test_custom_auth_empty_string_return_is_denied():
    app = FastAPI()
    registry = HealthRegistry(app, auth=lambda request: "", poll_interval_ms=None)
    registry.add(NoOpProbe(name="mem"))
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/health/status")
    assert resp.status_code == 403


def test_custom_auth_zero_return_is_denied():
    app = FastAPI()
    registry = HealthRegistry(app, auth=lambda request: 0, poll_interval_ms=None)
    registry.add(NoOpProbe(name="mem"))
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/health/status")
    assert resp.status_code == 403


def test_custom_auth_true_return_is_allowed():
    app = FastAPI()
    registry = HealthRegistry(app, auth=lambda request: True, poll_interval_ms=None)
    registry.add(NoOpProbe(name="mem"))
    client = TestClient(app)
    resp = client.get("/health/status")
    assert resp.status_code == 200


def test_custom_auth_false_return_is_denied():
    app = FastAPI()
    registry = HealthRegistry(app, auth=lambda request: False, poll_interval_ms=None)
    registry.add(NoOpProbe(name="mem"))
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/health/status")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# .py dashboard file is rejected
# ---------------------------------------------------------------------------

def test_py_dashboard_file_raises_value_error(tmp_path):
    py_file = tmp_path / "dashboard.py"
    py_file.write_text("def render_dashboard(*a, **kw): return '<html/>'")
    app = FastAPI()
    with pytest.raises(ValueError, match="callable"):
        HealthRegistry(app, dashboard=py_file)


def test_html_dashboard_file_accepted(tmp_path):
    html_file = tmp_path / "dashboard.html"
    html_file.write_text("<!DOCTYPE html><html><body>custom</body></html>")
    app = FastAPI()
    registry = HealthRegistry(app, dashboard=html_file, poll_interval_ms=None)
    client = TestClient(app)
    resp = client.get("/health/dashboard")
    assert resp.status_code == 200
    assert "custom" in resp.text


# ---------------------------------------------------------------------------
# Auth-None warning logged
# ---------------------------------------------------------------------------

def test_no_auth_warning_logged():
    mock_logger = MagicMock(spec=logging.Logger)
    app = FastAPI()
    HealthRegistry(app, auth=None, logger=mock_logger, poll_interval_ms=None)
    warning_messages = " ".join(
        str(call) for call in mock_logger.warning.call_args_list
    )
    assert "auth=None" in warning_messages or "publicly accessible" in warning_messages


def test_no_auth_warning_suppressed_when_auth_set():
    mock_logger = MagicMock(spec=logging.Logger)
    app = FastAPI()
    HealthRegistry(app, auth=lambda r: True, logger=mock_logger, poll_interval_ms=None)
    # No auth=None warning should be present
    warning_messages = " ".join(
        str(call) for call in mock_logger.warning.call_args_list
    )
    assert "publicly accessible" not in warning_messages


# ---------------------------------------------------------------------------
# Probe name validation at registry.add() time
# ---------------------------------------------------------------------------

def test_valid_probe_names_accepted():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    for name in ("mem", "my-probe", "probe_1", "redis.primary", "DB", "tcp:localhost:6379"):
        registry.add(NoOpProbe(name=name))


def test_probe_name_with_newline_rejected():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    with pytest.raises(ValueError, match="invalid characters"):
        registry.add(NoOpProbe(name="probe\ninjected"))


def test_probe_name_with_quote_rejected():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    with pytest.raises(ValueError, match="invalid characters"):
        registry.add(NoOpProbe(name='probe"name'))


def test_probe_name_with_space_rejected():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    with pytest.raises(ValueError, match="invalid characters"):
        registry.add(NoOpProbe(name="my probe"))


# ---------------------------------------------------------------------------
# Probe error string is generic (probe-level catches, not just registry-level)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tcp_probe_error_is_generic():
    from fastapi_watch.probes.tcp import TCPProbe
    probe = TCPProbe(host="192.0.2.1", port=9999, timeout=0.01)
    result = await probe.check()
    from fastapi_watch.models import ProbeStatus
    assert result.status == ProbeStatus.UNHEALTHY
    assert result.error == "probe check failed"


# ---------------------------------------------------------------------------
# Prometheus label escaping
# ---------------------------------------------------------------------------

def test_prometheus_label_escapes_quotes():
    from fastapi_watch.prometheus import render_prometheus
    from fastapi_watch.models import ProbeResult, ProbeStatus
    result = ProbeResult(name='probe"name', status=ProbeStatus.HEALTHY, latency_ms=0.0)
    output = render_prometheus([result])
    assert '"probe"name"' not in output
    assert r'probe\"name' in output


def test_prometheus_label_escapes_newlines():
    from fastapi_watch.prometheus import render_prometheus
    from fastapi_watch.models import ProbeResult, ProbeStatus
    result = ProbeResult(name="probe\ninjected", status=ProbeStatus.HEALTHY, latency_ms=0.0)
    output = render_prometheus([result])
    # Literal newline inside a label value would break the line-based format;
    # it must be escaped to the two-character sequence \n instead.
    assert 'name="probe\ninjected"' not in output  # literal newline must not appear
    assert r"probe\ninjected" in output             # escaped form must appear


def test_prometheus_label_escapes_backslashes():
    from fastapi_watch.prometheus import render_prometheus
    from fastapi_watch.models import ProbeResult, ProbeStatus
    result = ProbeResult(name="probe\\name", status=ProbeStatus.HEALTHY, latency_ms=0.0)
    output = render_prometheus([result])
    assert r"probe\\name" in output


# ---------------------------------------------------------------------------
# Maintenance endpoint: overflow/invalid minutes rejected at validation layer
# ---------------------------------------------------------------------------

def test_maintenance_huge_minutes_returns_422():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(NoOpProbe(name="mem"))
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/health/maintenance", json={"minutes": 1e308})
    assert resp.status_code == 422


def test_maintenance_negative_minutes_returns_422():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(NoOpProbe(name="mem"))
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/health/maintenance", json={"minutes": -1})
    assert resp.status_code == 422


def test_maintenance_valid_minutes_accepted():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(NoOpProbe(name="mem"))
    client = TestClient(app)
    resp = client.post("/health/maintenance", json={"minutes": 30})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Tag filter: bounded to 50 entries
# ---------------------------------------------------------------------------

def test_tag_filter_truncates_at_50():
    from fastapi_watch.registry import _parse_tag_filter
    tags = ",".join(f"tag{i}" for i in range(200))
    result = _parse_tag_filter(tags)
    assert len(result) <= 50


def test_tag_filter_normal_use_unaffected():
    from fastapi_watch.registry import _parse_tag_filter
    result = _parse_tag_filter("api,db,cache")
    assert result == frozenset({"api", "db", "cache"})
