import json
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from fastapi_watch.alerts import OpsGenieAlerter
from fastapi_watch.models import AlertRecord, ProbeStatus


def _alert(old: ProbeStatus, new: ProbeStatus, probe: str = "redis") -> AlertRecord:
    return AlertRecord(
        probe=probe,
        old_status=old,
        new_status=new,
        timestamp=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_creates_alert_on_unhealthy():
    with patch("fastapi_watch.alerts.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = MagicMock()
        alerter = OpsGenieAlerter(api_key="test-key")
        await alerter.notify(_alert(ProbeStatus.HEALTHY, ProbeStatus.UNHEALTHY))
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://api.opsgenie.com/v2/alerts"
        body = json.loads(req.data)
        assert body["priority"] == "P1"
        assert body["alias"] == "fastapi-watch:redis"
        assert body["details"]["probe"] == "redis"
        assert body["details"]["new_status"] == "unhealthy"


@pytest.mark.asyncio
async def test_creates_alert_on_degraded():
    with patch("fastapi_watch.alerts.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = MagicMock()
        alerter = OpsGenieAlerter(api_key="test-key")
        await alerter.notify(_alert(ProbeStatus.HEALTHY, ProbeStatus.DEGRADED))
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert body["priority"] == "P3"
        assert body["alias"] == "fastapi-watch:redis"


@pytest.mark.asyncio
async def test_closes_alert_on_healthy():
    with patch("fastapi_watch.alerts.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = MagicMock()
        alerter = OpsGenieAlerter(api_key="test-key")
        await alerter.notify(_alert(ProbeStatus.UNHEALTHY, ProbeStatus.HEALTHY))
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert "fastapi-watch%3Aredis/close" in req.full_url
        assert "identifierType=alias" in req.full_url


@pytest.mark.asyncio
async def test_eu_region_uses_correct_endpoint():
    with patch("fastapi_watch.alerts.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = MagicMock()
        alerter = OpsGenieAlerter(api_key="test-key", region="eu")
        await alerter.notify(_alert(ProbeStatus.HEALTHY, ProbeStatus.UNHEALTHY))
        req = mock_urlopen.call_args[0][0]
        assert req.full_url.startswith("https://api.eu.opsgenie.com")


@pytest.mark.asyncio
async def test_us_region_uses_correct_endpoint():
    with patch("fastapi_watch.alerts.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = MagicMock()
        alerter = OpsGenieAlerter(api_key="test-key", region="us")
        await alerter.notify(_alert(ProbeStatus.HEALTHY, ProbeStatus.UNHEALTHY))
        req = mock_urlopen.call_args[0][0]
        assert req.full_url.startswith("https://api.opsgenie.com")


def test_invalid_region_raises():
    with pytest.raises(ValueError, match="region must be 'us' or 'eu'"):
        OpsGenieAlerter(api_key="test-key", region="ap")


def test_repr_does_not_leak_api_key():
    alerter = OpsGenieAlerter(api_key="super-secret-key-12345")
    assert "super-secret-key-12345" not in repr(alerter)


@pytest.mark.asyncio
async def test_auth_header_uses_genie_key():
    with patch("fastapi_watch.alerts.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = MagicMock()
        alerter = OpsGenieAlerter(api_key="my-key")
        await alerter.notify(_alert(ProbeStatus.HEALTHY, ProbeStatus.UNHEALTHY))
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") == "GenieKey my-key"


@pytest.mark.asyncio
async def test_close_sends_empty_json_body():
    with patch("fastapi_watch.alerts.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = MagicMock()
        alerter = OpsGenieAlerter(api_key="test-key")
        await alerter.notify(_alert(ProbeStatus.UNHEALTHY, ProbeStatus.HEALTHY))
        req = mock_urlopen.call_args[0][0]
        assert json.loads(req.data) == {}


@pytest.mark.asyncio
async def test_alias_includes_probe_name():
    with patch("fastapi_watch.alerts.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = MagicMock()
        alerter = OpsGenieAlerter(api_key="test-key")
        await alerter.notify(_alert(ProbeStatus.HEALTHY, ProbeStatus.UNHEALTHY, probe="postgresql"))
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert body["alias"] == "fastapi-watch:postgresql"


@pytest.mark.asyncio
async def test_custom_source_label():
    with patch("fastapi_watch.alerts.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = MagicMock()
        alerter = OpsGenieAlerter(api_key="test-key", source="my-app/db")
        await alerter.notify(_alert(ProbeStatus.HEALTHY, ProbeStatus.UNHEALTHY))
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert body["source"] == "my-app/db"


@pytest.mark.asyncio
async def test_degraded_to_healthy_closes_alert():
    """DEGRADED→HEALTHY also triggers the close path, not a new create."""
    with patch("fastapi_watch.alerts.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = MagicMock()
        alerter = OpsGenieAlerter(api_key="test-key")
        await alerter.notify(_alert(ProbeStatus.DEGRADED, ProbeStatus.HEALTHY))
        req = mock_urlopen.call_args[0][0]
        assert "/close" in req.full_url
        assert "identifierType=alias" in req.full_url


@pytest.mark.asyncio
async def test_http_error_on_create_raises_runtime_error():
    """A 4xx/5xx from OpsGenie on create surfaces as RuntimeError (not silent)."""
    import urllib.error
    with patch("fastapi_watch.alerts.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://api.opsgenie.com/v2/alerts",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )
        alerter = OpsGenieAlerter(api_key="bad-key")
        with pytest.raises(RuntimeError, match="HTTP 401"):
            await alerter.notify(_alert(ProbeStatus.HEALTHY, ProbeStatus.UNHEALTHY))


@pytest.mark.asyncio
async def test_http_error_on_close_raises_runtime_error():
    """A 4xx/5xx from OpsGenie on close surfaces as RuntimeError (not silent)."""
    import urllib.error
    with patch("fastapi_watch.alerts.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://api.opsgenie.com/v2/alerts/fastapi-watch%3Aredis/close",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=None,
        )
        alerter = OpsGenieAlerter(api_key="test-key")
        with pytest.raises(RuntimeError, match="HTTP 404"):
            await alerter.notify(_alert(ProbeStatus.UNHEALTHY, ProbeStatus.HEALTHY))


def test_assert_safe_url_rejects_non_opsgenie_host():
    """_assert_safe_url must reject any host outside the OpsGenie allowlist."""
    alerter = OpsGenieAlerter(api_key="test-key")
    with pytest.raises(ValueError, match="not in allowed hosts"):
        alerter._assert_safe_url("https://evil.example.com/v2/alerts")


def test_assert_safe_url_accepts_us_host():
    alerter = OpsGenieAlerter(api_key="test-key", region="us")
    alerter._assert_safe_url("https://api.opsgenie.com/v2/alerts")  # must not raise


def test_assert_safe_url_accepts_eu_host():
    alerter = OpsGenieAlerter(api_key="test-key", region="eu")
    alerter._assert_safe_url("https://api.eu.opsgenie.com/v2/alerts")  # must not raise
