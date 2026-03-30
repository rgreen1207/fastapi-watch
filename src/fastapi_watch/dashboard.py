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
        # trim trailing zeros but keep at least one decimal for *_ms fields
        if key.endswith("_ms"):
            return f"{value:.2f} ms"
        s = f"{value:.4f}".rstrip("0").rstrip(".")
        return s
    if isinstance(value, int) and key.endswith("_ms"):
        return f"{value} ms"
    return _e(value)


def _fmt_detail_key(key: str) -> str:
    return key.replace("_", " ").title()


# ---------------------------------------------------------------------------
# CSS (static — no f-string escaping needed)
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
}
"""

# ---------------------------------------------------------------------------
# JavaScript (static — kept as a plain string to avoid f-string brace escaping)
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

    var ok = probe.status === 'healthy';
    card.className = 'probe-card ' + probe.status;

    // status badge
    var badgeEl = card.querySelector('.badge-status');
    if (badgeEl) {
      badgeEl.textContent = statusLabel(probe.status);
      badgeEl.className = 'badge badge-status ' + statusBadgeCls(probe.status);
    }

    // latency
    var latEl = card.querySelector('.probe-latency');
    if (latEl) latEl.textContent = probe.latency_ms ? fmtMs(probe.latency_ms) : '—';

    // error
    var errEl = card.querySelector('.probe-error');
    if (errEl) {
      errEl.textContent = probe.error || '';
      errEl.style.display = probe.error ? '' : 'none';
    }

    // details
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

    // header
    header.className = 'header ' + report.status;
    header.style.background = ok ? H_COLOR : (deg ? D_COLOR : U_COLOR);
    if (badge)    badge.textContent    = ok ? 'All Systems Operational' : (deg ? 'Degraded' : 'Unhealthy');
    if (subtitle) subtitle.textContent = ok ? H_LABEL : (deg ? D_LABEL : U_LABEL);

    // timestamp
    if (checkedEl && report.checked_at) {
      var tz = report.timezone || 'UTC';
      checkedEl.textContent = 'Last checked ' + report.checked_at.replace('T', ' ').slice(0, 19) + ' ' + tz;
    }

    // probes
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
    status_cls = probe.status.value  # "healthy" | "degraded" | "unhealthy"
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


# ---------------------------------------------------------------------------
# Full page renderer
# ---------------------------------------------------------------------------

def render_dashboard(
    report: HealthReport,
    stream_url: str,
    maintenance_banner: bool = False,
) -> str:
    header_cls = report.status.value  # "healthy" | "degraded" | "unhealthy"
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
