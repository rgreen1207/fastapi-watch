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


def _make_mock_session(status: int, body: bytes = b"OK"):
    mock_response = AsyncMock()
    mock_response.status = status
    mock_response.headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
    mock_response.read = AsyncMock(return_value=body)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    for method in ("get", "post", "put", "patch", "delete", "head", "options"):
        setattr(mock_session, method, MagicMock(return_value=mock_response))
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
    assert result.details["method"] == "GET"


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


@pytest.mark.asyncio
async def test_http_probe_post():
    from fastapi_watch.probes.http import HttpProbe
    mock_session = _make_mock_session(201)
    with patch("aiohttp.ClientSession", return_value=mock_session):
        probe = HttpProbe(
            url="http://example.com/items",
            method="POST",
            json={"name": "test"},
            expected_status=201,
            name="items-write",
        )
        result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.details["method"] == "POST"
    mock_session.post.assert_called_once_with("http://example.com/items", json={"name": "test"})


@pytest.mark.asyncio
async def test_http_probe_put():
    from fastapi_watch.probes.http import HttpProbe
    mock_session = _make_mock_session(200)
    with patch("aiohttp.ClientSession", return_value=mock_session):
        probe = HttpProbe(
            url="http://example.com/items/1",
            method="PUT",
            json={"name": "updated"},
            name="items-put",
        )
        result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.details["method"] == "PUT"
    mock_session.put.assert_called_once_with("http://example.com/items/1", json={"name": "updated"})


@pytest.mark.asyncio
async def test_http_probe_patch():
    from fastapi_watch.probes.http import HttpProbe
    mock_session = _make_mock_session(200)
    with patch("aiohttp.ClientSession", return_value=mock_session):
        probe = HttpProbe(
            url="http://example.com/items/1",
            method="PATCH",
            json={"name": "patched"},
            name="items-patch",
        )
        result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.details["method"] == "PATCH"


@pytest.mark.asyncio
async def test_http_probe_delete():
    from fastapi_watch.probes.http import HttpProbe
    mock_session = _make_mock_session(204)
    with patch("aiohttp.ClientSession", return_value=mock_session):
        probe = HttpProbe(
            url="http://example.com/items/1",
            method="DELETE",
            expected_status=204,
            name="items-delete",
        )
        result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.details["method"] == "DELETE"
    mock_session.delete.assert_called_once_with("http://example.com/items/1")


@pytest.mark.asyncio
async def test_http_probe_invalid_method():
    from fastapi_watch.probes.http import HttpProbe
    with pytest.raises(ValueError, match="Unsupported method"):
        HttpProbe(url="http://example.com/health", method="BREW")


@pytest.mark.asyncio
async def test_http_probe_method_case_insensitive():
    from fastapi_watch.probes.http import HttpProbe
    probe = HttpProbe(url="http://example.com/health", method="post")
    assert probe.method == "POST"


@pytest.mark.asyncio
async def test_http_probe_custom_headers():
    from fastapi_watch.probes.http import HttpProbe
    mock_session = _make_mock_session(200)
    with patch("aiohttp.ClientSession", return_value=mock_session) as mock_cls:
        probe = HttpProbe(
            url="http://example.com/health",
            headers={"Authorization": "Bearer token"},
        )
        await probe.check()
    mock_cls.assert_called_once()
    _, kwargs = mock_cls.call_args
    assert kwargs["headers"] == {"Authorization": "Bearer token"}
