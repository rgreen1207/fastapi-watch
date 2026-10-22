"""Server-side rendered HTML health dashboard.

:func:`render_dashboard` produces a single self-contained HTML page from a
:class:`~fastapi_watch.models.HealthReport`.  The page auto-refreshes via a
Server-Sent Events stream so probe rows and charts update in real time without
a full page reload.  The page is an aligned timeboard: a status tape (one lane
per probe) above per-probe latency, error-rate, and throughput charts, all on a
shared time axis, drawn as inline SVG with no external assets.

The dashboard is enabled by default in :class:`~fastapi_watch.HealthRegistry`
and served at ``<prefix>/dashboard``.  Pass ``dashboard=False`` to disable it,
or pass a custom renderer callable to replace the default design.
"""
import html as _html
import json
import math
from importlib.resources import files
from typing import Any

from .models import HealthReport, ProbeResult, ProbeStatus

# ---------------------------------------------------------------------------
# Field glossary — displayed as a reference table below the probe grid
# Ordered logically: general → request metrics → RTT → circuit breaker →
# WebSocket → event loop → TCP → infrastructure
# ---------------------------------------------------------------------------

_FIELD_GLOSSARY: list[tuple[str, str]] = [
    # General
    ("message",              "General status note from the probe."),
    ("description",          "Label identifying the route or operation this probe covers."),
    # Request / call counts
    ("request_count",        "Total requests observed by this probe since startup."),
    ("call_count",           "Total calls monitored by this probe since startup (passive probes)."),
    ("error_count",          "Requests or calls that returned a status code at or above the error threshold (default: 500). Click to reveal the last recorded error."),
    ("error_rate",           "Fraction of recent requests or calls (the last window_size) counted as errors."),
    ("consecutive_errors",   "Unbroken run of error responses since the last success; resets to 0 on any success."),
    ("last_status_code",     "HTTP status code returned by the most recent request."),
    ("requests_per_minute",  "Estimated throughput computed from recent request timestamps."),
    # Cache counters
    ("cache_hits",           "Cache hits in the last window of lookups (record_cache_hit()). Older entries roll off automatically."),
    ("cache_misses",         "Cache misses in the last window of lookups (record_cache_miss()). Older entries roll off automatically."),
    # RTT / latency
    ("last_rtt_ms",          "Execution time for the most recent request or call."),
    ("avg_rtt_ms",           "Exponential moving average response time; smoothed to reduce noise from outliers."),
    ("p50_rtt_ms",           "Median (50th-percentile) response time over the last window of requests."),
    ("p95_rtt_ms",           "95th-percentile response time over the last window of requests."),
    ("p99_rtt_ms",           "99th-percentile response time over the last window of requests — worst case for nearly all users."),
    ("min_rtt_ms",           "Fastest response time ever recorded by this probe."),
    ("max_rtt_ms",           "Slowest response time ever recorded by this probe."),
    ("slow_calls",           "Requests exceeding slow_call_threshold_ms in the last window_size calls."),
    # Distribution
    ("status_distribution",  "Count of responses per HTTP status-code family (2xx/3xx/4xx/5xx) over the last cache_window_size requests."),
    ("error_types",          "Count per exception class over the last cache_window_size errors (exceptions only, not status-code-only errors)."),
    # Circuit breaker
    ("circuit_breaker",      "Circuit breaker state. Counts consecutive failures and suspends the probe after repeated failures to avoid hammering a broken dependency, then retries after a cooldown."),
    # Route breakdown
    ("routes",               "Per-route request metrics collected by RequestMetricsMiddleware when per_route=True."),
    # WebSocket
    ("active_connections",   "WebSocket sockets currently open."),
    ("total_connections",    "All WebSocket connections observed since startup."),
    ("messages_received",    "Total messages received across all WebSocket connections."),
    ("messages_sent",        "Total messages sent across all WebSocket connections."),
    ("avg_duration_ms",      "Exponential moving average of WebSocket connection lifetimes."),
    ("min_duration_ms",      "Shortest WebSocket connection ever recorded."),
    ("max_duration_ms",      "Longest WebSocket connection ever recorded."),
    # Event loop
    ("lag_ms",               "Current asyncio event loop lag — time between scheduling a zero-delay task and its execution. High lag indicates CPU-bound work blocking the loop."),
    ("warn_ms",              "Lag threshold above which the probe reports DEGRADED."),
    ("fail_ms",              "Lag threshold above which the probe reports UNHEALTHY."),
    # TCP
    ("host",                 "Hostname or IP address targeted by the TCP probe."),
    ("port",                 "TCP port targeted by the probe."),
    ("resolved_ips",         "IP addresses resolved from the hostname at check time."),
    ("connect_ms",           "Time taken to open a TCP connection to the target."),
    # Celery
    ("workers_online",       "Number of Celery workers that responded to the ping broadcast."),
    ("workers",              "Worker details returned by Celery inspection (list when ping-only; dict when detailed=True)."),
    # Kafka
    ("broker_count",         "Number of Kafka brokers in the cluster."),
    ("controller_id",        "Node ID of the current Kafka cluster controller."),
    ("topic_count",          "Number of user-defined Kafka topics (the list below shows at most 100)."),
    ("topics",               "User-defined Kafka topics visible to the admin client (at most 100)."),
    ("internal_topics",      "Kafka-managed internal topics (e.g. __consumer_offsets)."),
    # RabbitMQ
    ("connected",            "Whether an AMQP connection to RabbitMQ succeeded."),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _e(value: Any) -> str:
    """HTML-escape a value for safe embedding."""
    return _html.escape(str(value))


def _fmt_detail_value(key: str, value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if key == "error_rate" and isinstance(value, (int, float)):
        return f"{value:.2%}"
    if isinstance(value, float):
        if key.endswith("_ms"):
            return f"{value:.2f} ms"
        formatted_value = f"{value:.4f}".rstrip("0").rstrip(".")
        return formatted_value
    if isinstance(value, int) and key.endswith("_ms"):
        return f"{value} ms"
    if key == "circuit_breaker" and isinstance(value, dict):
        failures = value.get("consecutive_failures", 0)
        trips = value.get("trips_total", 0)
        trip_str = f", {trips} trip{'s' if trips != 1 else ''} total" if trips else ""
        if value.get("open"):
            return _e(f"open — suspended ({failures} consecutive failure{'s' if failures != 1 else ''}{trip_str})")
        return _e(f"closed{trip_str}")
    if key in ("status_distribution", "error_types") and isinstance(value, dict):
        parts = ", ".join(f"{k}: {v}" for k, v in sorted(value.items(), key=lambda kv: str(kv[0])))
        return _e(parts) if parts else "—"
    if key == "routes" and isinstance(value, dict):
        return _routes_table(value)
    if isinstance(value, dict):
        return "—"
    return _e(value)


def _routes_table(routes: dict) -> str:
    """Render RequestMetricsProbe's per-route breakdown as a compact table."""
    if not routes:
        return "—"
    rows = "".join(
        f"<tr><td>{_e(path)}</td><td>{_e(stats.get('request_count', 0))}</td>"
        f"<td>{_e(stats.get('error_count', 0))}</td>"
        f"<td>{_fmt_detail_value('error_rate', stats.get('error_rate'))}</td>"
        f"<td>{_fmt_detail_value('p95_rtt_ms', stats.get('p95_rtt_ms'))}</td></tr>"
        for path, stats in sorted(routes.items())
        if isinstance(stats, dict)
    )
    return (
        '<table class="routes-table"><thead><tr><th>Route</th><th>Requests</th>'
        f"<th>Errors</th><th>Error rate</th><th>p95</th></tr></thead><tbody>{rows}</tbody></table>"
    )


def _fmt_detail_key(key: str) -> str:
    return _e(key.replace("_", " ").title())


# ---------------------------------------------------------------------------
# Assets — shipped as package files, inlined so the page stays self-contained
# ---------------------------------------------------------------------------

_STATIC = files(__package__) / "static"
_CSS = (_STATIC / "dashboard.css").read_text(encoding="utf-8")
_JS = (_STATIC / "dashboard.js").read_text(encoding="utf-8")

_RANGES = (("15m", "15 min"), ("1h", "1 hour"), ("2h", "2 hours"), ("all", "All"))

_STATUS_LABEL = {
    ProbeStatus.HEALTHY: "Healthy",
    ProbeStatus.DEGRADED: "Degraded",
    ProbeStatus.UNHEALTHY: "Unhealthy",
}
_STATUS_BADGE_CLS = {
    ProbeStatus.HEALTHY: "badge-healthy",
    ProbeStatus.DEGRADED: "badge-degraded",
    ProbeStatus.UNHEALTHY: "badge-unhealthy",
}


def _status_sentence(report: HealthReport) -> str:
    """The header's plain-language answer; the page script refines it with durations."""
    critical = [p for p in report.probes if p.critical]
    for status, word in ((ProbeStatus.UNHEALTHY, "unhealthy"), (ProbeStatus.DEGRADED, "degraded")):
        matching = [p.name for p in critical if p.status == status]
        if len(matching) == 1:
            return f"{matching[0]} {word}"
        if matching:
            return f"{len(matching)} probes {word}: {', '.join(matching)}"
    count = len(report.probes)
    return f"All {count} probe{'s' if count != 1 else ''} healthy" if count else "No probes registered"


def _summary(report: HealthReport) -> str:
    total = len(report.probes)
    healthy = sum(1 for p in report.probes if p.is_healthy)
    degraded = sum(1 for p in report.probes if p.is_degraded)
    unhealthy = total - healthy - degraded
    noun = f"probe{'s' if total != 1 else ''}"
    if degraded or unhealthy:
        return f"{healthy} healthy, {degraded} degraded, {unhealthy} unhealthy / {total} {noun}"
    return f"{healthy} / {total} {noun} healthy"


def _finite(value: Any) -> Any:
    """*value*, or ``None`` for a NaN or infinite float.

    ``json.dumps`` writes those as ``NaN``/``Infinity``, which ``JSON.parse``
    rejects — one such value would cost the page all of its embedded data.
    """
    return None if isinstance(value, float) and not math.isfinite(value) else value


def _json_for_script(value: Any) -> str:
    """JSON safe to embed in a ``<script type="application/json">`` element.

    ``<``, ``>`` and ``&`` are escaped so no value — a probe error message, a
    route path — can close the element or open a comment.
    """
    return (
        json.dumps(value, default=str, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


# ---------------------------------------------------------------------------
# Embedded chart data — only what static/dashboard.js reads
# ---------------------------------------------------------------------------

# Detail fields pointsFor() reads from each history entry.
_CHART_DETAIL_FIELDS = (
    "p50_rtt_ms", "p95_rtt_ms", "p99_rtt_ms", "error_rate", "requests_per_minute", "lag_ms", "warn_ms", "fail_ms",
)
# Per-route fields the routes section reads (topRoutes() ranks, drawRoutes() charts and labels).
_CHART_ROUTE_FIELDS = ("request_count", "p95_rtt_ms", "error_rate", "requests_per_minute")
# The only one it reads from older entries: their p95 line.
_CHART_ROUTE_HISTORY_FIELDS = ("p95_rtt_ms",)
# Routes charted; matches TOP_ROUTES in dashboard.js.
_CHART_ROUTES = 8
# Newest results embedded per probe; matches MAX_POINTS in dashboard.js.
_MAX_CHART_POINTS = 2000


def _top_routes(routes: dict) -> list[str]:
    """The busiest routes by request count, ranked as topRoutes() in dashboard.js ranks them."""
    def count(path: str) -> int:
        stats = routes[path]
        return (stats.get("request_count") or 0) if isinstance(stats, dict) else 0

    return sorted(routes, key=lambda path: (-count(path), path))[:_CHART_ROUTES]


def _slim_routes(routes: dict, keep: list[str], fields: tuple[str, ...] = _CHART_ROUTE_FIELDS) -> dict:
    """*routes* reduced to the paths in *keep*, each with only *fields* (by default all the charts read)."""
    return {
        path: {field: _finite(routes[path][field]) for field in fields if field in routes[path]}
        for path in keep
        if isinstance(routes.get(path), dict)
    }


def _chart_history(results: list[ProbeResult]) -> list[dict]:
    """One probe's stored history as chart points: a few fields per result, not every detail.

    Routes are cut to the busiest few of the newest breakdown — the ones the
    page charts — and kept under the same paths in every entry so each chart
    line covers the whole history; older entries carry only the charted p95.
    At most the newest ``_MAX_CHART_POINTS`` results are kept, as the page
    itself keeps no more.
    """
    results = results[-_MAX_CHART_POINTS:]
    keep: list[str] = []
    newest_routes = -1
    for index in range(len(results) - 1, -1, -1):
        routes = (results[index].details or {}).get("routes")
        if isinstance(routes, dict):
            keep = _top_routes(routes)
            newest_routes = index
            break
    points = []
    for index, result in enumerate(results):
        point: dict[str, Any] = {
            "checked_at": result.checked_at.isoformat() if result.checked_at else None,
            "status": result.status.value,
            "latency_ms": _finite(result.latency_ms),
        }
        details = result.details or {}
        slim = {field: _finite(details[field]) for field in _CHART_DETAIL_FIELDS if field in details}
        routes = details.get("routes")
        if isinstance(routes, dict):
            fields = _CHART_ROUTE_FIELDS if index == newest_routes else _CHART_ROUTE_HISTORY_FIELDS
            slim["routes"] = _slim_routes(routes, keep, fields)
        if slim:
            point["details"] = slim
        points.append(point)
    return points


def _current_probe(probe: ProbeResult) -> dict:
    """A current result for the page script: full details, but routes cut to those the page shows."""
    data = probe.model_dump(mode="json")
    # JSON mode nulls non-finite floats in details, not in typed fields.
    data["latency_ms"] = _finite(data["latency_ms"])
    routes = (data.get("details") or {}).get("routes")
    if isinstance(routes, dict):
        data["details"]["routes"] = _slim_routes(routes, _top_routes(routes))
    return data


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

def _lane(probe: ProbeResult) -> str:
    tags_attr = _e(",".join(probe.tags))
    return (
        f'<div class="lane board-row" data-probe="{_e(probe.name)}" data-name="{_e(probe.name)}" data-tags="{tags_attr}">'
        f'<div class="gutter"><span class="status-chip {probe.status.value}"></span>{_e(probe.name)}</div>'
        f'<div class="plot"><svg class="lane-svg" aria-hidden="true"></svg></div>'
        f"</div>"
    )


def _probe_card(probe: ProbeResult) -> str:
    """One timeboard row: identity and current values in the gutter, charts in the plot column."""
    status_label = _STATUS_LABEL[probe.status]
    badge_cls = _STATUS_BADGE_CLS[probe.status]
    latency = f"{probe.latency_ms:.2f} ms" if probe.latency_ms else "—"
    optional_badge = '<span class="badge badge-optional">optional</span>' if not probe.critical else ""

    probe_details = probe.details or {}
    last_error = probe_details.get("last_error")
    ts_parts = []
    if probe_details.get("last_error_at"):
        ts_parts.append(f"last error: {str(probe_details['last_error_at'])[:19].replace('T', ' ')} UTC")
    if probe_details.get("last_success_at"):
        ts_parts.append(f"last success: {str(probe_details['last_success_at'])[:19].replace('T', ' ')} UTC")
    ts_style = "" if ts_parts else ' style="display:none"'
    timestamps_html = f'<div class="probe-timestamps"{ts_style}>{" &bull; ".join(_e(p) for p in ts_parts)}</div>'
    error_style = "" if probe.error else ' style="display:none"'
    error_html = f'<div class="probe-error"{error_style}>{_e(probe.error or "")}</div>'

    rows = []
    for key, value in probe_details.items():
        if value is None or key in ("last_error", "last_error_at", "last_success_at"):
            continue
        if key == "error_count" and isinstance(value, int) and value > 0 and last_error:
            key_cell = f'<span class="error-count-btn" data-error="{_e(last_error)}" tabindex="0">Error Count &#9658;</span>'
        else:
            key_cell = _fmt_detail_key(key)
        rows.append(f"<tr><td>{key_cell}</td><td>{_fmt_detail_value(key, value)}</td></tr>")
    fields_html = (
        f'<details class="probe-fields"><summary>All fields</summary>'
        f'<table class="details-table"><tbody>{"".join(rows)}</tbody></table></details>'
        if rows else ""
    )

    description_html = f'<div class="probe-description">{_e(probe.description)}</div>' if probe.description else ""
    tags_html = ""
    if probe.tags:
        chips = "".join(f'<span class="probe-tag">{_e(t)}</span>' for t in probe.tags)
        tags_html = f'<div class="probe-tags">{chips}</div>'
    tags_attr = _e(",".join(probe.tags)) if probe.tags else ""

    return (
        f'<section class="probe-card {probe.status.value} board-row" data-probe="{_e(probe.name)}" '
        f'data-name="{_e(probe.name)}" data-tags="{tags_attr}" aria-label="{_e(probe.name)}">'
        f'<div class="gutter">'
        f'<div class="probe-name-group"><div class="probe-name">{_e(probe.name)}</div>{description_html}{tags_html}</div>'
        f'<div class="probe-meta"><span class="badge badge-status {badge_cls}">{status_label}</span>{optional_badge}'
        f'<span class="probe-latency">{latency}</span></div>'
        f"{error_html}{timestamps_html}"
        f"</div>"
        f'<div class="plot charts"><p class="charts-empty">Charts load with JavaScript.</p></div>'
        f"{fields_html}"
        f"</section>"
    )


def _glossary_html() -> str:
    rows = "".join(f"<tr><td>{_e(key)}</td><td>{_e(desc)}</td></tr>" for key, desc in _FIELD_GLOSSARY)
    return (
        '<details class="glossary-details"><summary>Field reference</summary>'
        '<table class="glossary-table"><thead><tr><th>Field</th><th>Description</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></details>"
    )


# ---------------------------------------------------------------------------
# Full page renderer
# ---------------------------------------------------------------------------

def render_dashboard(
    report: HealthReport,
    stream_url: str,
    maintenance_banner: bool = False,
    history: dict[str, list[ProbeResult]] | None = None,
) -> str:
    """Render the dashboard page.

    Args:
        report: Current results, rendered server-side so the page is useful
            before (and without) JavaScript.
        stream_url: SSE endpoint the page subscribes to for live updates.
        maintenance_banner: Show the maintenance-mode banner.
        history: Recent results per probe (``HealthRegistry`` passes its
            stored history); embedded so the charts have data on first paint.
            Only the fields the charts read are embedded, so the page stays
            small however many details or routes each result carries.
    """
    checked_at_text = ""
    if report.checked_at:
        checked_at_text = f"Last checked {report.checked_at.strftime('%Y-%m-%d %H:%M:%S')} {report.timezone or 'UTC'}"

    all_tags = sorted({t for p in report.probes for t in p.tags})
    tag_filter_html = ""
    if all_tags:
        buttons = "".join(f'<button class="tag-filter-btn" data-tag="{_e(t)}">{_e(t)}</button>' for t in all_tags)
        tag_filter_html = (
            f'<div id="tag-filter" class="tag-filter"><span class="tag-filter-label">Tags</span>{buttons}'
            f'<button id="tag-filter-clear" class="tag-filter-clear">Clear</button></div>'
        )
    range_buttons = "".join(
        f'<button type="button" data-range="{key}" aria-pressed="{"true" if key == "1h" else "false"}">{label}</button>'
        for key, label in _RANGES
    )
    maint_html = (
        '<div class="maintenance-banner">Maintenance in progress — probe failures are not alerting.</div>'
        if maintenance_banner else ""
    )
    data = {
        "probes": [_current_probe(p) for p in report.probes],
        "history": {name: _chart_history(results) for name, results in (history or {}).items()},
    }
    lanes = "".join(_lane(p) for p in report.probes)
    cards = "\n".join(_probe_card(p) for p in report.probes)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Service health</title>
  <style>{_CSS}</style>
</head>
<body data-stream-url="{_e(stream_url)}">
  {maint_html}
  <header class="header {report.status.value}">
    <div class="header-inner">
      <div class="header-left">
        <h1 class="status-badge" id="status-badge-text">{_e(_status_sentence(report))}</h1>
        <p class="status-subtitle" id="status-subtitle">{_e(_summary(report))}</p>
      </div>
      <div class="header-right">
        <div class="live-pill"><span class="live-dot" id="live-dot"></span>Live</div>
        <div class="checked-at" id="checked-at">{_e(checked_at_text)}</div>
      </div>
    </div>
  </header>
  <div class="controls">
    <div class="range" role="group" aria-label="Time range">{range_buttons}</div>
    <input id="probe-search" class="probe-search" type="search" placeholder="Filter probes" aria-label="Filter probes" autocomplete="off" />
    {tag_filter_html}
  </div>
  <main class="content">
    <section class="tape" aria-label="Status over time">
      <p class="tape-head">Status over time</p>
      <div class="board-row"><div class="gutter"></div><div class="plot"><svg class="axis-svg" aria-hidden="true"></svg></div></div>
      {lanes}
    </section>
    <h2 class="section-title" id="probe-summary">Probes</h2>
    <div class="probe-grid">
{cards}
    </div>
    <section class="routes" id="routes" hidden></section>
    {_glossary_html()}
    <div class="footer">
      <a href="status">JSON status</a> &nbsp;·&nbsp; <a href="history">History</a> &nbsp;·&nbsp;
      <a href="ready">Readiness</a> &nbsp;·&nbsp; <a href="metrics">Metrics</a>
    </div>
  </main>
  <div class="chart-tooltip" id="chart-tooltip" hidden></div>
  <script type="application/json" id="fw-data">{_json_for_script(data)}</script>
  <script>{_JS}</script>
</body>
</html>"""
