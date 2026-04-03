"""Tests for MemcachedProbe — mocked so no real Memcached needed."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi_watch.models import ProbeStatus

try:
    import aiomcache
    HAS_AIOMCACHE = True
except ImportError:
    HAS_AIOMCACHE = False

pytestmark = pytest.mark.skipif(not HAS_AIOMCACHE, reason="aiomcache not installed")


def _make_mock_client(stats_exc=None):
    client = AsyncMock()
    if stats_exc:
        client.stats = AsyncMock(side_effect=stats_exc)
    else:
        client.stats = AsyncMock(return_value={b"version": b"1.6.0"})
    client.close = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_memcached_probe_healthy():
    from fastapi_watch.probes.memcached import MemcachedProbe
    mock_client = _make_mock_client()
    with patch("aiomcache.Client", return_value=mock_client):
        probe = MemcachedProbe(host="localhost", port=11211)
        result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.name == "memcached"
    mock_client.close.assert_called_once()


@pytest.mark.asyncio
async def test_memcached_probe_unhealthy_on_error():
    from fastapi_watch.probes.memcached import MemcachedProbe
    mock_client = _make_mock_client(stats_exc=Exception("connection refused"))
    with patch("aiomcache.Client", return_value=mock_client):
        probe = MemcachedProbe(host="localhost")
        result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY
    assert result.error == "probe check failed"
    mock_client.close.assert_called_once()


@pytest.mark.asyncio
async def test_memcached_probe_custom_name():
    from fastapi_watch.probes.memcached import MemcachedProbe
    mock_client = _make_mock_client()
    with patch("aiomcache.Client", return_value=mock_client):
        probe = MemcachedProbe(name="session-cache")
        result = await probe.check()
    assert result.name == "session-cache"


@pytest.mark.asyncio
async def test_memcached_probe_default_port():
    from fastapi_watch.probes.memcached import MemcachedProbe
    probe = MemcachedProbe()
    assert probe.port == 11211
