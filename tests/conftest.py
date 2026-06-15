import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from fastapi import FastAPI
from fastapi_watch import HealthRegistry


@pytest.fixture
def app():
    return FastAPI()


@pytest.fixture
def registry(app):
    return HealthRegistry(app)
