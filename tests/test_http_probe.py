"""Tests for HttpProbe using aiohttp mock."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi_watch.models import ProbeStatus

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

pytestmark = pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")


def _make_mock_session(status: int):
    mock_response = AsyncMock()
    mock_response.status = status
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.get = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


@pytest.mark.asyncio
async def test_http_probe_healthy_response():
    from fastapi_watch.probes.http import HttpProbe
    with patch("aiohttp.ClientSession", return_value=_make_mock_session(200)):
        probe = HttpProbe(url="http://example.com/health", name="upstream")
        result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.name == "upstream"


@pytest.mark.asyncio
async def test_http_probe_unhealthy_on_bad_status():
    from fastapi_watch.probes.http import HttpProbe
    with patch("aiohttp.ClientSession", return_value=_make_mock_session(503)):
        probe = HttpProbe(url="http://example.com/health", name="upstream")
        result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY
    assert "503" in result.error


@pytest.mark.asyncio
async def test_http_probe_unhealthy_on_exception():
    from fastapi_watch.probes.http import HttpProbe

    mock_session = AsyncMock()
    mock_session.get = MagicMock(side_effect=Exception("connection refused"))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        probe = HttpProbe(url="http://example.com/health", name="upstream")
        result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY
    assert "connection refused" in result.error


@pytest.mark.asyncio
async def test_http_probe_name_defaults_to_host():
    from fastapi_watch.probes.http import HttpProbe
    probe = HttpProbe(url="http://api.example.com/health")
    assert probe.name == "api.example.com"


@pytest.mark.asyncio
async def test_http_probe_custom_expected_status():
    from fastapi_watch.probes.http import HttpProbe
    with patch("aiohttp.ClientSession", return_value=_make_mock_session(204)):
        probe = HttpProbe(url="http://example.com/health", expected_status=204)
        result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
