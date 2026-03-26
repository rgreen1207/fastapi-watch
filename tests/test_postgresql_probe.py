"""Tests for PostgreSQLProbe — mocked so no real Postgres needed."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi_watch.models import ProbeStatus

try:
    import asyncpg
    HAS_ASYNCPG = True
except ImportError:
    HAS_ASYNCPG = False

pytestmark = pytest.mark.skipif(not HAS_ASYNCPG, reason="asyncpg not installed")


def _make_mock_conn(fetchval_results=None, fetchval_exc=None):
    conn = AsyncMock()
    if fetchval_exc:
        conn.fetchval = AsyncMock(side_effect=fetchval_exc)
    else:
        # The probe runs 4 fetchval calls concurrently via asyncio.gather
        results = fetchval_results or ["PostgreSQL 16.2", 5, 100, "42 MB"]
        conn.fetchval = AsyncMock(side_effect=results)
    conn.close = AsyncMock()
    return conn


@pytest.mark.asyncio
async def test_postgresql_probe_healthy():
    from fastapi_watch.probes.postgresql import PostgreSQLProbe
    mock_conn = _make_mock_conn()
    with patch("asyncpg.connect", AsyncMock(return_value=mock_conn)):
        probe = PostgreSQLProbe(url="postgresql://user:pass@localhost/db")
        result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.name == "postgresql"
    mock_conn.close.assert_called_once()


@pytest.mark.asyncio
async def test_postgresql_probe_unhealthy_on_connect_error():
    from fastapi_watch.probes.postgresql import PostgreSQLProbe
    with patch("asyncpg.connect", AsyncMock(side_effect=Exception("connection refused"))):
        probe = PostgreSQLProbe(url="postgresql://user:pass@localhost/db")
        result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY
    assert "connection refused" in result.error


@pytest.mark.asyncio
async def test_postgresql_probe_unhealthy_on_query_error():
    from fastapi_watch.probes.postgresql import PostgreSQLProbe
    mock_conn = _make_mock_conn(fetchval_exc=Exception("auth failed"))
    with patch("asyncpg.connect", AsyncMock(return_value=mock_conn)):
        probe = PostgreSQLProbe(url="postgresql://user:pass@localhost/db")
        result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY
    assert "auth failed" in result.error
    mock_conn.close.assert_called_once()


@pytest.mark.asyncio
async def test_postgresql_probe_custom_name():
    from fastapi_watch.probes.postgresql import PostgreSQLProbe
    mock_conn = _make_mock_conn()
    with patch("asyncpg.connect", AsyncMock(return_value=mock_conn)):
        probe = PostgreSQLProbe(url="postgresql://localhost/db", name="primary-db")
        result = await probe.check()
    assert result.name == "primary-db"


@pytest.mark.asyncio
async def test_postgresql_probe_returns_details():
    from fastapi_watch.probes.postgresql import PostgreSQLProbe
    mock_conn = _make_mock_conn(fetchval_results=["PostgreSQL 16.2", 5, 100, "42 MB"])
    with patch("asyncpg.connect", AsyncMock(return_value=mock_conn)):
        probe = PostgreSQLProbe(url="postgresql://localhost/db")
        result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.details["version"] == "PostgreSQL 16.2"
    assert result.details["active_connections"] == 5
    assert result.details["max_connections"] == 100
    assert result.details["database_size"] == "42 MB"
