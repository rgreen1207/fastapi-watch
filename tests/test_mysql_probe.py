"""Tests for MySQLProbe — mocked so no real MySQL needed."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi_watch.models import ProbeStatus

try:
    import aiomysql
    HAS_AIOMYSQL = True
except ImportError:
    HAS_AIOMYSQL = False

pytestmark = pytest.mark.skipif(not HAS_AIOMYSQL, reason="aiomysql not installed")


def _make_mock_conn(execute_exc=None):
    cursor = AsyncMock()
    if execute_exc:
        cursor.execute = AsyncMock(side_effect=execute_exc)
    else:
        cursor.execute = AsyncMock(return_value=None)
    cursor.__aenter__ = AsyncMock(return_value=cursor)
    cursor.__aexit__ = AsyncMock(return_value=False)

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cursor)
    conn.close = MagicMock()
    return conn


@pytest.mark.asyncio
async def test_mysql_probe_healthy():
    from fastapi_watch.probes.mysql import MySQLProbe
    mock_conn = _make_mock_conn()
    with patch("aiomysql.connect", AsyncMock(return_value=mock_conn)):
        probe = MySQLProbe(host="localhost", user="root", password="pass", db="mydb")
        result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.name == "mysql"
    mock_conn.close.assert_called_once()


@pytest.mark.asyncio
async def test_mysql_probe_url_parsing():
    from fastapi_watch.probes.mysql import MySQLProbe
    probe = MySQLProbe(url="mysql://admin:secret@db.example.com:3307/appdb")
    assert probe._host == "db.example.com"
    assert probe._port == 3307
    assert probe._user == "admin"
    assert probe._password == "secret"
    assert probe._db == "appdb"


@pytest.mark.asyncio
async def test_mysql_probe_unhealthy_on_connect_error():
    from fastapi_watch.probes.mysql import MySQLProbe
    with patch("aiomysql.connect", AsyncMock(side_effect=Exception("host unreachable"))):
        probe = MySQLProbe(host="localhost")
        result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY
    assert "host unreachable" in result.error


@pytest.mark.asyncio
async def test_mysql_probe_custom_name():
    from fastapi_watch.probes.mysql import MySQLProbe
    mock_conn = _make_mock_conn()
    with patch("aiomysql.connect", AsyncMock(return_value=mock_conn)):
        probe = MySQLProbe(host="localhost", name="replica-db")
        result = await probe.check()
    assert result.name == "replica-db"


@pytest.mark.asyncio
async def test_mysql_probe_unhealthy_on_query_error():
    from fastapi_watch.probes.mysql import MySQLProbe
    mock_conn = _make_mock_conn(execute_exc=Exception("access denied"))
    with patch("aiomysql.connect", AsyncMock(return_value=mock_conn)):
        probe = MySQLProbe(host="localhost")
        result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY
    assert "access denied" in result.error
    mock_conn.close.assert_called_once()
