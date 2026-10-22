"""Tests for KafkaProbe — mocked so no real Kafka needed."""
import importlib.util
import sys
import types

import pytest
from unittest.mock import AsyncMock, patch
from fastapi_watch.models import ProbeStatus

HAS_AIOKAFKA = importlib.util.find_spec("aiokafka") is not None


@pytest.fixture(autouse=True)
def _aiokafka_module(monkeypatch):
    """Stand in for aiokafka when it is not installed: every test patches the client anyway."""
    if HAS_AIOKAFKA:
        return
    admin = types.ModuleType("aiokafka.admin")
    admin.AIOKafkaAdminClient = None
    package = types.ModuleType("aiokafka")
    package.admin = admin
    monkeypatch.setitem(sys.modules, "aiokafka", package)
    monkeypatch.setitem(sys.modules, "aiokafka.admin", admin)


def _make_mock_admin(start_exc=None):
    client = AsyncMock()
    if start_exc:
        client.start = AsyncMock(side_effect=start_exc)
    else:
        client.start = AsyncMock(return_value=None)
    client.list_topics = AsyncMock(return_value=[])
    client.describe_cluster = AsyncMock(return_value={"brokers": [], "controller_id": None})
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
    assert result.error.startswith("probe check failed: ")


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


def _admin_with_metadata(topics: list[str]):
    """An admin client returning what aiokafka really returns: names, and a dict for the cluster."""
    client = _make_mock_admin()
    client.list_topics = AsyncMock(return_value=topics)
    client.describe_cluster = AsyncMock(return_value={
        "cluster_id": "abc",
        "controller_id": 2,
        "brokers": [{"node_id": i, "host": f"b{i}", "port": 9092, "rack": None} for i in (1, 2, 3)],
    })
    return client


@pytest.mark.asyncio
async def test_kafka_probe_reads_cluster_details_from_the_metadata_dict():
    # describe_cluster() returns a dict; attribute access raised and the
    # details were silently dropped on every check.
    from fastapi_watch.probes.kafka import KafkaProbe
    with patch("aiokafka.admin.AIOKafkaAdminClient", return_value=_admin_with_metadata(["orders", "__consumer_offsets"])):
        result = await KafkaProbe().check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.details == {
        "broker_count": 3,
        "controller_id": 2,
        "topic_count": 1,
        "topics": ["orders"],
        "internal_topics": ["__consumer_offsets"],
    }


@pytest.mark.asyncio
async def test_kafka_probe_caps_listed_topics():
    from fastapi_watch.probes import kafka
    topics = [f"topic-{i:05d}" for i in range(5000)]
    with patch("aiokafka.admin.AIOKafkaAdminClient", return_value=_admin_with_metadata(topics)):
        result = await kafka.KafkaProbe().check()
    assert result.details["topic_count"] == 5000
    assert result.details["topics"] == topics[:kafka._MAX_TOPICS_LISTED]
