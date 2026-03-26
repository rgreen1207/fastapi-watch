"""Tests for RedisProbe — mocked so no real Redis needed."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi_watch.models import ProbeStatus

try:
    import redis.asyncio
    HAS_REDIS = True
except ImportError:
    HAS_REDIS = False

pytestmark = pytest.mark.skipif(not HAS_REDIS, reason="redis not installed")


def _make_mock_redis(ping_exc=None, info=None, dbsize=0):
    r = AsyncMock()
    if ping_exc:
        r.ping = AsyncMock(side_effect=ping_exc)
    else:
        r.ping = AsyncMock(return_value=True)
    r.info = AsyncMock(return_value=info or {
        "redis_version": "7.0.0",
        "uptime_in_seconds": 3600,
        "used_memory_human": "1.00M",
        "connected_clients": 5,
        "role": "master",
    })
    r.dbsize = AsyncMock(return_value=dbsize)
    r.scan_iter = MagicMock(return_value=_async_iter([]))
    r.aclose = AsyncMock()
    return r


async def _async_iter(items):
    for item in items:
        yield item


@pytest.mark.asyncio
async def test_redis_probe_healthy():
    from fastapi_watch.probes.redis import RedisProbe
    mock_redis = _make_mock_redis()
    with patch("redis.asyncio.from_url", return_value=mock_redis):
        probe = RedisProbe()
        result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.name == "redis"
    assert result.latency_ms >= 0
    mock_redis.ping.assert_called_once()
    mock_redis.aclose.assert_called_once()


@pytest.mark.asyncio
async def test_redis_probe_details_populated():
    from fastapi_watch.probes.redis import RedisProbe
    mock_redis = _make_mock_redis(dbsize=42)
    with patch("redis.asyncio.from_url", return_value=mock_redis):
        probe = RedisProbe()
        result = await probe.check()
    assert result.details["version"] == "7.0.0"
    assert result.details["uptime_seconds"] == 3600
    assert result.details["connected_clients"] == 5
    assert result.details["role"] == "master"
    assert result.details["total_keys"] == 42


@pytest.mark.asyncio
async def test_redis_probe_unhealthy_on_connect_error():
    from fastapi_watch.probes.redis import RedisProbe
    mock_redis = _make_mock_redis(ping_exc=Exception("connection refused"))
    with patch("redis.asyncio.from_url", return_value=mock_redis):
        probe = RedisProbe()
        result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY
    assert "connection refused" in result.error
    mock_redis.aclose.assert_called_once()


@pytest.mark.asyncio
async def test_redis_probe_custom_name():
    from fastapi_watch.probes.redis import RedisProbe
    mock_redis = _make_mock_redis()
    with patch("redis.asyncio.from_url", return_value=mock_redis):
        probe = RedisProbe(name="session-cache")
        result = await probe.check()
    assert result.name == "session-cache"


@pytest.mark.asyncio
async def test_redis_probe_custom_url():
    from fastapi_watch.probes.redis import RedisProbe
    mock_redis = _make_mock_redis()
    with patch("redis.asyncio.from_url", return_value=mock_redis) as mock_from_url:
        probe = RedisProbe(url="redis://:secret@myhost:6380/1")
        await probe.check()
    mock_from_url.assert_called_once_with("redis://:secret@myhost:6380/1", decode_responses=True)


@pytest.mark.asyncio
async def test_redis_probe_cluster_breakdown():
    from fastapi_watch.probes.redis import RedisProbe
    mock_redis = _make_mock_redis()
    mock_redis.scan_iter = MagicMock(return_value=_async_iter(["sess:abc", "sess:def", "cache:xyz"]))
    mock_redis.ttl = AsyncMock(return_value=300)
    with patch("redis.asyncio.from_url", return_value=mock_redis):
        probe = RedisProbe()
        result = await probe.check()
    clusters = result.details.get("clusters", {})
    assert clusters["sess"]["keys"] == 2
    assert clusters["cache"]["keys"] == 1
    assert clusters["sess"]["ttl_seconds"] == 300
