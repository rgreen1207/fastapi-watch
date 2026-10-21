import pytest
from fastapi import FastAPI
from fastapi_watch import HealthRegistry
from fastapi_watch import alerts


@pytest.fixture
def app():
    return FastAPI()


@pytest.fixture
def registry(app):
    return HealthRegistry(app)


@pytest.fixture(autouse=True)
def _no_real_alerter_requests(monkeypatch):
    """Fail any alerter HTTP request a test did not mock.

    Every built-in alerter posts through ``alerts._OPENER``; a test that forgets
    to patch it would otherwise call Slack, PagerDuty, or OpsGenie for real.
    Tests that exercise requests patch ``_OPENER.open`` themselves, which
    takes precedence over this default.
    """
    def _refuse(request, *args, **kwargs):
        raise AssertionError(f"unmocked alerter request to {request.full_url}")

    monkeypatch.setattr(alerts._OPENER, "open", _refuse)
