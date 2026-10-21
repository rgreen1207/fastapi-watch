"""Retention arguments reach a caller-supplied storage backend.

``HealthRegistry(storage=s, history_size=5, max_alerts=3)`` used to keep the
backend's own limits without a word, so storage could grow past the window
the caller asked for.
"""
import inspect
import logging

import pytest
from fastapi import FastAPI

from fastapi_watch import HealthRegistry
from fastapi_watch.storage import InMemoryProbeStorage


class _Configurable(InMemoryProbeStorage):
    def __init__(self):
        super().__init__()
        self.configured: list[dict] = []

    def configure(self, *, max_results, result_ttl_seconds, alert_ttl_seconds, max_alerts):
        self.configured.append({
            "max_results": max_results,
            "result_ttl_seconds": result_ttl_seconds,
            "alert_ttl_seconds": alert_ttl_seconds,
            "max_alerts": max_alerts,
        })


class _Plain(InMemoryProbeStorage):
    """A backend without configure()."""

    configure = None  # InMemoryProbeStorage implements it; hide it


def test_explicit_retention_is_passed_to_configure_with_all_four_values():
    storage = _Configurable()
    HealthRegistry(FastAPI(), storage=storage, history_size=5, max_alerts=3)
    assert storage.configured == [{
        "max_results": 5,
        "result_ttl_seconds": 7200.0,
        "alert_ttl_seconds": 259200.0,
        "max_alerts": 3,
    }]


def test_effective_values_are_clamped():
    storage = _Configurable()
    HealthRegistry(FastAPI(), storage=storage, history_size=0, max_alerts=-1, result_ttl_seconds=60)
    assert storage.configured[0]["max_results"] == 1
    assert storage.configured[0]["max_alerts"] == 1
    assert storage.configured[0]["result_ttl_seconds"] == 60.0


@pytest.mark.parametrize("arg, value", [
    ("history_size", 120),
    ("result_ttl_seconds", 7200.0),
    ("alert_ttl_seconds", 259200.0),
    ("max_alerts", 120),
])
def test_passing_the_default_value_explicitly_still_configures(arg, value):
    storage = _Configurable()
    HealthRegistry(FastAPI(), storage=storage, **{arg: value})
    assert len(storage.configured) == 1


def test_no_retention_args_leaves_the_backend_alone(caplog):
    storage = _Configurable()
    with caplog.at_level(logging.WARNING, logger="fastapi_watch"):
        HealthRegistry(FastAPI(), storage=storage)
        HealthRegistry(FastAPI(), storage=_Plain())
    assert storage.configured == []
    assert not any("ignored" in r.getMessage() for r in caplog.records)


def test_backend_without_configure_warns_naming_the_ignored_args(caplog):
    storage = _Plain()
    with caplog.at_level(logging.WARNING, logger="fastapi_watch"):
        HealthRegistry(FastAPI(), storage=storage, history_size=5, alert_ttl_seconds=60)
    messages = [r.getMessage() for r in caplog.records if "ignored" in r.getMessage()]
    assert len(messages) == 1
    assert "history_size" in messages[0] and "alert_ttl_seconds" in messages[0]
    assert "max_alerts" not in messages[0]
    assert storage._max_results == 120  # unchanged


def test_warning_goes_to_the_configured_logger():
    records = []

    class _Handler(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("test_storage_retention.custom")
    logger.addHandler(_Handler())
    HealthRegistry(FastAPI(), storage=_Plain(), max_alerts=3, logger=logger)
    assert any("max_alerts is ignored" in m for m in records)


def test_default_in_memory_path_is_unchanged():
    registry = HealthRegistry(FastAPI(), history_size=5, max_alerts=3, result_ttl_seconds=10, alert_ttl_seconds=20)
    storage = registry._storage
    assert isinstance(storage, InMemoryProbeStorage)
    assert (storage._max_results, storage._max_alerts) == (5, 3)
    assert (storage._result_ttl, storage._alert_ttl) == (10, 20)


def test_falsy_storage_is_still_used():
    """``storage or InMemory(...)`` replaced a backend that defines __len__."""

    class _Empty(InMemoryProbeStorage):
        def __len__(self):
            return 0

    storage = _Empty()
    assert HealthRegistry(FastAPI(), storage=storage)._storage is storage


def test_signature_defaults_still_read_as_plain_numbers():
    params = inspect.signature(HealthRegistry).parameters
    assert params["history_size"].default == 120
    assert params["max_alerts"].default == 120
    assert params["result_ttl_seconds"].default == 7200.0
    assert params["alert_ttl_seconds"].default == 259200.0
    assert repr(params["history_size"].default) == "120"
