"""Server-side rendered HTML health dashboard.

:func:`render_dashboard` produces a single self-contained HTML page from a
:class:`~fastapi_watch.models.HealthReport`.  The page auto-refreshes via a
Server-Sent Events stream so probe cards update in real-time without a full
page reload.

The dashboard is enabled by default in :class:`~fastapi_watch.HealthRegistry`
and served at ``<prefix>/dashboard``.  Pass ``dashboard=False`` to disable it,
or pass a custom renderer callable to replace the default design.
"""
import html as _html
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
    ("call_count",           "Total calls instrumented by this probe since startup (passive probes)."),
    ("error_count",          "Requests or calls that returned a status code at or above the error threshold (default: 500)."),
    ("error_rate",           "Fraction of requests counted as errors (error_count ÷ request_count)."),
    ("consecutive_errors",   "Unbroken run of error responses since the last success; resets to 0 on any success."),
    ("last_status_code",     "HTTP status code returned by the most recent request."),
    ("requests_per_minute",  "Estimated throughput computed from recent request timestamps."),
    # RTT / latency
    ("last_rtt_ms",          "Execution time for the most recent request or call."),
    ("avg_rtt_ms",           "Exponential moving average response time; smoothed to reduce noise from outliers."),
    ("p95_rtt_ms",           "95th-percentile response time over the last window of requests."),
    ("min_rtt_ms",           "Fastest response time ever recorded by this probe."),
    ("max_rtt_ms",           "Slowest response time ever recorded by this probe."),
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
    ("topics",               "User-defined Kafka topics visible to the admin client."),
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
    if key == "error_rate":
        return f"{float(value):.2%}"
    if isinstance(value, float):
        if key.endswith("_ms"):
            return f"{value:.2f} ms"
        s = f"{value:.4f}".rstrip("0").rstrip(".")
        return s
    if isinstance(value, int) and key.endswith("_ms"):
        return f"{value} ms"
    if key == "circuit_breaker" and isinstance(value, dict):
        failures = value.get("consecutive_failures", 0)
        trips = value.get("trips_total", 0)
        trip_str = f", {trips} trip{'s' if trips != 1 else ''} total" if trips else ""
        if value.get("open"):
            return _e(f"open — suspended ({failures} consecutive failure{'s' if failures != 1 else ''}{trip_str})")
        return _e(f"closed{trip_str}")
    if isinstance(value, dict):
        return "—"
    return _e(value)


def _fmt_detail_key(key: str) -> str:
    return key.replace("_", " ").title()


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

_CSS = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  font-size: 14px;
  background: #f1f5f9;
  color: #1e293b;
  min-height: 100vh;
}

/* ── Header ─────────────────────────────────────────────────── */

.header {
  padding: 28px 32px 24px;
  transition: background 0.4s;
}
.header.healthy   { background: #15803d; }
.header.degraded  { background: #b45309; }
.header.unhealthy { background: #b91c1c; }

.maintenance-banner {
  background: #fef3c7;
  color: #92400e;
  border-bottom: 1px solid #fde68a;
  text-align: center;
  font-size: 13px;
  font-weight: 600;
  padding: 10px 16px;
  letter-spacing: .01em;
}

.header-inner {
  max-width: 1200px;
  margin: 0 auto;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
}

.header-left {}

.status-label {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: .1em;
  text-transform: uppercase;
  color: rgba(255,255,255,.65);
  margin-bottom: 6px;
}

.status-badge {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  font-size: 26px;
  font-weight: 700;
  color: #fff;
  letter-spacing: -.02em;
}

.status-dot {
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: #fff;
  flex-shrink: 0;
}
.healthy  .status-dot { box-shadow: 0 0 0 4px rgba(255,255,255,.25); }
.degraded .status-dot { animation: pulse-amber 1.4s ease-in-out infinite; }
.unhealthy .status-dot { animation: pulse-red 1.4s ease-in-out infinite; }

@keyframes pulse-amber {
  0%, 100% { box-shadow: 0 0 0 0 rgba(255,255,255,.5); }
  50%       { box-shadow: 0 0 0 7px rgba(255,255,255,0); }
}

@keyframes pulse-red {
  0%, 100% { box-shadow: 0 0 0 0 rgba(255,255,255,.5); }
  50%       { box-shadow: 0 0 0 7px rgba(255,255,255,0); }
}

.status-subtitle {
  margin-top: 6px;
  font-size: 13px;
  color: rgba(255,255,255,.7);
}

.header-right {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 8px;
  padding-top: 4px;
}

.live-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 600;
  color: rgba(255,255,255,.8);
  background: rgba(255,255,255,.12);
  border: 1px solid rgba(255,255,255,.2);
  border-radius: 999px;
  padding: 4px 12px;
}

.live-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: rgba(255,255,255,.5);
  transition: background .3s;
}
.live-dot.connected {
  background: #4ade80;
  animation: pulse-live 2s ease-in-out infinite;
}
@keyframes pulse-live {
  0%, 100% { opacity: 1; }
  50%       { opacity: .5; }
}

.checked-at {
  font-size: 12px;
  color: rgba(255,255,255,.55);
}

/* ── Probe grid ──────────────────────────────────────────────── */

.content {
  max-width: 1200px;
  margin: 32px auto;
  padding: 0 32px 48px;
}

.section-title {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: .08em;
  text-transform: uppercase;
  color: #64748b;
  margin-bottom: 14px;
}

.probe-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 16px;
}

/* ── Probe card ──────────────────────────────────────────────── */

.probe-card {
  background: #fff;
  border-radius: 10px;
  border: 1px solid #e2e8f0;
  border-left-width: 4px;
  overflow: hidden;
  transition: border-left-color .3s;
}
.probe-card.healthy   { border-left-color: #16a34a; }
.probe-card.degraded  { border-left-color: #d97706; }
.probe-card.unhealthy { border-left-color: #dc2626; }

.probe-card-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 16px 10px;
}

.probe-indicator {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  flex-shrink: 0;
  transition: background .3s;
}
.probe-card.healthy   .probe-indicator { background: #16a34a; }
.probe-card.degraded  .probe-indicator { background: #d97706; }
.probe-card.unhealthy .probe-indicator { background: #dc2626; }

.probe-name {
  font-weight: 600;
  font-size: 15px;
  flex: 1;
  color: #0f172a;
}

.badge {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: .05em;
  text-transform: uppercase;
  border-radius: 4px;
  padding: 2px 6px;
}
.badge-optional {
  background: #f1f5f9;
  color: #64748b;
  border: 1px solid #e2e8f0;
}
.badge-healthy {
  background: #dcfce7;
  color: #15803d;
}
.badge-degraded {
  background: #fef3c7;
  color: #92400e;
}
.badge-unhealthy {
  background: #fee2e2;
  color: #b91c1c;
}

.probe-latency {
  font-size: 12px;
  font-weight: 600;
  color: #3b82f6;
  background: #eff6ff;
  border-radius: 4px;
  padding: 2px 7px;
  white-space: nowrap;
}

.probe-error {
  margin: 0 16px 12px;
  padding: 8px 10px;
  font-size: 12px;
  color: #991b1b;
  background: #fff5f5;
  border: 1px solid #fecaca;
  border-radius: 6px;
  font-family: ui-monospace, monospace;
  word-break: break-word;
}

/* ── Details table ───────────────────────────────────────────── */

.details-table {
  width: 100%;
  border-collapse: collapse;
  border-top: 1px solid #f1f5f9;
}
.details-table tr:not(:last-child) td {
  border-bottom: 1px solid #f8fafc;
}
.details-table td {
  padding: 6px 16px;
  font-size: 12.5px;
  vertical-align: top;
}
.details-table td:first-child {
  color: #64748b;
  width: 55%;
  padding-right: 8px;
}
.details-table td:last-child {
  color: #0f172a;
  font-weight: 500;
  word-break: break-all;
}
.details-table tr:last-child td {
  padding-bottom: 14px;
}

/* ── Field glossary ──────────────────────────────────────────── */

.glossary-details {
  margin: 20px 0 8px;
  background: #fff;
  border-radius: 10px;
  border: 1px solid #e2e8f0;
  overflow: hidden;
}

.glossary-details summary {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 11px 16px;
  cursor: pointer;
  user-select: none;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: .08em;
  text-transform: uppercase;
  color: #64748b;
  list-style: none;
}
.glossary-details summary::-webkit-details-marker { display: none; }
.glossary-details summary::before {
  content: '▶';
  font-size: 9px;
  transition: transform .2s;
  color: #94a3b8;
}
.glossary-details[open] summary::before {
  transform: rotate(90deg);
}
.glossary-details summary:hover {
  background: #f8fafc;
  color: #475569;
}

.glossary-table {
  width: 100%;
  border-collapse: collapse;
  border-top: 1px solid #e2e8f0;
}
.glossary-table th {
  text-align: left;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: .06em;
  text-transform: uppercase;
  color: #64748b;
  background: #f8fafc;
  padding: 8px 16px;
  border-bottom: 1px solid #e2e8f0;
}
.glossary-table td {
  padding: 7px 16px;
  font-size: 12.5px;
  vertical-align: top;
  border-bottom: 1px solid #f1f5f9;
}
.glossary-table tr:last-child td {
  border-bottom: none;
  padding-bottom: 12px;
}
.glossary-table td:first-child {
  width: 22%;
  font-family: ui-monospace, monospace;
  font-size: 12px;
  color: #3b82f6;
  white-space: nowrap;
}
.glossary-table td:last-child {
  color: #475569;
}

/* ── Footer ──────────────────────────────────────────────────── */

.footer {
  text-align: center;
  font-size: 11px;
  color: #94a3b8;
  padding: 16px 0 32px;
}
.footer a {
  color: #64748b;
  text-decoration: none;
}
.footer a:hover { text-decoration: underline; }

/* ── Responsive ──────────────────────────────────────────────── */

@media (max-width: 600px) {
  .header { padding: 20px 16px 18px; }
  .header-inner { flex-direction: column; gap: 12px; }
  .header-right { align-items: flex-start; }
  .content { padding: 0 16px 32px; margin-top: 20px; }
  .probe-grid { grid-template-columns: 1fr; }
  .glossary-table td:first-child { width: 35%; white-space: normal; }
}
"""

# ---------------------------------------------------------------------------
# JavaScript
# ---------------------------------------------------------------------------

_JS = r"""
(function () {
  var streamUrl = document.body.getAttribute('data-stream-url');
  if (!streamUrl) return;

  var liveDot  = document.getElementById('live-dot');
  var header   = document.querySelector('.header');
  var badge    = document.getElementById('status-badge-text');
  var subtitle = document.getElementById('status-subtitle');
  var checkedEl = document.getElementById('checked-at');

  var H_COLOR  = '#15803d';
  var D_COLOR  = '#b45309';
  var U_COLOR  = '#b91c1c';
  var H_LABEL  = 'All Systems Operational';
  var D_LABEL  = 'Degraded — one or more probes warning';
  var U_LABEL  = 'Unhealthy — one or more probes failing';

  function fmtMs(val) {
    if (val == null) return '—';
    return parseFloat(val).toFixed(2) + ' ms';
  }

  function fmtDetailVal(key, val) {
    if (val == null) return '—';
    if (key === 'error_rate') return (parseFloat(val) * 100).toFixed(2) + '%';
    if (key.endsWith('_ms') && typeof val === 'number') return val.toFixed(2) + ' ms';
    if (typeof val === 'boolean') return val ? 'yes' : 'no';
    if (typeof val === 'number') {
      var s = val.toFixed(4).replace(/\.?0+$/, '');
      return s;
    }
    if (key === 'circuit_breaker' && typeof val === 'object' && val !== null) {
      var failures = val.consecutive_failures || 0;
      var trips = val.trips_total || 0;
      var tripStr = trips ? ', ' + trips + ' trip' + (trips !== 1 ? 's' : '') + ' total' : '';
      if (val.open) {
        return 'open \u2014 suspended (' + failures + ' consecutive failure' + (failures !== 1 ? 's' : '') + tripStr + ')';
      }
      return 'closed' + tripStr;
    }
    if (typeof val === 'object') return '—';
    return String(val);
  }

  function statusLabel(s) {
    return s === 'healthy' ? 'Healthy' : s === 'degraded' ? 'Degraded' : 'Unhealthy';
  }
  function statusBadgeCls(s) {
    return s === 'healthy' ? 'badge-healthy' : s === 'degraded' ? 'badge-degraded' : 'badge-unhealthy';
  }

  function updateCard(probe) {
    var card = document.querySelector('[data-probe="' + probe.name + '"]');
    if (!card) return;

    card.className = 'probe-card ' + probe.status;

    var badgeEl = card.querySelector('.badge-status');
    if (badgeEl) {
      badgeEl.textContent = statusLabel(probe.status);
      badgeEl.className = 'badge badge-status ' + statusBadgeCls(probe.status);
    }

    var latEl = card.querySelector('.probe-latency');
    if (latEl) latEl.textContent = probe.latency_ms ? fmtMs(probe.latency_ms) : '—';

    var errEl = card.querySelector('.probe-error');
    if (errEl) {
      errEl.textContent = probe.error || '';
      errEl.style.display = probe.error ? '' : 'none';
    }

    var tbody = card.querySelector('.details-table tbody');
    if (tbody && probe.details) {
      var rows = '';
      Object.entries(probe.details).forEach(function(kv) {
        var k = kv[0], v = kv[1];
        if (v == null) return;
        var label = k.replace(/_/g, ' ').replace(/\b\w/g, function(c) { return c.toUpperCase(); });
        rows += '<tr><td>' + label + '</td><td>' + fmtDetailVal(k, v) + '</td></tr>';
      });
      tbody.innerHTML = rows;
    }
  }

  function applyReport(report) {
    var ok  = report.status === 'healthy';
    var deg = report.status === 'degraded';

    header.className = 'header ' + report.status;
    header.style.background = ok ? H_COLOR : (deg ? D_COLOR : U_COLOR);
    if (badge)    badge.textContent    = ok ? 'All Systems Operational' : (deg ? 'Degraded' : 'Unhealthy');
    if (subtitle) subtitle.textContent = ok ? H_LABEL : (deg ? D_LABEL : U_LABEL);

    if (checkedEl && report.checked_at) {
      var tz = report.timezone || 'UTC';
      checkedEl.textContent = 'Last checked ' + report.checked_at.replace('T', ' ').slice(0, 19) + ' ' + tz;
    }

    (report.probes || []).forEach(updateCard);
  }

  var source = new EventSource(streamUrl);

  source.onopen = function () {
    if (liveDot) liveDot.className = 'live-dot connected';
  };

  source.onmessage = function (e) {
    try { applyReport(JSON.parse(e.data)); } catch (_) {}
  };

  source.onerror = function () {
    if (liveDot) liveDot.className = 'live-dot';
  };
})();
"""

# ---------------------------------------------------------------------------
# Status label / badge lookups
# ---------------------------------------------------------------------------

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
_STATUS_TEXT = {
    ProbeStatus.HEALTHY: ("All Systems Operational", "All probes are passing."),
    ProbeStatus.DEGRADED: ("Degraded", "One or more probes are warning."),
    ProbeStatus.UNHEALTHY: ("Unhealthy", "One or more probes are failing."),
}

# ---------------------------------------------------------------------------
# Per-probe card renderer
# ---------------------------------------------------------------------------

def _probe_card(probe: ProbeResult) -> str:
    status_cls = probe.status.value
    status_label = _STATUS_LABEL[probe.status]
    badge_cls = _STATUS_BADGE_CLS[probe.status]

    latency = f"{probe.latency_ms:.2f} ms" if probe.latency_ms else "—"

    optional_badge = (
        '<span class="badge badge-optional">optional</span>'
        if not probe.critical else ""
    )
    status_badge = (
        f'<span class="badge badge-status {badge_cls}">{status_label}</span>'
    )

    error_style = "" if probe.error else ' style="display:none"'
    error_html = (
        f'<div class="probe-error"{error_style}>{_e(probe.error or "")}</div>'
    )

    details_html = ""
    if probe.details:
        rows = "".join(
            f"<tr><td>{_fmt_detail_key(k)}</td><td>{_fmt_detail_value(k, v)}</td></tr>"
            for k, v in probe.details.items()
            if v is not None
        )
        if rows:
            details_html = f'<table class="details-table"><tbody>{rows}</tbody></table>'

    return (
        f'<div class="probe-card {status_cls}" data-probe="{_e(probe.name)}">'
        f'  <div class="probe-card-header">'
        f'    <div class="probe-indicator"></div>'
        f'    <div class="probe-name">{_e(probe.name)}</div>'
        f'    {optional_badge}'
        f'    <span class="probe-latency">{latency}</span>'
        f'    {status_badge}'
        f'  </div>'
        f'  {error_html}'
        f'  {details_html}'
        f'</div>'
    )


def _glossary_html() -> str:
    rows = "".join(
        f"<tr><td>{_e(key)}</td><td>{_e(desc)}</td></tr>"
        for key, desc in _FIELD_GLOSSARY
    )
    return (
        '<details class="glossary-details">'
        "<summary>Field Reference</summary>"
        '<table class="glossary-table">'
        "<thead><tr><th>Field</th><th>Description</th></tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
        "</details>"
    )


# ---------------------------------------------------------------------------
# Full page renderer
# ---------------------------------------------------------------------------

def render_dashboard(
    report: HealthReport,
    stream_url: str,
    maintenance_banner: bool = False,
) -> str:
    header_cls = report.status.value
    status_text, status_subtitle = _STATUS_TEXT[report.status]

    if report.checked_at:
        tz = report.timezone or "UTC"
        ts = report.checked_at.strftime("%Y-%m-%d %H:%M:%S")
        checked_at_text = f"Last checked {ts} {tz}"
    else:
        checked_at_text = ""

    probe_count = len(report.probes)
    healthy_count = sum(1 for p in report.probes if p.is_healthy)
    degraded_count = sum(1 for p in report.probes if p.is_degraded)
    unhealthy_count = probe_count - healthy_count - degraded_count
    if degraded_count or unhealthy_count:
        summary = (
            f"{healthy_count} healthy, {degraded_count} degraded, {unhealthy_count} unhealthy"
            f" / {probe_count} probe{'s' if probe_count != 1 else ''}"
        )
    else:
        summary = f"{healthy_count} / {probe_count} probe{'s' if probe_count != 1 else ''} healthy"

    probe_cards = "\n".join(_probe_card(p) for p in report.probes)

    maint_html = (
        '<div class="maintenance-banner">&#128679; Scheduled maintenance in progress — '
        'probe failures are suppressed.</div>'
        if maintenance_banner
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Health Dashboard</title>
  <style>{_CSS}</style>
</head>
<body data-stream-url="{_e(stream_url)}">

  {maint_html}
  <header class="header {header_cls}">
    <div class="header-inner">
      <div class="header-left">
        <div class="status-label">System Status</div>
        <div class="status-badge">
          <div class="status-dot"></div>
          <span id="status-badge-text">{status_text}</span>
        </div>
        <div class="status-subtitle" id="status-subtitle">{status_subtitle}</div>
      </div>
      <div class="header-right">
        <div class="live-pill">
          <div class="live-dot" id="live-dot"></div>
          Live
        </div>
        <div class="checked-at" id="checked-at">{_e(checked_at_text)}</div>
      </div>
    </div>
  </header>

  <div class="content">
    {_glossary_html()}
    <div class="section-title">{summary}</div>
    <div class="probe-grid">
{probe_cards}
    </div>

    <div class="footer">
      <a href="status">JSON status</a> &nbsp;·&nbsp;
      <a href="history">History</a> &nbsp;·&nbsp;
      <a href="ready">Readiness</a> &nbsp;·&nbsp;
      <a href="metrics">Metrics</a>
    </div>
  </div>

  <script>{_JS}</script>
</body>
</html>"""
