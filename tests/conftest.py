import pytest
from fastapi import FastAPI
from fastapi_watch import HealthRegistry


@pytest.fixture
def app():
    return FastAPI()


@pytest.fixture
def registry(app):
    return HealthRegistry(app)
