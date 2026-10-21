"""Alerter hardening: URL validation, no redirects, closed responses, isolated dispatch."""
import asyncio
import http.server
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI

from fastapi_watch import HealthRegistry, alerts
from fastapi_watch.alerts import BaseAlerter, WebhookAlerter, _validate_webhook_url
from fastapi_watch.models import AlertRecord, ProbeStatus


def _alert(probe: str = "db") -> AlertRecord:
    return AlertRecord(probe=probe, old_status=ProbeStatus.HEALTHY, new_status=ProbeStatus.UNHEALTHY,
                       timestamp=datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("host", [
    "0.0.0.0", "[::]", "[::ffff:127.0.0.1]", "[::ffff:169.254.169.254]",
    "2130706433", "0x7f000001", "0177.0.0.1", "127.1",
    "localhost.", "LOCALHOST", "api.localhost",
    "224.0.0.1", "100.64.0.1", "169.254.169.254", "10.1.2.3", "[fd00::1]", "[fe80::1]",
])
def test_internal_hosts_rejected_in_any_spelling(host):
    with pytest.raises(ValueError):
        _validate_webhook_url(f"https://{host}/hook")


@pytest.mark.parametrize("url", ["https://hooks.slack.com/services/x", "https://8.8.8.8/hook", "http://example.com/h"])
def test_public_urls_accepted(url):
    _validate_webhook_url(url)


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "https:///nohost"])
def test_bad_schemes_and_missing_hosts_rejected(url):
    with pytest.raises(ValueError):
        _validate_webhook_url(url)


# ---------------------------------------------------------------------------
# Requests: no redirects, responses always closed
# ---------------------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    hits: list = []

    def do_POST(self):
        _Handler.hits.append(self.path)
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/internal")
            self.end_headers()
        else:
            self.send_response(200)
            self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def local_server(monkeypatch):
    # Restore the real opener for this test only (conftest refuses requests by default).
    monkeypatch.setattr(alerts._OPENER, "open", urllib.request.OpenerDirector.open.__get__(alerts._OPENER))
    _Handler.hits = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def test_redirects_are_not_followed(local_server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        alerts._post_json(f"{local_server}/redirect", b"{}", timeout=2)
    assert excinfo.value.code == 302
    assert _Handler.hits == ["/redirect"]  # /internal never contacted


def test_successful_post_reaches_endpoint(local_server):
    alerts._post_json(f"{local_server}/ok", b"{}", timeout=2)
    assert _Handler.hits == ["/ok"]


def test_response_is_closed_on_success():
    response = MagicMock()
    with patch.object(alerts._OPENER, "open", return_value=response):
        alerts._post_json("https://example.com/h", b"{}", timeout=1)
    response.__exit__.assert_called_once()


def test_error_response_is_closed():
    error = urllib.error.HTTPError("https://example.com/h", 500, "boom", {}, MagicMock())
    error.close = MagicMock()
    with patch.object(alerts._OPENER, "open", side_effect=error):
        with pytest.raises(urllib.error.HTTPError):
            alerts._post_json("https://example.com/h", b"{}", timeout=1)
    error.close.assert_called_once()


def test_webhook_alerter_posts_through_the_no_redirect_opener():
    with patch.object(alerts._OPENER, "open", return_value=MagicMock()) as opened:
        asyncio.run(WebhookAlerter(url="https://example.com/hook").notify(_alert()))
    request = opened.call_args[0][0]
    assert request.full_url == "https://example.com/hook"
    assert request.get_method() == "POST"


# ---------------------------------------------------------------------------
# Dispatch: concurrent, isolated, bounded, logged
# ---------------------------------------------------------------------------

class _Recording(BaseAlerter):
    def __init__(self, delay: float = 0.0, fail: bool = False):
        self.delay, self.fail, self.received = delay, fail, []

    async def notify(self, alert):
        await asyncio.sleep(self.delay)
        if self.fail:
            raise ConnectionError("integration down")
        self.received.append(alert.probe)


@pytest.mark.asyncio
async def test_slow_alerter_does_not_delay_the_others():
    slow, fast = _Recording(delay=0.3), _Recording()
    registry = HealthRegistry(FastAPI(), alerters=[slow, fast], serve_routes=False)
    task = asyncio.create_task(registry._dispatch_alert(_alert()))
    await asyncio.sleep(0.05)
    assert fast.received == ["db"] and slow.received == []
    await task
    assert slow.received == ["db"]


@pytest.mark.asyncio
async def test_failing_alerter_is_logged_and_isolated(caplog):
    broken, healthy = _Recording(fail=True), _Recording()
    registry = HealthRegistry(FastAPI(), alerters=[broken, healthy], serve_routes=False)
    with caplog.at_level("WARNING", logger="fastapi_watch"):
        await registry._dispatch_alert(_alert())
    assert healthy.received == ["db"]
    [record] = [r for r in caplog.records if "failed for probe" in r.getMessage()]
    assert record.exc_info and isinstance(record.exc_info[1], ConnectionError)


@pytest.mark.asyncio
async def test_hung_alerter_is_bounded(monkeypatch, caplog):
    monkeypatch.setattr(alerts, "_NOTIFY_TIMEOUT_S", 0.05)
    hung, healthy = _Recording(delay=3600), _Recording()
    registry = HealthRegistry(FastAPI(), alerters=[hung, healthy], serve_routes=False)
    with caplog.at_level("WARNING", logger="fastapi_watch"):
        await asyncio.wait_for(registry._dispatch_alert(_alert()), 2)
    assert healthy.received == ["db"]
    assert any("timed out" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_alert_loop_survives_and_drains_on_dispatch_error(caplog):
    registry = HealthRegistry(FastAPI(), alerters=[_Recording()], serve_routes=False)

    async def explode(alert):
        raise RuntimeError("dispatch bug")

    registry._dispatch_alert = explode
    registry._ensure_alert_worker()
    registry._alert_queue.put_nowait(_alert())
    with caplog.at_level("ERROR", logger="fastapi_watch"):
        await asyncio.wait_for(registry._alert_queue.join(), 2)  # task_done reached despite the error
    assert not registry._alert_worker.done()
    assert any("Dispatching alert" in r.getMessage() for r in caplog.records)
    await registry._shutdown()
