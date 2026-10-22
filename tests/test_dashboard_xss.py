"""Hostile probe data cannot inject markup or script into the dashboard.

Probe names, descriptions, tags, errors, detail keys and values, and route
paths all come from probes (custom ones included) and reach the page in
three places: element text, attribute values, and the embedded JSON the page
script reads.  The page script itself only ever assigns text.
"""
import json
import math
import pathlib
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi_watch import HealthRegistry
from fastapi_watch.dashboard import render_dashboard
from fastapi_watch.models import HealthReport, ProbeResult, ProbeStatus
from fastapi_watch.probes.base import BaseProbe

ROOT = pathlib.Path(__file__).resolve().parent.parent

_PAYLOADS = [
    "<script>alert(1)</script>",
    '"><img src=x onerror=alert(1)>',
    "'><svg onload=alert(1)>",
    "</script><!--<script>alert(1)//",
    "javascript:alert(1)",
    "  </SCRIPT >",
    "&lt;script&gt;",
]


class _TagCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.attrs: list[tuple[str, str | None]] = []
        self.script_bodies: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.extend(attrs)
        self._in_script = tag == "script"

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script:
            self.script_bodies.append(data)


def _hostile_result(i: int, payload: str) -> ProbeResult:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return ProbeResult(
        name=f"p{i}{payload}",
        status=ProbeStatus.UNHEALTHY,
        latency_ms=1.5,
        error=payload,
        description=payload,
        tags=[payload, "safe"],
        checked_at=start + timedelta(seconds=i),
        details={
            payload: payload,
            "last_error": payload,  # rendered into the error-count button's data-error attribute
            "error_count": 3,
            "error_types": {payload: 1},
            "status_distribution": {payload: 2},
            "routes": {payload: {"request_count": 5, "p95_rtt_ms": 1.0, "error_rate": 0.0}},
        },
    )


def _render() -> str:
    results = [_hostile_result(i, payload) for i, payload in enumerate(_PAYLOADS)]
    report = HealthReport.from_results(results, checked_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                                       timezone="<b>UTC</b>")
    return render_dashboard(report, stream_url='/s?x="><script>alert(1)</script>', maintenance_banner=True,
                            history={r.name: [r] for r in results})


def test_hostile_values_never_become_markup():
    html = _render()
    parser = _TagCollector()
    parser.feed(html)

    # Only the page's own elements: its two scripts, no injected img or b,
    # and no event-handler attribute (an injected <svg onload> would carry one).
    assert parser.tags.count("script") == 2
    assert "img" not in parser.tags
    assert "b" not in parser.tags
    assert not any(name.startswith("on") for name, _ in parser.attrs)
    assert not any((value or "").lower().startswith("javascript:") for name, value in parser.attrs
                   if name in ("href", "src", "action", "formaction"))


def test_hostile_values_round_trip_as_text_and_attributes():
    parser = _TagCollector()
    parser.feed(_render())
    values = {value for _, value in parser.attrs}
    # Attribute values decode back to the exact payload: escaped, not mangled.
    for i, payload in enumerate(_PAYLOADS):
        assert f"p{i}{payload}" in values, payload
        assert payload in values, payload  # data-error / data-tag


def test_embedded_json_cannot_break_out_of_its_script_element():
    html = _render()
    raw = re.search(r'<script type="application/json" id="fw-data">(.*?)</script>', html, re.S).group(1)
    assert "<" not in raw and ">" not in raw  # no </script>, no <!--
    data = json.loads(raw)
    assert [p["error"] for p in data["probes"]] == _PAYLOADS
    assert list(data["history"]) == [f"p{i}{payload}" for i, payload in enumerate(_PAYLOADS)]


def test_non_finite_numbers_do_not_break_the_embedded_json():
    # json.dumps writes NaN/Infinity, which JSON.parse rejects: one such value
    # used to cost the page all of its embedded data, and so all its charts.
    nan = float("nan")
    result = ProbeResult(
        name="db", status=ProbeStatus.HEALTHY, latency_ms=nan,
        checked_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        details={"error_rate": nan, "p95_rtt_ms": float("inf"),
                 "routes": {"/a": {"request_count": 1, "p95_rtt_ms": nan}}},
    )
    html = render_dashboard(HealthReport.from_results([result]), stream_url="/s", history={"db": [result]})
    raw = re.search(r'id="fw-data">(.*?)</script>', html, re.S).group(1)

    def reject(constant):
        raise ValueError(constant)

    data = json.loads(raw, parse_constant=reject)
    [point] = data["history"]["db"]
    assert point["latency_ms"] is None
    assert point["details"]["error_rate"] is None and point["details"]["p95_rtt_ms"] is None
    assert point["details"]["routes"]["/a"]["p95_rtt_ms"] is None
    assert data["probes"][0]["latency_ms"] is None
    assert math.isnan(result.latency_ms)  # the result itself is untouched


def test_unexpected_detail_types_do_not_fail_the_page():
    # A custom probe's details are free-form; one odd value must not 500 the dashboard.
    result = ProbeResult(
        name="custom", status=ProbeStatus.HEALTHY,
        details={
            "error_rate": "n/a",
            "status_distribution": {200: 4, "other": 1},
            "routes": {"/a": {"request_count": 1, "error_rate": "n/a", "p95_rtt_ms": "?"}},
        },
    )
    html = render_dashboard(HealthReport.from_results([result]), stream_url="/s")
    assert "n/a" in html
    assert "200: 4, other: 1" in html


def test_unexpected_route_keys_and_counts_do_not_fail_the_page():
    # Mixed-type route keys failed sorting the routes table (dashboard 500);
    # a non-numeric request_count failed ranking, which the dashboard's live
    # updates (?view=dashboard) also run for every event.
    from fastapi_watch.dashboard import _current_probe

    result = ProbeResult(
        name="custom", status=ProbeStatus.HEALTHY,
        details={"routes": {
            1: {"request_count": 3}, "/a": {"request_count": "5"}, "/b": {"request_count": None},
            "/c": {"request_count": math.nan}, "/d": {"request_count": 7},
        }},
    )
    html = render_dashboard(HealthReport.from_results([result]), stream_url="/s", history={"custom": [result]})
    assert "<td>/d</td>" in html and "<td>1</td>" in html
    assert list(_current_probe(result)["details"]["routes"])[:2] == ["/d", "1"]


class _HostileProbe(BaseProbe):
    name = "hostile"
    description = _PAYLOADS[1]
    tags = [_PAYLOADS[0]]

    async def check(self) -> ProbeResult:
        return ProbeResult(
            name=self.name, status=ProbeStatus.UNHEALTHY, error=_PAYLOADS[3],
            details={"routes": {_PAYLOADS[1]: {"request_count": 1}}, _PAYLOADS[2]: _PAYLOADS[0]},
        )


def test_served_dashboard_escapes_hostile_probe_output():
    app = FastAPI()
    HealthRegistry(app, poll_interval_ms=None, circuit_breaker=False).add(_HostileProbe())
    client = TestClient(app)
    client.get("/health/status")
    response = client.get("/health/dashboard")
    assert response.status_code == 200
    parser = _TagCollector()
    parser.feed(response.text)
    assert parser.tags.count("script") == 2
    assert "img" not in parser.tags
    assert not any(name.startswith("on") for name, _ in parser.attrs)


def test_page_script_only_assigns_text():
    # Every string from the server reaches the DOM through textContent or
    # setAttribute on data-/class attributes; nothing parses markup.
    js = (ROOT / "src" / "fastapi_watch" / "static" / "dashboard.js").read_text()
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                 "createContextualFragment", "DOMParser"):
        assert sink not in js, sink
    # Selectors built from probe names are escaped (a quote would throw).
    for line in js.splitlines():
        if "data-probe=" in line and "+" in line and "querySelector" in line:
            raise AssertionError(f"unescaped selector: {line.strip()}")
    assert "CSS.escape(" in js
