import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi_watch.probes.tcp import TCPProbe
from fastapi_watch.models import ProbeStatus


@pytest.mark.asyncio
async def test_tcp_healthy_on_successful_connect():
    mock_reader = MagicMock()
    mock_writer = MagicMock()
    mock_writer.wait_closed = AsyncMock()

    with (
        patch("socket.getaddrinfo", return_value=[
            (None, None, None, None, ("127.0.0.1", 6379))
        ]),
        patch("asyncio.open_connection", new=AsyncMock(return_value=(mock_reader, mock_writer))),
    ):
        probe = TCPProbe("localhost", 6379)
        result = await probe.check()

    assert result.status == ProbeStatus.HEALTHY
    assert result.details["host"] == "localhost"
    assert result.details["port"] == 6379
    assert "127.0.0.1" in result.details["resolved_ips"]
    assert result.latency_ms >= 0


@pytest.mark.asyncio
async def test_tcp_unhealthy_on_connection_refused():
    with (
        patch("socket.getaddrinfo", return_value=[
            (None, None, None, None, ("127.0.0.1", 9999))
        ]),
        patch("asyncio.open_connection", new=AsyncMock(side_effect=ConnectionRefusedError("refused"))),
    ):
        probe = TCPProbe("localhost", 9999)
        result = await probe.check()

    assert result.status == ProbeStatus.UNHEALTHY
    assert result.error == "probe check failed"


@pytest.mark.asyncio
async def test_tcp_unhealthy_on_dns_failure():
    import socket
    with patch("socket.getaddrinfo", side_effect=socket.gaierror("name resolution failed")):
        probe = TCPProbe("nonexistent.invalid", 80)
        result = await probe.check()

    assert result.status == ProbeStatus.UNHEALTHY


@pytest.mark.asyncio
async def test_tcp_default_name():
    probe = TCPProbe("db.internal", 5432)
    assert probe.name == "tcp:db.internal:5432"


@pytest.mark.asyncio
async def test_tcp_custom_name():
    probe = TCPProbe("db.internal", 5432, name="primary-db-tcp")
    assert probe.name == "primary-db-tcp"


@pytest.mark.asyncio
async def test_tcp_unhealthy_on_timeout():
    import asyncio
    with (
        patch("socket.getaddrinfo", return_value=[
            (None, None, None, None, ("127.0.0.1", 80))
        ]),
        patch(
            "asyncio.open_connection",
            new=AsyncMock(side_effect=asyncio.TimeoutError()),
        ),
    ):
        probe = TCPProbe("slow.host", 80, timeout=0.001)
        result = await probe.check()

    assert result.status == ProbeStatus.UNHEALTHY
