// Unit tests for the dashboard's pure helpers.  Run by tests/test_dashboard_js.py
// with `node`; exits non-zero on the first failure.
const assert = require('assert');
const path = require('path');
const h = require(path.join(__dirname, '..', '..', 'src', 'fastapi_watch', 'static', 'dashboard.js'));

const tests = {
  niceCeil() {
    assert.strictEqual(h.niceCeil(0), 1);
    assert.strictEqual(h.niceCeil(-5), 1);
    assert.strictEqual(h.niceCeil(7), 10);
    assert.strictEqual(h.niceCeil(180), 200);
    assert.strictEqual(h.niceCeil(230), 250);
    assert.strictEqual(h.niceCeil(0.03), 0.05);
  },

  scaleMapsAndHandlesZeroWidthDomains() {
    const s = h.scale(0, 10, 100, 200);
    assert.strictEqual(s(5), 150);
    assert.strictEqual(h.scale(3, 3, 7, 9)(3), 7);
  },

  pointsForSortsDedupesAndSkipsUntimed() {
    const pts = h.pointsFor([
      { checked_at: '2026-01-01T00:00:10Z', status: 'healthy', latency_ms: 5, details: { p95_rtt_ms: 9 } },
      { checked_at: '2026-01-01T00:00:00Z', status: 'unhealthy', latency_ms: 0 },
      { status: 'healthy' },
      { checked_at: '2026-01-01T00:00:10Z', status: 'degraded', latency_ms: 6 },
    ]);
    assert.deepStrictEqual(pts.map((p) => p.status), ['unhealthy', 'degraded']);
    assert.strictEqual(pts[0].latency, null, 'latency 0 means not measured');
    assert.strictEqual(pts[1].latency, 6);
  },

  addPointRejectsOlderOrEqual() {
    const pts = [{ t: 10 }];
    assert.strictEqual(h.addPoint(pts, { t: 10 }), false);
    assert.strictEqual(h.addPoint(pts, { t: 5 }), false);
    assert.strictEqual(h.addPoint(pts, { t: 11 }), true);
    assert.strictEqual(pts.length, 2);
  },

  pathDBreaksAcrossGaps() {
    const id = (v) => v;
    const d = h.pathD([[0, 1], [1, 2], [10, 3]], id, id, 5);
    assert.strictEqual(d, 'M0.0 1.0L1.0 2.0M10.0 3.0');
  },

  areaDClosesEachRun() {
    const id = (v) => v;
    const d = h.areaD([[0, 1], [1, 2], [10, 3]], id, id, 5, 0);
    assert.strictEqual((d.match(/Z/g) || []).length, 2);
    assert.ok(d.startsWith('M0.0 0.0L0.0 1.0'));
  },

  segmentsMergeRunsAndClip() {
    const pts = [
      { t: 0, status: 'healthy' }, { t: 10, status: 'healthy' },
      { t: 20, status: 'unhealthy' }, { t: 30, status: 'unhealthy' },
    ];
    const segs = h.segments(pts, 5, 35, 100);
    assert.deepStrictEqual(segs, [
      { t0: 5, t1: 20, status: 'healthy' },
      { t0: 20, t1: 35, status: 'unhealthy' },
    ]);
  },

  segmentsLeaveGapsEmpty() {
    const pts = [{ t: 0, status: 'healthy' }, { t: 100, status: 'healthy' }];
    const segs = h.segments(pts, 0, 110, 10);
    assert.deepStrictEqual(segs, [
      { t0: 0, t1: 10, status: 'healthy' },
      { t0: 100, t1: 110, status: 'healthy' },
    ]);
  },

  gapThresholdUsesMedianSpacing() {
    const pts = [0, 30e3, 60e3, 90e3].map((t) => ({ t }));
    assert.strictEqual(h.gapThreshold(pts), 90e3);
    assert.strictEqual(h.gapThreshold([{ t: 0 }]), Infinity);
  },

  transitionsAndHeldSince() {
    const pts = [
      { t: 0, status: 'healthy' }, { t: 10, status: 'unhealthy' }, { t: 20, status: 'unhealthy' },
    ];
    assert.deepStrictEqual(h.transitions(pts), [{ t: 10, from: 'healthy', to: 'unhealthy' }]);
    assert.strictEqual(h.heldSince(pts), 10);
    assert.strictEqual(h.heldSince([]), null);
  },

  nearestPicksClosest() {
    assert.deepStrictEqual(h.nearest([[0, 1], [10, 2], [20, 3]], 14), [10, 2]);
    assert.strictEqual(h.nearest([], 5), null);
  },

  formatting() {
    assert.strictEqual(h.fmtDuration(45e3), '45 s');
    assert.strictEqual(h.fmtDuration(8 * 60e3), '8 min');
    assert.strictEqual(h.fmtDuration(125 * 60e3), '2 h 5 min');
    assert.strictEqual(h.fmtValue('ms', 0), '0 ms');
    assert.strictEqual(h.fmtValue('ms', 12.34), '12.3 ms');
    assert.strictEqual(h.fmtValue('%', 0.25), '25%');
    assert.strictEqual(h.fmtValue('%', 0.004), '0.4%');
    assert.strictEqual(h.fmtValue('rpm', 420), '420/min');
    assert.strictEqual(h.fmtValue('ms', null), '—');
  },

  topRoutesByVolumeFromLatestBreakdown() {
    const pts = [
      { routes: { '/a': { request_count: 1 } } },
      { routes: { '/a': { request_count: 5 }, '/b': { request_count: 9 }, '/c': { request_count: 1 } } },
      { routes: null },
    ];
    assert.deepStrictEqual(h.topRoutes(pts, 2), ['/b', '/a']);
    assert.deepStrictEqual(h.topRoutes([{ routes: null }], 3), []);
  },

  statusSentence() {
    const now = 600e3;
    const history = { db: [{ t: 0, status: 'healthy' }, { t: 120e3, status: 'unhealthy' }, { t: 540e3, status: 'unhealthy' }] };
    assert.strictEqual(h.statusSentence([{ name: 'db', status: 'unhealthy' }], history, now), 'db unhealthy for 8 min');
    assert.strictEqual(
      h.statusSentence([{ name: 'a', status: 'unhealthy' }, { name: 'b', status: 'unhealthy' }], {}, now),
      '2 probes unhealthy: a, b',
    );
    assert.strictEqual(h.statusSentence([{ name: 'a', status: 'healthy' }], {}, now), 'All 1 probe healthy');
    assert.strictEqual(
      h.statusSentence([{ name: 'opt', status: 'unhealthy', critical: false }], {}, now),
      'All 1 probe healthy',
    );
    assert.strictEqual(h.statusSentence([], {}, now), 'No probes registered');
  },
};

let failed = 0;
for (const [name, fn] of Object.entries(tests)) {
  try {
    fn();
    console.log('ok  ' + name);
  } catch (err) {
    failed++;
    console.log('FAIL ' + name + ': ' + err.message);
  }
}
process.exit(failed ? 1 : 0);
