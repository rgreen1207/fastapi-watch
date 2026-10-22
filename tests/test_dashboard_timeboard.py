"""The timeboard dashboard: timestamps, embedded history, assets, JS helpers."""
import json
import pathlib
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

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


# ---------------------------------------------------------------------------
# Embedded data stays small: only the fields the page script reads
# ---------------------------------------------------------------------------

_POINT_KEYS = {"checked_at", "status", "latency_ms", "details"}
_POINT_DETAIL_KEYS = {
    "p50_rtt_ms", "p95_rtt_ms", "p99_rtt_ms", "error_rate", "requests_per_minute", "lag_ms", "warn_ms", "fail_ms", "routes",
}
_ROUTE_KEYS = {"request_count", "p95_rtt_ms", "error_rate", "requests_per_minute"}


def _route_stats(count: int) -> dict:
    """One route as RequestMetricsProbe reports it."""
    return {
        "request_count": count, "error_count": count // 10, "error_rate": 0.1, "consecutive_errors": 0,
        "avg_rtt_ms": 12.34, "p50_rtt_ms": 10.5, "p95_rtt_ms": 40.25, "p99_rtt_ms": 80.75, "requests_per_minute": 120.5,
    }


def _request_metrics_history(routes: int = 150, entries: int = 120) -> list[ProbeResult]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        ProbeResult(
            name="request_metrics",
            status=ProbeStatus.HEALTHY,
            latency_ms=12.34,
            checked_at=start + timedelta(minutes=k),
            details={
                **_route_stats(routes * 100),
                "routes": {f"/api/v1/resource{i}/{{item_id}}": _route_stats(i * 10 + k) for i in range(routes)},
            },
        )
        for k in range(entries)
    ]


def test_page_with_many_routes_and_full_history_stays_small():
    # Before slimming, this page embedded every detail of every route of every
    # entry: about 4 MB, rebuilt on each load and auto-reload.
    history = _request_metrics_history()
    html = render_dashboard(HealthReport.from_results([history[-1]]), stream_url="/s",
                            history={"request_metrics": history})
    assert len(html.encode()) < 200_000  # 215 KB while older entries kept every charted route field


def test_embedded_history_keeps_only_charted_fields():
    history = _request_metrics_history(routes=20, entries=5)
    history[0] = history[0].model_copy(update={"error": "boom", "tags": ["x"], "description": "d"})
    html = render_dashboard(HealthReport.from_results([history[-1]]), stream_url="/s",
                            history={"request_metrics": history})
    points = _embedded(html)["history"]["request_metrics"]

    assert len(points) == 5
    newest = history[-1].details["routes"]
    top = sorted(newest, key=lambda p: (-newest[p]["request_count"], p))[:8]
    for point in points:
        assert set(point) <= _POINT_KEYS
        assert set(point["details"]) <= _POINT_DETAIL_KEYS
        # The same busiest routes of the newest entry in every entry, so each line spans the history.
        assert list(point["details"]["routes"]) == top
        # The newest entry feeds the route labels and ranking; older ones only the p95 line.
        keys = _ROUTE_KEYS if point is points[-1] else {"p95_rtt_ms"}
        for stats in point["details"]["routes"].values():
            assert set(stats) == keys
    assert points[-1]["details"]["p95_rtt_ms"] == 40.25
    assert points[-1]["details"]["routes"][top[0]]["p95_rtt_ms"] == 40.25


def test_embedded_history_keeps_event_loop_fields_and_drops_others():
    result = ProbeResult(
        name="loop", status=ProbeStatus.DEGRADED, latency_ms=3.0, checked_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        details={"lag_ms": 120.0, "warn_ms": 100, "fail_ms": 500, "message": "slow", "samples": list(range(100))},
    )
    bare = ProbeResult(name="loop", status=ProbeStatus.HEALTHY, checked_at=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc))
    html = render_dashboard(HealthReport.from_results([bare]), stream_url="/s", history={"loop": [result, bare]})
    first, second = _embedded(html)["history"]["loop"]
    assert first["details"] == {"lag_ms": 120.0, "warn_ms": 100, "fail_ms": 500}
    assert first["status"] == "degraded" and first["latency_ms"] == 3.0
    assert "details" not in second


def test_embedded_history_capped_per_probe():
    from fastapi_watch.dashboard import _MAX_CHART_POINTS

    history = _request_metrics_history(routes=1, entries=_MAX_CHART_POINTS + 50)
    html = render_dashboard(HealthReport.from_results([history[-1]]), stream_url="/s",
                            history={"request_metrics": history})
    points = _embedded(html)["history"]["request_metrics"]
    assert len(points) == _MAX_CHART_POINTS
    assert points[-1]["checked_at"].startswith(history[-1].checked_at.isoformat()[:19])


def test_embedded_data_is_encoded_one_probe_at_a_time(monkeypatch):
    # The page renders in a worker thread, but json.dumps holds the GIL for a
    # whole call: encoding every probe's history at once stalled the app's
    # event loop for as long as it took (447 ms with 200 probes x 2000 points).
    # One call per probe lets the loop run between them.
    import fastapi_watch.dashboard as dashboard_module

    sizes: list[int] = []
    real_dumps = dashboard_module.json.dumps

    def _recording_dumps(*args, **kwargs):
        out = real_dumps(*args, **kwargs)
        sizes.append(len(out))
        return out

    monkeypatch.setattr(dashboard_module.json, "dumps", _recording_dumps)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    history = {
        f"p{i}": [
            ProbeResult(name=f"p{i}", status=ProbeStatus.HEALTHY, latency_ms=1.0,
                        checked_at=start + timedelta(seconds=10 * k), details={"p95_rtt_ms": 2.0})
            for k in range(200)
        ]
        for i in range(20)
    }
    report = HealthReport.from_results([results[-1] for results in history.values()])
    html = render_dashboard(report, stream_url="/s", history=history)
    data = _embedded(html)
    assert [p["name"] for p in data["probes"]] == list(history)
    assert {name: len(points) for name, points in data["history"].items()} == {name: 200 for name in history}
    assert max(sizes) < sum(sizes) / 10


def test_embedded_current_probe_keeps_details_but_trims_routes():
    [result] = _request_metrics_history(routes=150, entries=1)
    html = render_dashboard(HealthReport.from_results([result]), stream_url="/s")
    [probe] = _embedded(html)["probes"]
    assert probe["details"]["avg_rtt_ms"] == 12.34  # every other detail is kept
    assert probe["details"]["consecutive_errors"] == 0
    assert len(probe["details"]["routes"]) == 8
    assert list(probe["details"]["routes"])[0] == "/api/v1/resource149/{item_id}"
    # The server-rendered "All fields" table still lists every route.
    markup = html.split('<script type="application/json"')[0]
    assert markup.count("/api/v1/resource") == 150


def test_route_ranking_matches_the_page_script():
    from fastapi_watch.dashboard import _top_routes

    routes = {"/b": {"request_count": 5}, "/a": {"request_count": 5}, "/c": {"request_count": 9}, "/d": "junk", "/e": {}}
    assert _top_routes(routes) == ["/c", "/a", "/b", "/d", "/e"]
