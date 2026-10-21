"""The timeboard dashboard: timestamps, embedded history, assets, JS helpers."""
import json
import pathlib
import re
import shutil
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi_watch import HealthRegistry
from fastapi_watch.dashboard import render_dashboard
from fastapi_watch.models import HealthReport, ProbeResult, ProbeStatus
from fastapi_watch.probes import NoOpProbe

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _embedded(html: str) -> dict:
    raw = re.search(r'<script type="application/json" id="fw-data">(.*?)</script>', html, re.S).group(1)
    return json.loads(raw)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_dashboard_js_helpers():
    result = subprocess.run(
        ["node", str(ROOT / "tests" / "js" / "dashboard_helpers.test.js")],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.asyncio
async def test_results_and_history_carry_checked_at():
    registry = HealthRegistry(FastAPI(), poll_interval_ms=None, serve_routes=False)
    registry.add(NoOpProbe(name="db"))
    [result] = await registry.run_all()
    assert result.checked_at is not None and result.checked_at.tzinfo is not None
    [stored] = (await registry._storage.get_history())["db"]
    assert stored.checked_at == result.checked_at


def test_dashboard_embeds_current_results_and_history():
    app = FastAPI()
    HealthRegistry(app, poll_interval_ms=None).add(NoOpProbe(name="db"))
    client = TestClient(app)
    client.get("/health/status")
    data = _embedded(client.get("/health/dashboard").text)
    assert [p["name"] for p in data["probes"]] == ["db"]
    assert len(data["history"]["db"]) >= 1
    assert data["history"]["db"][0]["checked_at"]


def test_embedded_data_cannot_close_the_script_element():
    hostile = ProbeResult(name="db", status=ProbeStatus.UNHEALTHY, error="</script><script>alert(1)</script>")
    html = render_dashboard(HealthReport.from_results([hostile]), stream_url="/s",
                            history={"db": [hostile]})
    data_block = html.split('id="fw-data">')[1].split("</script>")[0]
    assert "<" not in data_block and ">" not in data_block
    assert _embedded(html)["probes"][0]["error"] == "</script><script>alert(1)</script>"


def test_custom_renderer_skips_the_history_read():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None, dashboard=lambda report: "<p>custom</p>")
    calls = []
    original = registry._storage.get_history

    async def counting():
        calls.append(1)
        return await original()

    registry._storage.get_history = counting
    assert TestClient(app).get("/health/dashboard").text == "<p>custom</p>"
    assert calls == []


def test_page_is_self_contained():
    html = render_dashboard(HealthReport.from_results([]), stream_url="/s")
    assert not re.search(r'<(script|link)[^>]+(src|href)=["\']https?://', html)
    assert "@import" not in html


def test_timeboard_structure_rendered_server_side():
    probe = ProbeResult(name="db", status=ProbeStatus.DEGRADED, tags=["data"])
    html = render_dashboard(HealthReport.from_results([probe]), stream_url="/s")
    assert 'class="lane board-row" data-probe="db"' in html
    assert 'class="probe-card degraded board-row"' in html
    assert 'role="group" aria-label="Time range"' in html
    assert 'data-range="1h" aria-pressed="true"' in html


def test_static_assets_ship_with_the_package():
    from importlib.resources import files

    static = files("fastapi_watch") / "static"
    assert (static / "dashboard.css").is_file()
    assert (static / "dashboard.js").is_file()
