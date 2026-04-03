"""Tests for KafkaProbe — mocked so no real Kafka needed."""
import pytest
from unittest.mock import AsyncMock, patch
from fastapi_watch.models import ProbeStatus

try:
    from aiokafka.admin import AIOKafkaAdminClient
    HAS_AIOKAFKA = True
except ImportError:
    HAS_AIOKAFKA = False

pytestmark = pytest.mark.skipif(not HAS_AIOKAFKA, reason="aiokafka not installed")


def _make_mock_admin(start_exc=None):
    client = AsyncMock()
    if start_exc:
        client.start = AsyncMock(side_effect=start_exc)
    else:
        client.start = AsyncMock(return_value=None)
    client.close = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_kafka_probe_healthy():
    from fastapi_watch.probes.kafka import KafkaProbe
    mock_admin = _make_mock_admin()
    with patch("aiokafka.admin.AIOKafkaAdminClient", return_value=mock_admin):
        probe = KafkaProbe(bootstrap_servers="localhost:9092")
        result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.name == "kafka"
    mock_admin.close.assert_called_once()


@pytest.mark.asyncio
async def test_kafka_probe_unhealthy_on_connection_error():
    from fastapi_watch.probes.kafka import KafkaProbe
    mock_admin = _make_mock_admin(start_exc=Exception("broker unavailable"))
    with patch("aiokafka.admin.AIOKafkaAdminClient", return_value=mock_admin):
        probe = KafkaProbe(bootstrap_servers="localhost:9092")
        result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY
    assert result.error == "probe check failed"


@pytest.mark.asyncio
async def test_kafka_probe_list_of_brokers():
    from fastapi_watch.probes.kafka import KafkaProbe
    probe = KafkaProbe(bootstrap_servers=["b1:9092", "b2:9092", "b3:9092"])
    assert probe.bootstrap_servers == "b1:9092,b2:9092,b3:9092"


@pytest.mark.asyncio
async def test_kafka_probe_custom_name():
    from fastapi_watch.probes.kafka import KafkaProbe
    mock_admin = _make_mock_admin()
    with patch("aiokafka.admin.AIOKafkaAdminClient", return_value=mock_admin):
        probe = KafkaProbe(bootstrap_servers="localhost:9092", name="events-cluster")
        result = await probe.check()
    assert result.name == "events-cluster"


@pytest.mark.asyncio
async def test_kafka_probe_close_called_even_on_start_error():
    from fastapi_watch.probes.kafka import KafkaProbe
    mock_admin = _make_mock_admin(start_exc=Exception("timeout"))
    with patch("aiokafka.admin.AIOKafkaAdminClient", return_value=mock_admin):
        probe = KafkaProbe(bootstrap_servers="localhost:9092")
        result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY
    mock_admin.close.assert_called_once()
