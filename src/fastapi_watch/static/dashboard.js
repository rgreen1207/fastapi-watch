/* fastapi-watch dashboard: an aligned timeboard of SVG charts, fed by the
   history embedded in the page and kept live by the SSE stream.  No
   dependencies.  Pure helpers come first and are exported for tests. */
(function (root) {
  'use strict';

  var RANGES = { '15m': 15 * 60e3, '1h': 60 * 60e3, '2h': 2 * 60 * 60e3, all: null };
  var PLOT_LEFT = 44;     // px reserved for y-axis labels; shared by every plot so x positions align
  var PLOT_RIGHT = 34;    // px reserved for direct line labels; shared so every plot's x range matches
  var PLOT_BOTTOM = 16;   // px reserved for x-axis labels
  var MIN_GAP_MS = 5000;  // never treat points closer than this as a gap
  // An open tab must not grow without bound: each probe keeps at most
  // MAX_POINTS points, and none older than the retention span (retentionMs).
  var MAX_POINTS = 2000;  // matches _MAX_CHART_POINTS in dashboard.py
  var TOP_ROUTES = 8;     // routes charted; matches _CHART_ROUTES in dashboard.py
  var RETRY_BASE_MS = 2000;       // first stream retry after a refused connection
  var RETRY_MAX_MS = 5 * 60e3;    // retries back off to at most this
  var MAX_STREAM_RETRIES = 6;     // then reload for a fresh page and token
  var ROUTE_FIELDS = ['request_count', 'p95_rtt_ms', 'error_rate', 'requests_per_minute'];

  // ── Pure helpers ──────────────────────────────────────────────────────

  function num(value) {
    return typeof value === 'number' && isFinite(value) ? value : null;
  }

  /** Smallest "nice" number (1, 2, 2.5, 5 × 10^n) at or above v; 1 for v <= 0. */
  function niceCeil(v) {
    if (!(v > 0)) return 1;
    var exp = Math.pow(10, Math.floor(Math.log10(v)));
    var steps = [1, 2, 2.5, 5, 10];
    for (var i = 0; i < steps.length; i++) {
      if (steps[i] * exp >= v) return steps[i] * exp;
    }
    return 10 * exp;
  }

  /** Linear map from [d0, d1] onto [r0, r1]; a zero-width domain maps to r0. */
  function scale(d0, d1, r0, r1) {
    var span = d1 - d0;
    return function (v) { return span ? r0 + ((v - d0) / span) * (r1 - r0) : r0; };
  }

  /** One plot point per probe result, sorted by time, one point per timestamp. */
  function pointsFor(results) {
    var byTime = {};
    (results || []).forEach(function (r) {
      var t = r && r.checked_at ? Date.parse(r.checked_at) : NaN;
      if (isNaN(t)) return;
      var d = r.details || {};
      byTime[t] = {
        t: t,
        status: r.status,
        latency: num(r.latency_ms) > 0 ? r.latency_ms : null,  // 0 = not measured (e.g. a failed check)
        p50: num(d.p50_rtt_ms),
        p95: num(d.p95_rtt_ms),
        p99: num(d.p99_rtt_ms),
        errorRate: num(d.error_rate),
        rpm: num(d.requests_per_minute),
        lag: num(d.lag_ms),
        warnMs: num(d.warn_ms),
        failMs: num(d.fail_ms),
        routes: d.routes && typeof d.routes === 'object' ? slimRoutes(d.routes, TOP_ROUTES) : null,
      };
    });
    return Object.keys(byTime).map(Number).sort(function (a, b) { return a - b; })
      .map(function (t) { return byTime[t]; });
  }

  /** Route paths ranked by request volume, busiest first. */
  function rankRoutes(routes) {
    return Object.keys(routes).sort(function (a, b) {
      return ((routes[b] || {}).request_count || 0) - ((routes[a] || {}).request_count || 0) || (a < b ? -1 : 1);
    });
  }

  /** The *limit* busiest routes, each with only the fields the routes section reads. */
  function slimRoutes(routes, limit) {
    var out = {};
    rankRoutes(routes).slice(0, limit).forEach(function (path) {
      var stats = routes[path];
      if (!stats || typeof stats !== 'object') return;
      var slim = {};
      ROUTE_FIELDS.forEach(function (f) { if (f in stats) slim[f] = stats[f]; });
      out[path] = slim;
    });
    return out;
  }

  /** Append a live point unless it is not newer than the last one. Returns true if added. */
  function addPoint(points, point) {
    if (!point) return false;
    var last = points[points.length - 1];
    if (last && point.t <= last.t) return false;
    points.push(point);
    return true;
  }

  /**
   * Drop the oldest points in place so at most *maxPoints* remain and none is
   * more than *maxAgeMs* older than the newest.  Returns how many were dropped.
   */
  function capPoints(points, maxAgeMs, maxPoints) {
    if (!points.length) return 0;
    var cutoff = points[points.length - 1].t - maxAgeMs;
    var drop = Math.max(0, points.length - maxPoints);
    while (drop < points.length && points[drop].t < cutoff) drop++;
    if (drop) points.splice(0, drop);
    return drop;
  }

  /**
   * How far back live points are kept: the longest range button with data —
   * 2 h, or the embedded history's span when "All" reaches further.
   */
  function retentionMs(history) {
    var span = RANGES['2h'];
    Object.keys(history).forEach(function (name) {
      var pts = history[name];
      if (pts.length) span = Math.max(span, pts[pts.length - 1].t - pts[0].t);
    });
    return span;
  }

  /** [[t, value], ...] for one field, skipping points where it is absent. */
  function series(points, pick) {
    var out = [];
    points.forEach(function (p) {
      var v = pick(p);
      if (v !== null && v !== undefined) out.push([p.t, v]);
    });
    return out;
  }

  /** The gap beyond which consecutive points are treated as missing data. */
  function gapThreshold(points) {
    if (points.length < 3) return Infinity;
    var deltas = [];
    for (var i = 1; i < points.length; i++) deltas.push(points[i].t - points[i - 1].t);
    deltas.sort(function (a, b) { return a - b; });
    return Math.max(MIN_GAP_MS, deltas[Math.floor(deltas.length / 2)] * 3);
  }

  /** SVG path data for a series, starting a new subpath across gaps. */
  function pathD(data, xs, ys, maxGap) {
    var d = '';
    for (var i = 0; i < data.length; i++) {
      var move = i === 0 || data[i][0] - data[i - 1][0] > maxGap;
      d += (move ? 'M' : 'L') + xs(data[i][0]).toFixed(1) + ' ' + ys(data[i][1]).toFixed(1);
    }
    return d;
  }

  /** SVG path data filling each contiguous run of a series down to the baseline y0. */
  function areaD(data, xs, ys, maxGap, y0) {
    var d = '';
    var runStart = 0;
    for (var i = 0; i <= data.length; i++) {
      var breaks = i === data.length || (i > 0 && data[i][0] - data[i - 1][0] > maxGap);
      if (i > 0 && breaks) {
        var run = data.slice(runStart, i);
        d += 'M' + xs(run[0][0]).toFixed(1) + ' ' + y0.toFixed(1);
        run.forEach(function (pt) { d += 'L' + xs(pt[0]).toFixed(1) + ' ' + ys(pt[1]).toFixed(1); });
        d += 'L' + xs(run[run.length - 1][0]).toFixed(1) + ' ' + y0.toFixed(1) + 'Z';
        runStart = i;
      }
    }
    return d;
  }

  /** Status segments [{t0, t1, status}] clipped to [from, to]; a gap ends a segment. */
  function segments(points, from, to, maxGap) {
    var out = [];
    for (var i = 0; i < points.length; i++) {
      var p = points[i];
      var next = points[i + 1];
      var end = next ? Math.min(next.t, p.t + maxGap) : Math.min(to, p.t + maxGap);
      var s = Math.max(p.t, from);
      var e = Math.min(end, to);
      if (e <= s) continue;
      var prev = out[out.length - 1];
      // One rect per run of the same status: per-point rects show seams.
      if (prev && prev.status === p.status && prev.t1 >= s) prev.t1 = e;
      else out.push({ t0: s, t1: e, status: p.status });
    }
    return out;
  }

  /** Status changes [{t, from, to}] between consecutive points. */
  function transitions(points) {
    var out = [];
    for (var i = 1; i < points.length; i++) {
      if (points[i].status !== points[i - 1].status) {
        out.push({ t: points[i].t, from: points[i - 1].status, to: points[i].status });
      }
    }
    return out;
  }

  /** The [t, value] in *data* closest to time t, or null. */
  function nearest(data, t) {
    var best = null;
    for (var i = 0; i < data.length; i++) {
      if (!best || Math.abs(data[i][0] - t) < Math.abs(best[0] - t)) best = data[i];
    }
    return best;
  }

  /** How long the current status has held: the time since the last change (or the first point). */
  function heldSince(points) {
    if (!points.length) return null;
    var status = points[points.length - 1].status;
    var since = points[points.length - 1].t;
    for (var i = points.length - 1; i >= 0 && points[i].status === status; i--) since = points[i].t;
    return since;
  }

  function fmtDuration(ms) {
    var s = Math.max(0, Math.round(ms / 1000));
    if (s < 60) return s + ' s';
    var m = Math.round(s / 60);
    if (m < 60) return m + ' min';
    var h = Math.floor(m / 60);
    return h + ' h' + (m % 60 ? ' ' + (m % 60) + ' min' : '');
  }

  function fmtValue(unit, v) {
    if (v === null || v === undefined) return '—';
    if (v === 0) return unit === '%' ? '0%' : unit === 'rpm' ? '0/min' : unit === 'ms' ? '0 ms' : '0';
    if (unit === 'ms') return (v >= 100 ? v.toFixed(0) : v.toFixed(1)) + ' ms';
    if (unit === '%') return (v * 100).toFixed(v >= 0.1 ? 0 : 1) + '%';
    if (unit === 'rpm') return (v >= 100 ? v.toFixed(0) : v.toFixed(1)) + '/min';
    return String(v);
  }

  function fmtClock(t) {
    var d = new Date(t);
    var pad = function (n) { return (n < 10 ? '0' : '') + n; };
    return pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  /** Routes ranked by request volume in the latest point that has a breakdown. */
  function topRoutes(points, limit) {
    for (var i = points.length - 1; i >= 0; i--) {
      if (points[i].routes) return rankRoutes(points[i].routes).slice(0, limit);
    }
    return [];
  }

  /**
   * Wait before retry number *attempt* (1, 2, ...): exponential backoff with
   * jitter, so tabs refused together (a restart, a full stream limit) do not
   * all come back at once.  *rand* is a number in [0, 1).
   */
  function retryDelayMs(attempt, rand) {
    var cap = Math.min(RETRY_MAX_MS, RETRY_BASE_MS * Math.pow(2, Math.max(0, attempt - 1)));
    return cap / 2 + rand * cap / 2;
  }

  /** True if the stream URL carries a token (``<expiry>.<sig>``) that has expired by *now* (ms). */
  function tokenExpired(url, now) {
    var m = /[?&]token=(\d+)\./.exec(url || '');
    return m !== null && now >= parseInt(m[1], 10) * 1000;
  }

  /**
   * The endpoint that mints a fresh stream URL for *url*'s dashboard, or null
   * when *url* carries no token (no auth: nothing to renew).
   */
  function renewUrl(url) {
    if (!/[?&]token=/.test(url || '')) return null;
    var m = /^(.*)\/status\/stream\?/.exec(url);
    return m ? m[1] + '/dashboard/stream-url' : null;
  }

  /** The newest point time across *history*, or -Infinity. */
  function newestTime(history) {
    var newest = -Infinity;
    Object.keys(history).forEach(function (name) {
      var pts = history[name];
      if (pts.length) newest = Math.max(newest, pts[pts.length - 1].t);
    });
    return newest;
  }

  /** The header sentence for a set of current results. */
  function statusSentence(probes, history, now) {
    var failing = probes.filter(function (p) { return p.critical !== false && p.status === 'unhealthy'; });
    var degraded = probes.filter(function (p) { return p.critical !== false && p.status === 'degraded'; });
    var pick = failing.length ? failing : degraded;
    if (!pick.length) {
      return probes.length ? 'All ' + probes.length + ' probe' + (probes.length !== 1 ? 's' : '') + ' healthy' : 'No probes registered';
    }
    var word = failing.length ? 'unhealthy' : 'degraded';
    if (pick.length === 1) {
      var since = heldSince(history[pick[0].name] || []);
      return pick[0].name + ' ' + word + (since !== null ? ' for ' + fmtDuration(now - since) : '');
    }
    return pick.length + ' probes ' + word + ': ' + pick.map(function (p) { return p.name; }).join(', ');
  }

  var api = {
    niceCeil: niceCeil, scale: scale, pointsFor: pointsFor, addPoint: addPoint, capPoints: capPoints,
    retentionMs: retentionMs, slimRoutes: slimRoutes, series: series,
    gapThreshold: gapThreshold, pathD: pathD, areaD: areaD, segments: segments, transitions: transitions,
    nearest: nearest, heldSince: heldSince, fmtDuration: fmtDuration, fmtValue: fmtValue,
    topRoutes: topRoutes, statusSentence: statusSentence, retryDelayMs: retryDelayMs,
    tokenExpired: tokenExpired, renewUrl: renewUrl, newestTime: newestTime, RANGES: RANGES, MAX_POINTS: MAX_POINTS,
    TOP_ROUTES: TOP_ROUTES, RETRY_MAX_MS: RETRY_MAX_MS,
  };
  if (typeof module !== 'undefined' && module.exports) { module.exports = api; return; }

  // ── Page ──────────────────────────────────────────────────────────────

  var doc = root.document;
  // The server's URL asks for ?view=dashboard, so events arrive with routes
  // already cut to TOP_ROUTES; pointsFor() still slims whatever it is given.
  var streamUrl = doc.body.getAttribute('data-stream-url');
  var SVGNS = 'http://www.w3.org/2000/svg';

  var dataEl = doc.getElementById('fw-data');
  var initial = {};
  try { initial = JSON.parse(dataEl ? dataEl.textContent : '{}') || {}; } catch (_) { initial = {}; }
  var history = {};
  Object.keys(initial.history || {}).forEach(function (name) { history[name] = pointsFor(initial.history[name]); });
  var retainMs = retentionMs(history);
  Object.keys(history).forEach(function (name) { capPoints(history[name], retainMs, MAX_POINTS); });

  var range = '1h';
  try { var saved = root.localStorage.getItem('fastapi-watch-range'); if (saved in RANGES) range = saved; } catch (_) {}

  var header = doc.querySelector('.header');
  var badge = doc.getElementById('status-badge-text');
  var subtitle = doc.getElementById('status-subtitle');
  var checkedEl = doc.getElementById('checked-at');
  var liveDot = doc.getElementById('live-dot');
  var tooltip = doc.getElementById('chart-tooltip');

  function svgEl(tag, attrs, parent) {
    var el = doc.createElementNS(SVGNS, tag);
    Object.keys(attrs || {}).forEach(function (k) { el.setAttribute(k, attrs[k]); });
    if (parent) parent.appendChild(el);
    return el;
  }

  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }

  // The server's clock stamps the points; a browser clock running behind it
  // must not push the newest ones off the right edge of every chart.
  function nowMs() { return Math.max(Date.now(), newestTime(history)); }

  function timeWindow() {
    var now = nowMs();
    var span = RANGES[range];
    if (span) return [now - span, now];
    var first = now;
    Object.keys(history).forEach(function (n) { if (history[n].length) first = Math.min(first, history[n][0].t); });
    return [Math.min(first, now - 60e3), now];
  }

  function inWindow(points, from) {
    return points.filter(function (p) { return p.t >= from; });
  }

  /** Tick times every nice interval across [from, to]. */
  function timeTicks(from, to, count) {
    var steps = [60e3, 2 * 60e3, 5 * 60e3, 10 * 60e3, 15 * 60e3, 30 * 60e3, 60 * 60e3, 2 * 60 * 60e3, 6 * 60 * 60e3];
    var step = steps[steps.length - 1];
    for (var i = 0; i < steps.length; i++) { if ((to - from) / steps[i] <= count) { step = steps[i]; break; } }
    var ticks = [];
    for (var t = Math.ceil(from / step) * step; t <= to; t += step) ticks.push(t);
    return ticks;
  }

  // Each plot remembers its geometry so the shared crosshair can find it.
  var plots = [];

  function registerPlot(svg, xs, from, to, width, height, lines) {
    var hair = svgEl('line', { class: 'xhair', y1: 0, y2: height, visibility: 'hidden' }, svg);
    plots.push({ svg: svg, xs: xs, from: from, to: to, width: width, hair: hair, lines: lines || [] });
  }

  function drawAxis(svg, from, to) {
    clear(svg);
    var width = svg.clientWidth || 600;
    var xs = scale(from, to, PLOT_LEFT, width - PLOT_RIGHT);
    timeTicks(from, to, Math.max(2, Math.floor(width / 110))).forEach(function (t) {
      var label = svgEl('text', { class: 'axis-label', x: xs(t), y: 12, 'text-anchor': 'middle' }, svg);
      label.textContent = fmtClock(t);
    });
    registerPlot(svg, xs, from, to, width, 20);
  }

  function drawLane(svg, points, from, to) {
    clear(svg);
    var width = svg.clientWidth || 600;
    var height = 20;
    var xs = scale(from, to, PLOT_LEFT, width - PLOT_RIGHT);
    svgEl('rect', { class: 'seg-gap', x: PLOT_LEFT, y: 4, width: Math.max(0, width - PLOT_LEFT - PLOT_RIGHT), height: height - 8, rx: 2 }, svg);
    var maxGap = gapThreshold(points);
    segments(points, from, to, maxGap).forEach(function (s) {
      var rect = svgEl('rect', { class: 'seg-' + s.status, x: xs(s.t0), y: 4, width: Math.max(1, xs(s.t1) - xs(s.t0)), height: height - 8 }, svg);
      var title = svgEl('title', {}, rect);
      title.textContent = s.status + ' ' + fmtClock(s.t0) + '–' + fmtClock(s.t1);
    });
    transitions(points).forEach(function (tr) {
      if (tr.t < from || tr.t > to) return;
      var x = xs(tr.t);
      var mark = svgEl('line', { class: 'tick-mark', x1: x, x2: x, y1: 0, y2: height }, svg);
      var title = svgEl('title', {}, mark);
      title.textContent = tr.from + ' → ' + tr.to + ' at ' + fmtClock(tr.t);
    });
    registerPlot(svg, xs, from, to, width, height);
  }

  /**
   * Draw a time-series chart into *svg*.  cfg.lines: [{key, label, data}],
   * cfg.unit, cfg.area (fill under the first line), cfg.thresholds: [{key, value}].
   */
  function drawChart(svg, cfg, from, to, maxGap) {
    clear(svg);
    var width = svg.clientWidth || 600;
    var height = svg.clientHeight || 120;
    var plotBottom = height - PLOT_BOTTOM;
    var top = 0;
    cfg.lines.forEach(function (l) { l.data.forEach(function (d) { if (d[0] >= from) top = Math.max(top, d[1]); }); });
    (cfg.thresholds || []).forEach(function (th) { top = Math.max(top, th.value); });
    if (cfg.unit === '%') top = Math.min(1, niceCeil(Math.max(top, 0.01)));
    else top = niceCeil(top);
    var xs = scale(from, to, PLOT_LEFT, width - PLOT_RIGHT);
    var ys = scale(0, top, plotBottom, 4);

    [0, 0.5, 1].forEach(function (f) {
      var v = top * f;
      svgEl('line', { class: 'grid-line', x1: PLOT_LEFT, x2: width - PLOT_RIGHT, y1: ys(v), y2: ys(v) }, svg);
      var label = svgEl('text', { class: 'axis-label', x: PLOT_LEFT - 6, y: ys(v) + 3, 'text-anchor': 'end' }, svg);
      label.textContent = fmtValue(cfg.unit, v);
    });
    timeTicks(from, to, Math.max(2, Math.floor(width / 110))).forEach(function (t) {
      var label = svgEl('text', { class: 'axis-label', x: xs(t), y: height - 3, 'text-anchor': 'middle' }, svg);
      label.textContent = fmtClock(t);
    });
    (cfg.thresholds || []).forEach(function (th) {
      svgEl('line', { class: 'threshold threshold-' + th.key, x1: PLOT_LEFT, x2: width - PLOT_RIGHT, y1: ys(th.value), y2: ys(th.value) }, svg);
    });
    cfg.lines.forEach(function (l, i) {
      var data = l.data.filter(function (d) { return d[0] >= from - maxGap; });
      if (!data.length) return;
      if (cfg.area && i === 0) {
        svgEl('path', { class: 'area-' + l.key, d: areaD(data, xs, ys, maxGap, ys(0)) }, svg);
      }
      svgEl('path', { class: 'line line-' + l.key, d: pathD(data, xs, ys, maxGap) }, svg);
    });
    // Direct labels at each line's right end (2–4 series), nudged apart so they
    // never overlap; identity then never rests on color alone.
    if (cfg.lines.length >= 2 && cfg.lines.length <= 4) {
      var ends = cfg.lines.map(function (l) {
        var last = l.data[l.data.length - 1];
        return last ? { label: l.label, y: ys(last[1]) } : null;
      }).filter(Boolean).sort(function (a, b) { return a.y - b.y; });
      for (var k = 1; k < ends.length; k++) ends[k].y = Math.max(ends[k].y, ends[k - 1].y + 11);
      var overflow = ends.length ? ends[ends.length - 1].y - plotBottom : 0;
      if (overflow > 0) ends.forEach(function (end) { end.y -= overflow; });
      ends.forEach(function (end) {
        var text = svgEl('text', { class: 'direct-label', x: width - PLOT_RIGHT + 5, y: end.y + 3 }, svg);
        text.textContent = end.label;
      });
    }
    var lastValues = cfg.lines.map(function (l) {
      var last = l.data[l.data.length - 1];
      return l.label + ' ' + fmtValue(cfg.unit, last ? last[1] : null);
    });
    svg.setAttribute('aria-label', cfg.title + ': ' + lastValues.join(', '));
    registerPlot(svg, xs, from, to, width, plotBottom, cfg.lines.map(function (l) {
      return { label: l.label, data: l.data, unit: cfg.unit };
    }));
  }

  function legend(figcaption, title, lines) {
    clear(figcaption);
    var label = doc.createElement('span');
    label.textContent = title;
    figcaption.appendChild(label);
    if (lines.length < 2) return;
    lines.forEach(function (l) {
      var key = doc.createElement('span');
      key.className = 'legend-key legend-' + l.key;
      key.textContent = l.label;
      figcaption.appendChild(key);
    });
  }

  function chartSpecs(points) {
    var specs = [];
    var pcts = ['p50', 'p95', 'p99'].map(function (k) {
      return { key: k, label: k, data: series(points, function (p) { return p[k]; }) };
    }).filter(function (l) { return l.data.length; });
    var latest = points[points.length - 1] || {};
    if (points.some(function (p) { return p.lag !== null; })) {
      var thresholds = [];
      if (latest.warnMs !== null && latest.warnMs !== undefined) thresholds.push({ key: 'warn', value: latest.warnMs });
      if (latest.failMs !== null && latest.failMs !== undefined) thresholds.push({ key: 'fail', value: latest.failMs });
      specs.push({ id: 'latency', title: 'Event loop lag', unit: 'ms', thresholds: thresholds,
        lines: [{ key: 'latency', label: 'lag', data: series(points, function (p) { return p.lag; }) }] });
    } else if (pcts.length) {
      specs.push({ id: 'latency', title: 'Latency', unit: 'ms', lines: pcts });
    } else {
      var lat = series(points, function (p) { return p.latency; });
      if (lat.some(function (d) { return d[1] > 0; })) {
        specs.push({ id: 'latency', title: 'Latency', unit: 'ms', lines: [{ key: 'latency', label: 'latency', data: lat }] });
      }
    }
    var errors = series(points, function (p) { return p.errorRate; });
    if (errors.length) specs.push({ id: 'errors', title: 'Error rate', unit: '%', area: true, lines: [{ key: 'error', label: 'errors', data: errors }] });
    var rpm = series(points, function (p) { return p.rpm; });
    if (rpm.length) specs.push({ id: 'throughput', title: 'Throughput', unit: 'rpm', lines: [{ key: 'rpm', label: 'requests', data: rpm }] });
    return specs;
  }

  function drawProbeCharts(card, points, from, to) {
    var holder = card.querySelector('.charts');
    if (!holder) return;
    clear(holder);
    var windowed = inWindow(points, from - gapThreshold(points));
    var specs = chartSpecs(windowed);
    if (!specs.length) {
      var empty = doc.createElement('p');
      empty.className = 'charts-empty';
      empty.textContent = windowed.length ? 'Nothing to chart for this probe.' : 'No results in this time range yet.';
      holder.appendChild(empty);
      return;
    }
    var maxGap = gapThreshold(points);
    specs.forEach(function (spec) {
      var fig = doc.createElement('figure');
      fig.className = 'chart';
      var cap = doc.createElement('figcaption');
      legend(cap, spec.title, spec.lines);
      fig.appendChild(cap);
      var svg = svgEl('svg', { role: 'img' });
      fig.appendChild(svg);
      holder.appendChild(fig);
      drawChart(svg, spec, from, to, maxGap);
    });
  }

  function drawRoutes(from, to) {
    var section = doc.getElementById('routes');
    if (!section) return;
    clear(section);
    var source = null;
    Object.keys(history).forEach(function (name) {
      if (!source && history[name].some(function (p) { return p.routes; })) source = name;
    });
    if (!source) { section.hidden = true; return; }
    section.hidden = false;
    var points = history[source];
    var heading = doc.createElement('h2');
    heading.className = 'section-title';
    heading.textContent = 'Routes by traffic (' + source + ')';
    section.appendChild(heading);
    var maxGap = gapThreshold(points);
    topRoutes(points, 8).forEach(function (path) {
      var row = doc.createElement('div');
      row.className = 'board-row';
      var gutter = doc.createElement('div');
      gutter.className = 'gutter';
      var name = doc.createElement('div');
      name.className = 'route-path';
      name.textContent = path;
      var stats = doc.createElement('div');
      stats.className = 'route-stats';
      var now = (points[points.length - 1].routes || {})[path] || {};
      stats.textContent = fmtValue('ms', num(now.p95_rtt_ms)) + ' p95 · ' + fmtValue('%', num(now.error_rate)) + ' errors · ' + fmtValue('rpm', num(now.requests_per_minute));
      gutter.appendChild(name);
      gutter.appendChild(stats);
      var plot = doc.createElement('div');
      plot.className = 'plot';
      var svg = svgEl('svg', { role: 'img' });
      svg.style.height = '4.5rem';
      plot.appendChild(svg);
      row.appendChild(gutter);
      row.appendChild(plot);
      section.appendChild(row);
      var pick = function (field) { return function (p) { var r = (p.routes || {})[path]; return r ? num(r[field]) : null; }; };
      drawChart(svg, { title: path, unit: 'ms', lines: [{ key: 'latency', label: 'p95', data: series(points, pick('p95_rtt_ms')) }] }, from, to, maxGap);
    });
  }

  var scheduled = false;
  function render() {
    scheduled = false;
    plots = [];
    var win = timeWindow();
    var from = win[0];
    var to = win[1];
    doc.querySelectorAll('.axis-svg').forEach(function (svg) { drawAxis(svg, from, to); });
    doc.querySelectorAll('.lane').forEach(function (lane) {
      var svg = lane.querySelector('.lane-svg');
      if (svg) drawLane(svg, history[lane.getAttribute('data-probe')] || [], from, to);
    });
    doc.querySelectorAll('.probe-card').forEach(function (card) {
      if (card.style.display === 'none') return;
      drawProbeCharts(card, history[card.getAttribute('data-probe')] || [], from, to);
    });
    drawRoutes(from, to);
  }
  function scheduleRender() {
    if (scheduled) return;
    scheduled = true;
    (root.requestAnimationFrame || setTimeout)(render);
  }

  // ── Shared crosshair ──────────────────────────────────────────────────

  function hideCrosshair() {
    plots.forEach(function (p) { p.hair.setAttribute('visibility', 'hidden'); });
    if (tooltip) tooltip.hidden = true;
  }

  doc.addEventListener('pointermove', function (e) {
    var target = null;
    for (var i = 0; i < plots.length; i++) {
      if (plots[i].svg.contains(e.target)) { target = plots[i]; break; }
    }
    if (!target) { hideCrosshair(); return; }
    var box = target.svg.getBoundingClientRect();
    var x = e.clientX - box.left;
    if (x < PLOT_LEFT || x > target.width - PLOT_RIGHT) { hideCrosshair(); return; }
    var t = target.from + ((x - PLOT_LEFT) / Math.max(1, target.width - PLOT_LEFT - PLOT_RIGHT)) * (target.to - target.from);
    plots.forEach(function (p) {
      var px = p.xs(t);
      p.hair.setAttribute('x1', px);
      p.hair.setAttribute('x2', px);
      p.hair.setAttribute('visibility', 'visible');
    });
    if (!tooltip) return;
    clear(tooltip);
    var when = doc.createElement('div');
    when.className = 'tt-time';
    when.textContent = new Date(t).toLocaleTimeString();
    tooltip.appendChild(when);
    target.lines.forEach(function (l) {
      var hit = nearest(l.data, t);
      var row = doc.createElement('div');
      row.textContent = l.label + ': ' + fmtValue(l.unit, hit ? hit[1] : null);
      tooltip.appendChild(row);
    });
    tooltip.hidden = target.lines.length === 0;
    tooltip.style.left = Math.min(e.clientX + 14, root.innerWidth - 220) + 'px';
    tooltip.style.top = (e.clientY + 14) + 'px';
  });
  doc.addEventListener('pointerleave', hideCrosshair);

  // ── Range, filters, tooltips ──────────────────────────────────────────

  doc.querySelectorAll('.range button').forEach(function (btn) {
    btn.setAttribute('aria-pressed', String(btn.getAttribute('data-range') === range));
    btn.addEventListener('click', function () {
      range = btn.getAttribute('data-range');
      try { root.localStorage.setItem('fastapi-watch-range', range); } catch (_) {}
      doc.querySelectorAll('.range button').forEach(function (b) {
        b.setAttribute('aria-pressed', String(b === btn));
      });
      render();
    });
  });

  var activeTags = new Set();
  var searchQuery = '';
  function applyFilters() {
    doc.querySelectorAll('.probe-card, .lane').forEach(function (card) {
      var tags = (card.getAttribute('data-tags') || '').split(',').filter(Boolean);
      var tagOk = activeTags.size === 0 || tags.some(function (t) { return activeTags.has(t); });
      var searchOk = !searchQuery || (card.getAttribute('data-name') || '').toLowerCase().indexOf(searchQuery) !== -1;
      card.style.display = tagOk && searchOk ? '' : 'none';
    });
    render();
  }
  var tagFilterEl = doc.getElementById('tag-filter');
  if (tagFilterEl) {
    var clearBtn = doc.getElementById('tag-filter-clear');
    var syncClear = function () { if (clearBtn) clearBtn.className = activeTags.size ? 'tag-filter-clear visible' : 'tag-filter-clear'; };
    tagFilterEl.addEventListener('click', function (e) {
      var btn = e.target.closest('.tag-filter-btn');
      if (!btn) return;
      var tag = btn.getAttribute('data-tag');
      if (activeTags.has(tag)) { activeTags.delete(tag); btn.classList.remove('active'); }
      else { activeTags.add(tag); btn.classList.add('active'); }
      applyFilters();
      syncClear();
    });
    if (clearBtn) clearBtn.addEventListener('click', function () {
      activeTags.clear();
      tagFilterEl.querySelectorAll('.tag-filter-btn').forEach(function (b) { b.classList.remove('active'); });
      applyFilters();
      syncClear();
    });
  }
  var searchEl = doc.getElementById('probe-search');
  if (searchEl) searchEl.addEventListener('input', function () {
    searchQuery = searchEl.value.trim().toLowerCase();
    applyFilters();
  });

  var errorPopup = doc.createElement('div');
  errorPopup.className = 'error-tooltip-popup';
  doc.body.appendChild(errorPopup);
  doc.addEventListener('click', function (e) {
    var btn = e.target.closest && e.target.closest('.error-count-btn');
    if (!btn) { errorPopup.style.display = 'none'; return; }
    errorPopup.textContent = btn.getAttribute('data-error') || '(no error message)';
    errorPopup.style.display = 'block';
    var r = btn.getBoundingClientRect();
    errorPopup.style.top = (r.bottom + 8) + 'px';
    errorPopup.style.left = Math.max(8, Math.min(r.left, root.innerWidth - 440)) + 'px';
    e.stopPropagation();
  });
  doc.addEventListener('keydown', function (e) { if (e.key === 'Escape') errorPopup.style.display = 'none'; });

  // ── Live updates ──────────────────────────────────────────────────────

  function statusLabel(s) { return s === 'healthy' ? 'Healthy' : s === 'degraded' ? 'Degraded' : 'Unhealthy'; }

  function fmtDetail(key, val) {
    if (val === null || val === undefined) return null;
    if (key === 'error_rate') return fmtValue('%', val);
    if (key.slice(-3) === '_ms' && typeof val === 'number') return val.toFixed(2) + ' ms';
    if (typeof val === 'boolean') return val ? 'yes' : 'no';
    if (typeof val === 'number') return val.toFixed(4).replace(/\.?0+$/, '');
    if (key === 'circuit_breaker' && typeof val === 'object') {
      var trips = val.trips_total || 0;
      var tripStr = trips ? ', ' + trips + ' trip' + (trips !== 1 ? 's' : '') + ' total' : '';
      return val.open ? 'open — suspended (' + (val.consecutive_failures || 0) + ' consecutive failures' + tripStr + ')' : 'closed' + tripStr;
    }
    if ((key === 'status_distribution' || key === 'error_types' || key === 'threshold_errors') && typeof val === 'object') {
      return Object.keys(val).sort().map(function (k) { return k + ': ' + val[k]; }).join(', ');
    }
    if (typeof val === 'object') return key === 'routes' ? Object.keys(val).length + ' routes (see chart below)' : '—';
    return String(val);
  }

  function updateCard(probe) {
    // Escaped: a name with a quote or backslash would otherwise make an
    // invalid selector, and the throw would stop this and every later update.
    var sel = '[data-probe="' + root.CSS.escape(String(probe.name)) + '"]';
    var card = doc.querySelector('.probe-card' + sel);
    var lane = doc.querySelector('.lane' + sel);
    if (lane) {
      var chip = lane.querySelector('.status-chip');
      if (chip) chip.className = 'status-chip ' + probe.status;
    }
    if (!card) return;
    card.className = 'probe-card ' + probe.status + ' board-row';
    var b = card.querySelector('.badge-status');
    if (b) { b.textContent = statusLabel(probe.status); b.className = 'badge badge-status badge-' + probe.status; }
    var lat = card.querySelector('.probe-latency');
    if (lat) lat.textContent = probe.latency_ms ? probe.latency_ms.toFixed(2) + ' ms' : '—';
    var err = card.querySelector('.probe-error');
    if (err) { err.textContent = probe.error || ''; err.style.display = probe.error ? '' : 'none'; }
    var d = probe.details || {};
    var ts = card.querySelector('.probe-timestamps');
    if (ts) {
      var parts = [];
      if (d.last_error_at) parts.push('last error: ' + String(d.last_error_at).slice(0, 19).replace('T', ' ') + ' UTC');
      if (d.last_success_at) parts.push('last success: ' + String(d.last_success_at).slice(0, 19).replace('T', ' ') + ' UTC');
      ts.textContent = parts.join(' • ');
      ts.style.display = parts.length ? '' : 'none';
    }
    var tbody = card.querySelector('.details-table tbody');
    if (tbody) {
      clear(tbody);
      Object.keys(d).forEach(function (key) {
        var shown = fmtDetail(key, d[key]);
        if (shown === null || key === 'last_error' || key === 'last_error_at' || key === 'last_success_at') return;
        var tr = doc.createElement('tr');
        var k = doc.createElement('td');
        if (key === 'error_count' && d[key] > 0 && d.last_error) {
          var btn = doc.createElement('span');
          btn.className = 'error-count-btn';
          btn.tabIndex = 0;
          btn.setAttribute('data-error', d.last_error);
          btn.textContent = 'Error Count ▸';
          k.appendChild(btn);
        } else {
          k.textContent = key.replace(/_/g, ' ').replace(/\b\w/g, function (c) { return c.toUpperCase(); });
        }
        var v = doc.createElement('td');
        v.textContent = shown;
        tr.appendChild(k);
        tr.appendChild(v);
        tbody.appendChild(tr);
      });
    }
  }

  function summaryText(probes) {
    var total = probes.length, healthy = 0, degraded = 0;
    probes.forEach(function (p) { if (p.status === 'healthy') healthy++; else if (p.status === 'degraded') degraded++; });
    var unhealthy = total - healthy - degraded;
    var noun = 'probe' + (total !== 1 ? 's' : '');
    if (degraded || unhealthy) return healthy + ' healthy, ' + degraded + ' degraded, ' + unhealthy + ' unhealthy / ' + total + ' ' + noun;
    return healthy + ' / ' + total + ' ' + noun + ' healthy';
  }

  function stored(key) {
    try { return parseInt(root.sessionStorage.getItem(key) || '0', 10) || 0; } catch (_) { return 0; }
  }
  function store(key, value) {
    try { root.sessionStorage.setItem(key, String(value)); } catch (_) {}
  }

  // Cards are server-rendered; when the set of probes changes, reload to pick
  // it up — at most once per 30 s (or *minDelay*), deferring rather than
  // dropping a request.  Each load renders the whole page on the server.
  var reloadPending = false;
  function reloadPage(minDelay) {
    if (reloadPending) return;
    reloadPending = true;
    var key = 'fastapi-watch-reloaded-at';
    setTimeout(function () {
      store(key, Date.now());
      root.location.reload();
    }, Math.max(minDelay || 0, 30000 - (Date.now() - stored(key))));
  }

  function probeSetChanged(probes) {
    var shown = Array.prototype.map.call(doc.querySelectorAll('.probe-card'), function (c) {
      return c.getAttribute('data-probe');
    }).sort().join('\n');
    return shown !== probes.map(function (p) { return p.name; }).sort().join('\n');
  }

  function applyReport(report) {
    var probes = report.probes || [];
    probes.forEach(function (p) {
      var points = history[p.name] = history[p.name] || [];
      if (addPoint(points, pointsFor([p])[0])) capPoints(points, retainMs, MAX_POINTS);
    });
    if (header) header.className = 'header ' + report.status;
    if (badge) badge.textContent = statusSentence(probes, history, nowMs());
    if (subtitle) subtitle.textContent = summaryText(probes);
    if (checkedEl && report.checked_at) {
      checkedEl.textContent = 'Last checked ' + report.checked_at.replace('T', ' ').slice(0, 19) + ' ' + (report.timezone || 'UTC');
    }
    if (probeSetChanged(probes)) reloadPage();
    probes.forEach(updateCard);
    scheduleRender();
  }

  if (badge) badge.textContent = statusSentence(initial.probes || [], history, nowMs());
  render();
  if (root.ResizeObserver) {
    var main = doc.querySelector('.content');
    if (main) new root.ResizeObserver(scheduleRender).observe(main);
  } else {
    root.addEventListener('resize', scheduleRender);
  }
  // Keep the time axis moving even between stream events.
  setInterval(scheduleRender, 15000);

  // Refused connections in a row, kept across reloads so a server that keeps
  // refusing (503 at max_stream_connections, a proxy error page) is retried
  // ever more slowly instead of re-rendering this page every 30 s.
  var FAILURES_KEY = 'fastapi-watch-stream-failures';
  var hasToken = /[?&]token=/.test(streamUrl || '');

  // Swap an expired stream token for a fresh one without re-rendering the
  // page; reload only if that fails (credentials gone, an older server).
  function renewAndConnect(delay) {
    var url = renewUrl(streamUrl);
    if (!url || !root.fetch) { reloadPage(delay); return; }
    root.fetch(url, { credentials: 'same-origin', cache: 'no-store' })
      .then(function (resp) { return resp.ok ? resp.json() : null; })
      .then(function (body) {
        if (!body || typeof body.stream_url !== 'string') throw new Error('no stream_url');
        streamUrl = body.stream_url;
        setTimeout(connect, delay);
      })
      .catch(function () { reloadPage(delay); });
  }

  function connect() {
    var source = new root.EventSource(streamUrl);
    source.onopen = function () {
      store(FAILURES_KEY, 0);
      if (liveDot) liveDot.className = 'live-dot connected';
    };
    source.onmessage = function (e) {
      try { applyReport(JSON.parse(e.data)); } catch (_) {}
    };
    source.onerror = function () {
      if (liveDot) liveDot.className = 'live-dot';
      // CONNECTING: the browser retries by itself.  CLOSED: it gave up on a
      // non-200 — an expired stream token, which a fresh one replaces, or a
      // refusal that a later retry of the stream alone can get past.
      if (source.readyState !== 2) return;
      var failures = stored(FAILURES_KEY) + 1;
      store(FAILURES_KEY, failures);
      var delay = retryDelayMs(failures, Math.random());
      // A token that keeps being refused though unexpired here (this clock
      // runs behind the server's) is replaced too.
      var stale = tokenExpired(streamUrl, Date.now()) || (hasToken && failures >= MAX_STREAM_RETRIES);
      if (stale) renewAndConnect(delay);
      else setTimeout(connect, delay);
    };
  }
  if (streamUrl) connect();
})(this);
