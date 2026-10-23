# fastapi-watch — Full Documentation

This file contains the complete reference for fastapi-watch. For the condensed overview, see [README.md](README.md).

---

## Table of contents

- [Health dashboard](#health-dashboard)
- [Live streaming](#live-streaming)
- [Polling and caching](#polling-and-caching)
- [Per-probe poll frequency](#per-probe-poll-frequency)
- [Three-state health (DEGRADED)](#three-state-health-degraded)
- [Circuit breaker](#circuit-breaker)
- [Circuit breaker metrics](#circuit-breaker-metrics)
- [Maintenance mode](#maintenance-mode)
- [ProbeGroup — organizing probes across files](#probegroup--organizing-probes-across-files)
- [Startup probe](#startup-probe)
- [Startup grace period](#startup-grace-period)
- [State-change callbacks](#state-change-callbacks)
- [Probe result history](#probe-result-history)
- [Alert history](#alert-history)
- [Custom storage backends](#custom-storage-backends)
- [Response format](#response-format)
- [Writing a custom probe](#writing-a-custom-probe)
- [Route auto-discovery](#route-auto-discovery)
- [Built-in probes](#built-in-probes)
  - [App-wide request metrics](#app-wide-request-metrics)
  - [FastAPI route probe](#fastapi-route-probe)
  - [WebSocket probe](#websocket-probe)
  - [Event loop lag](#event-loop-lag)
  - [TCP / DNS reachability](#tcp--dns-reachability)
  - [SMTP](#smtp)
  - [Threshold wrapper](#threshold-wrapper)
  - [PostgreSQL](#postgresql)
  - [MySQL / MariaDB](#mysql--mariadb)
  - [Redis](#redis)
  - [Memcached](#memcached)
  - [RabbitMQ](#rabbitmq)
  - [Kafka](#kafka)
  - [MongoDB](#mongodb)
  - [HTTP calls](#http-calls)
  - [Celery workers](#celery-workers)
  - [SQLAlchemy](#sqlalchemy)
  - [All built-in probes (summary table)](#all-built-in-probes-summary-table)
- [Alerters](#alerters)
- [Probe introspection](#probe-introspection)
- [Configuration reference](#configuration-reference)
- [Security](#security)
- [Kubernetes integration](#kubernetes-integration)

---

## Health dashboard

`GET /health/dashboard` returns a timeboard: every graph shares one time axis, so a status change, a latency spike, and an error burst line up vertically. It is a single self-contained HTML page — inline SVG and vanilla JavaScript, no CDN or external assets, so it works in air-gapped clusters — and it updates live over SSE. Light and dark themes follow the viewer's OS setting.

The built-in page sends its own strict `Content-Security-Policy`: `default-src 'none'`, its one inline script allowed by a nonce minted for each response, inline styles (`style-src 'unsafe-inline'`), and requests only to its own origin (`connect-src 'self'`, which covers the live stream and token renewal under any mount or `root_path`), plus `base-uri 'none'`, `form-action 'none'` and `frame-ancestors 'none'`. Nothing else loads or runs, so a script a proxy injects into HTML pages is blocked there. A policy your app or proxy sends as well, or instead, cannot know the nonce: unless it allows inline scripts and styles, the page loses its graphs and live updates, and its styling too. Exempt the dashboard route from such a policy, pass your own renderer as `dashboard=`, or turn the dashboard off with `dashboard=False`.

```python
registry = HealthRegistry(app)                  # dashboard on — GET /health/dashboard
registry = HealthRegistry(app, dashboard=False) # dashboard off
```

**Header** — a plain-language status sentence (`orders-db unhealthy for 8 min`, `All 6 probes healthy`), a probe count summary, and the last check time. A maintenance banner appears when maintenance mode is active.

**Time range** — 15 min, 1 hour, 2 hours, or all retained history (remembered per browser). Graphs draw from the registry's stored history, which is embedded in the page and extended by live updates. How far back "All" reaches depends on `history_size` and `result_ttl_seconds` (120 results per probe and 2 hours by default); raise both for longer retention, or use [`RedisProbeStorage`](#redisprobestorage) to share history across workers.

The page stays light however much a probe reports: it embeds only the fields its graphs read (status, latency, percentiles, error rate, throughput, and event-loop lag; for routes, only the eight busiest), at most 2,000 results per probe. An open tab keeps live points for the longer of 2 hours and the embedded history's span, and at most 2,000 points per probe, so leaving it open for days does not grow its memory.

**Status over time** — one lane per probe, colored by status, with a tick at every state change. Gaps in the data are left blank rather than drawn as healthy.

**Probe rows** — the probe's name, description, tags, status, current latency, and any error on the left; on the right, the graphs its results support:

| Graph | Shown for | Series |
|---|---|---|
| Latency | probes reporting percentiles (route, passive, request metrics) | p50 (red, solid), p95 (yellow, dashed), p99 (green, dotted) |
| Latency | other probes that report a latency | latency |
| Event loop lag | `EventLoopProbe` | lag, with dashed `warn_ms` / `fail_ms` lines |
| Error rate | probes reporting `error_rate` | errors |
| Throughput | probes reporting `requests_per_minute` | requests |

Each multi-series graph has a legend and labels every line at its right end, and the percentile lines differ by dash pattern as well as color, so they stay distinguishable for color-blind viewers. "All fields" expands the probe's full details table.

**Routes** — when `RequestMetricsProbe` runs with `per_route=True`, the eight busiest route templates get their own p95 latency graph, with current p95, error rate, and throughput alongside.

**Crosshair** — hovering any graph shows the same moment on every graph, with that graph's values in a tooltip.

**Live updates** — the page opens an `EventSource` to `/health/status/stream?view=dashboard` (see [Dashboard view](#dashboard-view)), appends each new result to the graphs, and updates the rows in place. If the set of probes changes it reloads once to pick up the new rows. If the server refuses the stream (for example `503` once `max_stream_connections` streams are open), the page retries the stream alone with jittered exponential backoff, up to 5 minutes apart, rather than reloading.

---

## Live streaming

`/health/ready/stream` and `/health/status/stream` use SSE to push probe results to connected clients.

Probes on the registry-wide `poll_interval_ms` are polled while at least one SSE client is connected; the loop stops when the last one disconnects. Probes with their own `poll_interval_ms` poll in the background regardless, starting at `set_started()` (or at the first health request, if `set_started()` was called outside a running event loop).

Each event is a JSON-encoded health report on a `data:` line:

```
data: {"status": "healthy", "checked_at": "2024-06-01T12:00:00.123456+00:00", "probes": [...]}
```

Events are sent at the shortest effective probe interval (a probe with `poll_interval_ms=5_000` gets 5-second updates even when the registry default is 60 s). Probes without an interval are re-checked at most once per event interval however many streams are open: a stream reuses a result checked within it. Between events the stream sends a `: keepalive` comment line every 15 seconds so proxies and load balancers with idle timeouts don't close it; `EventSource` ignores comment lines.

In single-fetch mode (no probe has an interval) the stream sends one event, preceded by `retry: 30000`, and closes. Browsers then wait 30 seconds before reconnecting instead of their ~3-second default — each reconnect runs every probe again.

A `?probe=`/`?tag=` filter naming no registered probe or tag gets `404` before the stream starts. If known filters select no result (a probe that reports tags of its own is filtered by those), the stream instead sends `retry: 30000`, `event: error` and `data: {"error": "no probe matches every filter"}`, then closes, never an empty report that would read as healthy. A browser `EventSource` delivers that event to `onerror` (with `event.data` set) and reconnects 30 seconds later.

**Connection limit.** At most `max_stream_connections` streams (default `100`, across both stream endpoints) are open at once; each one wakes every 0.5 s, so the cap keeps stream clients from loading the app's event loop. Further stream requests get `503` with `Retry-After: 30` before any streaming starts. Pass `max_stream_connections=None` for no limit.

Streams keep working while the storage backend is down: they are served from this process's own results (see [Polling and caching](#polling-and-caching)). A stream opened with a dashboard stream token closes when the token expires; see [Authentication](#authentication).

**JavaScript:**

```js
const es = new EventSource('/health/status/stream');
es.onmessage = (event) => {
    const report = JSON.parse(event.data);
    console.log(report.status, report.probes);
};
es.onerror = () => es.close();
```

**curl:**

```bash
curl -N http://localhost:8000/health/status/stream
```

**Configuring the poll interval:**

```python
registry = HealthRegistry(app, poll_interval_ms=10_000)  # every 10 s
registry = HealthRegistry(app, poll_interval_ms=None)    # single-fetch mode

# Change at runtime
registry.set_poll_interval(30_000)
registry.set_poll_interval(0)  # disable
```

Minimum enforced interval is 1000 ms — lower values are clamped.

### Dashboard view

By default every event carries each probe's full `details`, including every route a `RequestMetricsProbe` tracks with all of its fields. Pass `?view=dashboard` (on either stream) to get what the built-in dashboard reads: each probe keeps its full `details`, except that `routes` is cut to the eight busiest routes with only `request_count`, `p95_rtt_ms`, `error_rate` and `requests_per_minute`. With 150 routes that is about 1.4 KB per event instead of about 42 KB. It combines with `?tag=`, `?probe=` and `?token=`; any other `view` value gets `422`. Without `view` the payload is unchanged. The built-in dashboard's stream URL includes it.

```bash
curl -N "http://localhost:8000/health/status/stream?view=dashboard"
```

---

## Polling and caching

`GET /health/ready` and `GET /health/status` always respond immediately:

- **With SSE clients connected** — serves the most recent cached result without re-running probes.
- **With no streaming active** — runs probes on demand with a built-in lock to prevent thundering herd.

Results are served from an in-process copy of each registered probe's last result; the request path writes to storage but never reads it. Two things follow:

- **Shared storage.** With a backend shared by several replicas (e.g. Redis), each replica reports only its own registered probes as checked by itself. A probe that has not yet run in this process runs on the first request, so `/health/ready` never passes on another replica's result.
- **Storage outages.** Every storage call is bounded by `storage_timeout` (default 2 s; `None` disables). A failing or timed-out call is logged (at most once a minute per operation) and never fails a request: `/health/ready`, `/health/status`, `/health/metrics` and the streams keep answering from in-process results, `/health/history` and `/health/alerts` return `503 {"error": "storage unavailable"}`, and the dashboard renders without history. Writes to any backend other than the default in-memory one (latest result, history, alert records) run in the background, so a slow or hung backend never delays a health check: each probe has at most one write in flight, and while the backend is behind only its newest unwritten result is kept (alert records: the newest 256). At shutdown, pending writes get up to 2 s to land. A result served from cache while a probe's circuit is open is not written again.

If the backend implements `forget(names)`, the registry uses it so a renamed or removed probe's data does not linger: it remembers every probe name it has written, and at most every 10 minutes (from the poll loop or a request, in the background, bounded by `storage_timeout`) it calls `forget()` with the names it wrote but no longer registers, for example after `discover_routes(refresh=True)` renames a route probe. Names this process never wrote are never deleted, so apps and replicas can share one backend and key prefix without removing each other's data, including old and new pods with different probe sets during a rolling deploy. Data of probes nobody writes any more expires with `result_ttl_seconds`. `/health/history`, `/health/alerts` and the dashboard show only the probes the app itself registers, so apps sharing a prefix do not see each other's probes, while replicas of one app (same probe names) share their history. Two apps that register the same probe *name* on one prefix still write to the same keys and cannot be told apart, so give each app its own prefix.

On shutdown the registry awaits `aclose()` on every registered and startup probe that defines one (5 s limit each; failures are logged).

---

## Per-probe poll frequency

Each active probe can override the registry's `poll_interval_ms`. Passive probes don't accept this parameter — their `check()` only reads in-memory stats.

```python
registry = HealthRegistry(app, poll_interval_ms=60_000)

registry.add(TCPProbe("db.internal", 5432, poll_interval_ms=30_000))  # every 30 s
registry.add(KafkaProbe("broker:9092", poll_interval_ms=10_000))      # every 10 s
registry.add(NoOpProbe())                                              # inherits 60 s
```

Custom probes can set a class-level default:

```python
class MyServiceProbe(BaseProbe):
    name = "my-service"
    poll_interval_ms = 5_000

    async def check(self) -> ProbeResult:
        ...
```

---

## Three-state health (DEGRADED)

| State | Meaning | `/health/ready` | `/health/status` |
|-------|---------|-----------------|------------------|
| `"healthy"` | All clear | `200` | `200` |
| `"degraded"` | Under stress, still serving | `200` | `207` |
| `"unhealthy"` | Critical failure | `503` | `207` |
| `"no_data"` | Nothing to report yet: no status | `200` | `200` |

`no_data` is what passive, route and WebSocket probes and `RequestMetricsProbe` report before they have observed a single call. It is not healthy and not failing: it never alerts or fires `on_state_change`, `ThresholdProbe` passes it through, `/metrics` writes no `probe_healthy`/`probe_degraded` sample for it (`probe_no_data` is `1`), and the dashboard shows it grey. It is not written to storage, so it does not appear in `/health/history` (and replicas of earlier versions sharing the backend never read a status they do not know). It does not fail readiness, so a pod whose probes need traffic to report can still become ready. A `probe=`/`tag=` filter that selects no registered probe is different: it gets `404` (see the README).

DEGRADED lets a probe communicate "something is wrong but we're still responding" without triggering an emergency. Load balancers keep routing traffic.

Overall status rules (critical probes only):
- Any UNHEALTHY → overall `"unhealthy"`
- Any DEGRADED (no UNHEALTHY) → overall `"degraded"`
- All passing → overall `"healthy"`
- No critical probe has data yet → overall `"no_data"`

```python
return ProbeResult(
    name=self.name,
    status=ProbeStatus.DEGRADED,
    details={"queue_depth": 950, "threshold": 800},
)
```

`ProbeResult` convenience properties:

| Property | Returns `True` when |
|----------|---------------------|
| `is_healthy` | `status == "healthy"` |
| `is_degraded` | `status == "degraded"` |
| `is_passing` | `status != "unhealthy"` |

DEGRADED counts as passing for the circuit breaker — a probe oscillating between healthy and degraded never trips its circuit.

---

## Circuit breaker

After a probe fails a configurable number of consecutive times, the circuit opens and the probe is suspended — it returns the last known result until the cooldown expires.

The breaker applies to **active** probes, which call a real dependency on every check. Passive probes (`PassiveProbe` and its subclasses, `FastAPIRouteProbe`, `FastAPIWebSocketProbe`, `RequestMetricsProbe`) default to `circuit_breaker_enabled=False`: they only read in-memory stats, so suspending them protects nothing and would keep showing a stale failure after traffic recovers. Pass `circuit_breaker_enabled=True` to opt one in.

```python
# Defaults: opens after 5 consecutive failures, cooldown 10 minutes
registry = HealthRegistry(app)

# Custom
registry = HealthRegistry(
    app,
    circuit_breaker_threshold=3,
    circuit_breaker_cooldown_ms=120_000,
)

# Disable
registry = HealthRegistry(app, circuit_breaker=False)
```

Per-probe override:

```python
probe = TCPProbe(host="db.internal", port=5432)
probe.circuit_breaker_threshold = 2
probe.circuit_breaker_cooldown_ms = 60_000
registry.add(probe)
```

---

## Circuit breaker metrics

When enabled (default), a `circuit_breaker` dict is injected into every probe result's `details`:

```json
{
  "circuit_breaker": {
    "open": false,
    "consecutive_failures": 3,
    "trips_total": 1
  }
}
```

| Field | Description |
|-------|-------------|
| `open` | `true` when the circuit is open and the probe is suspended |
| `consecutive_failures` | Unbroken run of failures since the last success |
| `trips_total` | Lifetime trip count |

Dashboard display:

| State | Display |
|---|---|
| Closed, never tripped | `closed` |
| Closed, tripped before | `closed, 2 trips total` |
| Open | `open — suspended (5 consecutive failures, 2 trips total)` |

Also exported as Prometheus gauges: `probe_circuit_open`, `probe_circuit_consecutive_failures`, `probe_circuit_trips_total`.

All `/metrics` names start with `probe_`, which collides with blackbox_exporter's metrics if both are scraped into one Prometheus. Pass `HealthRegistry(app, metrics_prefix="fastapi_watch_")` to get `fastapi_watch_probe_healthy` and so on; the default keeps the existing names so current dashboards and alerts keep working.

---

## Maintenance mode

While active: `/health/ready` returns `200 {"status": "maintenance"}`, alerters are suppressed, and the dashboard shows an amber banner.

Probes keep running and state changes are still recorded and passed to `on_state_change` callbacks. When maintenance ends, the first check of each probe sends alerters any difference from the last status they were sent: a dependency that went down during the window and is still down alerts `healthy -> unhealthy`, while one that failed and recovered within the window sends nothing.

### HTTP endpoints

`POST` and `DELETE /health/maintenance` change readiness and silence alerts, so they are **only mounted when `auth=` is configured** (see [Authentication](#authentication)). Without auth, `GET /health/maintenance` still reports status and the [Python API](#python-api) works as usual.

```bash
# Enable (indefinite)
curl -X POST https://your-app/health/maintenance -H "X-API-Key: $KEY"

# Enable for 30 minutes
curl -X POST https://your-app/health/maintenance -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" -d '{"minutes": 30}'

# Enable until a specific time
curl -X POST https://your-app/health/maintenance -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"until": "2026-04-01T02:00:00+00:00"}'

# Disable
curl -X DELETE https://your-app/health/maintenance -H "X-API-Key: $KEY"

# Check status
curl https://your-app/health/maintenance
# → {"active": true, "until": "2026-04-01T02:00:00+00:00"}
```

An `until` without a UTC offset is read in the registry's `timezone`. An `until` that has already passed is rejected with `422` and leaves any current maintenance window untouched.

Because browsers attach cached Basic credentials to any request to your site, `POST` and `DELETE` refuse cross-site browser requests with `403`: a `Sec-Fetch-Site: cross-site` header, or an `Origin` whose host and port don't match the request's `Host` (or `X-Forwarded-Host`). A non-empty body must be `Content-Type: application/json`, otherwise `415`, and at most 4 KiB, otherwise `413`; it is read only after the credentials are checked. curl, scripts and other non-browser clients send neither header, so the commands above, including the empty-body `POST`, work unchanged.

### Deployment workflow

```bash
# 1. Enable maintenance before deploying
curl -X POST https://your-app/health/maintenance -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" -d '{"minutes": 10}'

# 2. Deploy / restart
kubectl rollout restart deployment/my-app

# 3. Clear maintenance (or let the window expire)
curl -X DELETE https://your-app/health/maintenance -H "X-API-Key: $KEY"
```

### Python API

```python
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

registry.set_maintenance()                                                          # indefinite
registry.set_maintenance(until=datetime.now(ZoneInfo("UTC")) + timedelta(hours=2)) # timed
registry.clear_maintenance()
```

Both methods return `self` for chaining. Once `until` passes, maintenance auto-clears. A naive `until` is interpreted in the registry's `timezone`.

---

## ProbeGroup — organizing probes across files

`ProbeGroup` mirrors FastAPI's `APIRouter` pattern — declare probes in the modules that own them and compose them at startup.

```python
# features/database/probes.py
from fastapi_watch import ProbeGroup
from fastapi_watch.probes import PostgreSQLProbe, RedisProbe

router = ProbeGroup()
router.add(PostgreSQLProbe(name="postgres"))
router.add(RedisProbe(name="redis"), critical=False)
```

```python
# features/users/probes.py
from fastapi_watch import ProbeGroup, FastAPIRouteProbe

router = ProbeGroup()
users_probe = FastAPIRouteProbe(name="users-api", max_error_rate=0.05)
router.add(users_probe)
```

```python
# main.py
from fastapi import FastAPI
from fastapi_watch import HealthRegistry
from features.database.probes import router as db_router
from features.users.probes import router as users_router, users_probe

app = FastAPI()
registry = HealthRegistry(app, groups=[db_router, users_router])

@app.get("/users")
@users_probe.watch
async def list_users():
    ...
```

Groups can compose other groups:

```python
from fastapi_watch import ProbeGroup
from .database import router as db_router
from .payments import router as payments_router

router = ProbeGroup()
router.include(db_router)
router.include(payments_router)
```

**Group-level tags** — tags declared on a `ProbeGroup` are automatically merged into each probe's own tags when the group is added to the registry. This lets you tag an entire module's probes without touching each probe individually:

```python
db_probes = ProbeGroup(tags=["database"])
db_probes.add(PostgreSQLProbe(name="postgres"))
db_probes.add(RedisProbe(name="redis"), critical=False)

# Both probes now carry the "database" tag
registry = HealthRegistry(app, groups=[db_probes])

# Filter health checks to only database probes
# GET /health/ready?tag=database
```

`ProbeGroup` API — all methods return `self`:

```python
group = ProbeGroup(tags=["infra"])   # optional group-level tags
group.add(probe)                     # single probe, critical by default
group.add(probe, critical=False)
group.add_probes([a, b])
group.include(another_group)
```

---

## Startup probe

`GET /health/startup` returns `503` until `set_started()` is called. Use as a Kubernetes `startupProbe` target.

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi_watch import HealthRegistry

@asynccontextmanager
async def lifespan(app: FastAPI):
    registry.set_started()
    yield

app = FastAPI(lifespan=lifespan)
registry = HealthRegistry(app)
```

Pass `startup_probes` to also require specific checks before the gate opens:

```python
from fastapi_watch.probes import TCPProbe

registry = HealthRegistry(
    app,
    startup_probes=[TCPProbe(host="db.internal", port=5432)],
)
```

Startup probes are separate from the main registry — they don't appear in `/health/status` and are not subject to the circuit breaker.

```yaml
startupProbe:
  httpGet:
    path: /health/startup
    port: 8000
  failureThreshold: 30
  periodSeconds: 5
```

---

## Startup grace period

`grace_period_ms` holds `/health/ready` at `503 {"status": "starting"}` for a fixed window after startup — useful for warm-up without requiring all probes to pass immediately.

```python
registry = HealthRegistry(app, grace_period_ms=15_000)  # hold for 15 s
```

Only `/health/ready` is affected — `/health/status` and `/health/live` always reflect real probe results. The window is measured from when the `HealthRegistry` is created, not from `set_started()`.

---

## State-change callbacks

Callbacks fire whenever a probe's status changes between runs. Receives `(probe_name: str, old_status: ProbeStatus, new_status: ProbeStatus)`. A probe's state before its first result counts as `HEALTHY`, so a probe that is already failing at startup fires `healthy → unhealthy` (and alerts); a first healthy result fires nothing. A callback that raises is logged and does not affect the health check or the other callbacks.

```python
def on_change(probe_name, old_status, new_status):
    logger.warning("Probe %s changed: %s → %s", probe_name, old_status, new_status)

registry.on_state_change(on_change)
```

Async callbacks are also supported. Multiple callbacks can be registered; all fire in registration order. `on_state_change()` returns `self` for chaining.

---

## Probe result history

```python
registry = HealthRegistry(
    app,
    history_size=20,           # max results per probe (default: 120)
    result_ttl_seconds=3600,   # drop results older than 1 hour (default: 7200)
)
```

`GET /health/history` response:

```json
{
  "probes": {
    "postgresql": [
      {"name": "postgresql", "status": "healthy", "critical": true, "latency_ms": 1.8, "error": null, "details": {...}}
    ]
  }
}
```

Results are ordered oldest-first and are in-memory by default. Each entry is dropped once it is older than `result_ttl_seconds`, and each probe keeps at most `history_size` entries. See [Custom storage backends](#custom-storage-backends) to share them across workers or keep them across restarts. With a custom `storage=`, the retention arguments reach the backend only when you pass at least one of them explicitly (see [Retention settings](#retention-settings)); a backend without `configure()` keeps its own limits and a warning names the ignored arguments. If the backend is unavailable, the endpoint returns `503 {"error": "storage unavailable"}`.

---

## Alert history

Every probe state change is recorded. Retrieve with `GET /health/alerts`.

```python
registry = HealthRegistry(
    app,
    alert_ttl_seconds=86400,  # 24 hours (default: 259200 = 72 hours)
    max_alerts=500,            # default: 120
)
```

```json
{
  "alerts": [
    {
      "probe": "redis",
      "old_status": "healthy",
      "new_status": "unhealthy",
      "timestamp": "2026-03-29T14:22:01.843+00:00"
    }
  ]
}
```

Alerts are ordered oldest-first. If the storage backend is unavailable, the endpoint returns `503 {"error": "storage unavailable"}`.

---

## Custom storage backends

Pass any object implementing `ProbeStorage` as `HealthRegistry(app, storage=...)`. Setting a TTL to `0` disables age-based expiry for that collection (count limits still apply).

The default in-process backend, `InMemoryProbeStorage(max_results=120, result_ttl_seconds=7200, alert_ttl_seconds=259200, max_alerts=120, max_entry_bytes=8192)`, caps history entries by bytes the same way `RedisProbeStorage` does (see **Byte cap** below), so a probe with a very large `details` payload cannot keep `history_size` full copies of it in memory.

### Retention settings

Which limits a custom backend uses depends on what you pass to `HealthRegistry`:

- **None of `history_size`, `result_ttl_seconds`, `alert_ttl_seconds`, `max_alerts` passed:** the backend keeps the values it was constructed with. `HealthRegistry(app, storage=RedisProbeStorage(client, max_results=500))` keeps 500 entries per probe.
- **At least one passed:** the registry calls the backend's `configure()` with all four of its effective values, the ones you passed plus the registry defaults (`120`, `7200`, `259200`, `120`) for the rest. These replace the backend's constructor values.
- **Backend without `configure()`:** it always keeps its own limits; a warning names any retention arguments you passed.

Set the limits in one place: either on the backend's constructor, or as registry arguments.

### `RedisProbeStorage`

```python
import redis.asyncio as redis
from fastapi_watch import HealthRegistry, RedisProbeStorage

storage = RedisProbeStorage(
    redis.from_url("redis://localhost:6379/0"),  # with or without decode_responses=True
    prefix="myapp:health",   # namespace; one per app sharing the Redis
    max_entry_bytes=8192,    # byte cap per stored result
    max_connections=4,       # most connections taken from the client's pool at once
)
registry = HealthRegistry(app, storage=storage, history_size=120, result_ttl_seconds=7200)
# history_size / result_ttl_seconds passed here, so configure() applies them
# (and the registry's defaults for alert_ttl_seconds and max_alerts).
```

| Argument | Default | Description |
|----------|---------|-------------|
| `redis` | required | A `redis.asyncio` client |
| `prefix` | `"fastapi-watch"` | Prefix for every key. On Redis Cluster use braces (`"{myapp:health}"`) so all keys share a slot. Give each app its own: alerts under one prefix share one `max_alerts` cap, so another app's alerts can push yours out of `/health/alerts` |
| `max_results` | `120` | History entries per probe |
| `result_ttl_seconds` | `7200` | Age limit for latest results and history entries. `0` = no age limit |
| `alert_ttl_seconds` | `259200` | Age limit for alerts. `0` = no age limit |
| `max_alerts` | `120` | Total alerts kept |
| `max_entry_bytes` | `8192` | Largest stored latest result or history entry, minimum 512 |
| `max_connections` | `4` | Most Redis commands in flight at once, so the most connections taken from the client's pool. `None` = no limit |

The four retention arguments are replaced by the registry's values only when `HealthRegistry` is given at least one of `history_size`, `result_ttl_seconds`, `alert_ttl_seconds` or `max_alerts` (see [Retention settings](#retention-settings)).

Built so monitoring never loads the Redis it shares with your app:

- **No `KEYS` or `SCAN`.** Probe names are kept in an index (`<prefix>:names`, a sorted set scored by each name's last write). `get_all_latest` is `ZRANGEBYSCORE` plus one `MGET`, and `get_history` is `ZRANGEBYSCORE` plus one pipelined round trip, however many probes there are.
- **Atomic writes.** Each write is one `MULTI/EXEC`: add the entry, drop entries older than the TTL, trim to the count limit, drop index names not written within the TTL, and refresh a key-level expiry set slightly past the TTL. No key is ever left without its expiry, and names of renamed or removed probes leave the index on their own, even if nothing reads it. With a TTL of `0` no expiry is set (an expiry left by an earlier non-zero TTL is removed) and nothing is deleted by age.
- **Bounded connections.** At most `max_connections` commands run at once; the rest queue (within `storage_timeout`). Without this, checking many probes together (for example after `discover_routes()`) took two connections per probe at once and could drain a pool the app shares: redis-py 5+ then raises `MaxConnectionsError` for the app's own commands too.
- **Per-entry ageing.** History (`<prefix>:history:<name>`) and alerts (`<prefix>:alerts`) are sorted sets scored by `checked_at` / `timestamp`; reads skip expired entries even before a write trims them.
- **Byte cap.** A latest result or history entry over `max_entry_bytes` loses its largest `details` values first (for example a request-metrics probe's per-route table) and gets `details["_truncated"] = true`; scalar values such as `p95_rtt_ms` stay, so dashboard graphs keep working. If that is not enough, `description` and `tags` are dropped and `error` is shortened.
- **Cleanup.** Keys of probes that stop reporting expire on their own. `forget(names)` deletes the latest result, history and index entry of exactly the named probes; the registry passes only names it wrote and no longer registers, so it never removes another app's or replica's data. With `result_ttl_seconds=0` nothing expires, so a probe renamed or removed across a restart keeps its keys until you delete them (`await storage.forget({"old-name"})`); this includes keys written while the TTL was `0` and never written again after you set one.
- **Upgrading.** Versions before this one indexed names in a set at `<prefix>:probes`. Each new instance deletes it on its first write; with a non-zero TTL it would also expire on its own.
- **One alert per state change across replicas.** `claim_alert(key, value, ttl_seconds)` is one Lua script on `<prefix>:alert-claim:<key>`: it returns `False` if the key already holds `value`, and otherwise stores `value` with a `PX` expiry and returns `True`. So only the first replica to announce a status gets `True`, a probe that changes status again within the window (unhealthy → healthy → unhealthy) is announced again, and every claim key expires.

Upper bound on memory used:

```
probes × ((history_size + 1) × max_entry_bytes + probe name) + max_alerts × (~150 B + probe name)
```

where `probes` counts the probe names written within the last `result_ttl_seconds` (or, when that TTL is `0`, every name written and not yet removed by `forget()`).

### Writing your own

Implement the eight `ProbeStorage` methods: `get_latest`, `get_all_latest`, `set_latest`, `clear_latest` (sync; schedule I/O on the running loop), `append_history`, `get_history`, `append_alert` and `get_alerts`. Results and alerts are returned oldest-first. Three optional methods are called when present:

- `configure(*, max_results, result_ttl_seconds, alert_ttl_seconds, max_alerts)`: receives the registry's retention settings, only when at least one was passed to `HealthRegistry` (see [Retention settings](#retention-settings)).
- `async forget(names: set[str])`: delete the latest result, history and any index entry of exactly the probes in `names`, and nothing else. The registry passes only names this process wrote and no longer registers.
- `async claim_alert(key: str, value: str, ttl_seconds: float) -> bool`: atomically store `value` under `key` for `ttl_seconds`; return `True` if `key` was absent, expired or held a different value, and `False` if it already held `value` (another replica announced it). `ttl_seconds <= 0` always returns `True`. Claims must expire.

Bound every collection by count and by age, per entry. The `ProbeStorage` docstring has an outline.

---

## Response format

Every response from `/health/ready`, `/health/status`, and the SSE streams shares this shape (with `?view=dashboard`, the streams trim `details.routes`; see [Dashboard view](#dashboard-view)).

**Health report:**

| Field | Type | Description |
|-------|------|-------------|
| `status` | `"healthy"` \| `"degraded"` \| `"unhealthy"` \| `"no_data"` | Overall result — critical probes only; `"no_data"` while none of them has reported |
| `checked_at` | `string \| null` | UTC ISO 8601 timestamp of the last probe run |
| `timezone` | `string \| null` | IANA timezone name |
| `probes` | `array` | Individual probe results |
| `failing` | `string[]` | Names of critical probes whose status is `"unhealthy"`. Empty array when nothing is failing. Not affected by DEGRADED — only UNHEALTHY probes appear here. |

**Probe result:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | `string` | Probe identifier |
| `status` | `"healthy"` \| `"degraded"` \| `"unhealthy"` \| `"no_data"` | State for this probe; `"no_data"` (no status) until it has observed anything |
| `critical` | `boolean` | `true` if the probe affects overall status |
| `latency_ms` | `number` | How long the check took |
| `error` | `string \| null` | Error message on failure |
| `details` | `object \| null` | Service-specific metadata |

**Example — all healthy:**

```json
{
  "status": "healthy",
  "checked_at": "2024-06-01T12:00:00.123456+00:00",
  "timezone": "UTC",
  "probes": [
    {
      "name": "postgresql",
      "status": "healthy",
      "critical": true,
      "latency_ms": 1.8,
      "error": null,
      "details": {"version": "PostgreSQL 16.2", "active_connections": 5}
    }
  ]
}
```

---

## Writing a custom probe

Extend `BaseProbe` and implement `async check() -> ProbeResult`. Unhandled exceptions are caught automatically by the registry.

### Minimal

```python
from fastapi_watch.probes import BaseProbe
from fastapi_watch.models import ProbeResult, ProbeStatus

class MyServiceProbe(BaseProbe):
    name = "my-service"

    async def check(self) -> ProbeResult:
        ok = await call_my_service()
        return ProbeResult(
            name=self.name,
            status=ProbeStatus.HEALTHY if ok else ProbeStatus.UNHEALTHY,
        )

registry.add(MyServiceProbe())
```

### With latency and details

```python
import time
from fastapi_watch.probes import BaseProbe
from fastapi_watch.models import ProbeResult, ProbeStatus

class PaymentGatewayProbe(BaseProbe):
    name = "payment-gateway"

    async def check(self) -> ProbeResult:
        start = time.perf_counter()
        try:
            info = await ping_payment_gateway()
            latency = (time.perf_counter() - start) * 1000
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.HEALTHY,
                latency_ms=round(latency, 2),
                details={"region": info.region, "version": info.version},
            )
        except Exception as exc:
            latency = (time.perf_counter() - start) * 1000
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.UNHEALTHY,
                latency_ms=round(latency, 2),
                error=f"gateway check failed: {type(exc).__name__}",
            )
```

`error` is shown to anyone who can read the health endpoints. Report the exception type, as the built-in probes do, and log the message: it can carry hostnames, credentials or query parameters (see [Probe error messages](#probe-error-messages)).

### Configurable

```python
class S3BucketProbe(BaseProbe):
    def __init__(self, bucket: str, region: str = "us-east-1", name: str = "s3") -> None:
        self.bucket = bucket
        self.region = region
        self.name = name

    async def check(self) -> ProbeResult:
        import time, aiobotocore.session
        start = time.perf_counter()
        try:
            session = aiobotocore.session.get_session()
            async with session.create_client("s3", region_name=self.region) as client:
                await client.head_bucket(Bucket=self.bucket)
            latency = (time.perf_counter() - start) * 1000
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.HEALTHY,
                latency_ms=round(latency, 2),
                details={"bucket": self.bucket, "region": self.region},
            )
        except Exception as exc:
            latency = (time.perf_counter() - start) * 1000
            return ProbeResult(name=self.name, status=ProbeStatus.UNHEALTHY,
                               latency_ms=round(latency, 2), error=type(exc).__name__)

registry.add(S3BucketProbe("my-uploads", "eu-west-1", name="s3-uploads"))
registry.add(S3BucketProbe("my-backups", "us-east-1", name="s3-backups"))
```

### Composite (custom aggregation)

```python
import asyncio
from fastapi_watch.probes import BaseProbe, TCPProbe
from fastapi_watch.models import ProbeResult, ProbeStatus

class RedisHAProbe(BaseProbe):
    name = "redis-ha"

    def __init__(self, primary_host: str, replica_host: str) -> None:
        self._primary = TCPProbe(host=primary_host, port=6379, name="primary")
        self._replica = TCPProbe(host=replica_host, port=6379, name="replica")

    async def check(self) -> ProbeResult:
        primary, replica = await asyncio.gather(
            self._primary.check(), self._replica.check()
        )
        if primary.is_healthy or replica.is_healthy:
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.HEALTHY,
                details={"primary": primary.status.value, "replica": replica.status.value},
            )
        return ProbeResult(
            name=self.name,
            status=ProbeStatus.UNHEALTHY,
            error=f"both nodes down — primary: {primary.error}, replica: {replica.error}",
        )
```

### Releasing resources

A probe that holds a client, connection pool or background task can override `async aclose()`. The registry awaits it once at shutdown for every registered and startup probe (5 s limit each; failures are logged and do not stop the others). The default does nothing.

```python
class QueueDepthProbe(BaseProbe):
    name = "queue-depth"

    def __init__(self, url: str) -> None:
        self._client = QueueClient(url)  # reused across checks

    async def check(self) -> ProbeResult:
        depth = await self._client.depth()
        return ProbeResult(name=self.name, status=ProbeStatus.HEALTHY, details={"depth": depth})

    async def aclose(self) -> None:
        await self._client.close()
```

### Testing a custom probe

```python
import pytest
from fastapi import FastAPI
from fastapi_watch import HealthRegistry
from fastapi_watch.models import ProbeStatus
from myapp.probes import MyServiceProbe

@pytest.mark.asyncio
async def test_healthy_when_service_responds():
    probe = MyServiceProbe()
    result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY

@pytest.mark.asyncio
async def test_unhealthy_on_exception(monkeypatch):
    async def fail():
        raise ConnectionError("refused")
    monkeypatch.setattr("myapp.probes.call_my_service", fail)
    # The registry turns an exception escaping check() into an UNHEALTHY result.
    registry = HealthRegistry(FastAPI(), poll_interval_ms=None)
    registry.add(MyServiceProbe())
    (result,) = await registry.run_all()
    assert result.status == ProbeStatus.UNHEALTHY
    assert "ConnectionError" in result.error  # the type only, never the message
```

### Checklist

- `name` must be set — class attribute or `self.name = ...` in `__init__`.
- `check()` must be `async` and return `ProbeResult`.
- Set `latency_ms` for probes where response time matters.
- Every probe is bounded by the registry's `probe_timeout` (30 s by default); set `timeout` to use a tighter bound.
- Do not call `registry.run_all()` from inside `check()`.

---

## Route auto-discovery

fastapi-watch provides three ways to monitor FastAPI routes. They have a fixed priority order and are safe to combine — when multiple approaches target the same route, the highest-priority one wins and the others skip it silently.

### Monitoring priority

| Priority | Method | When to use |
|----------|--------|-------------|
| 1 — highest | `@probe.watch` | One specific route needs its own name, thresholds, description, or tags |
| 2 | `watch_router` | A whole router represents a logical group with shared settings |
| 3 — lowest | `discover_routes` | Catch-all for everything not handled explicitly |

**Why this order?** `@probe.watch` represents the most deliberate choice — the developer explicitly created a probe with custom configuration for a specific route. That intent should never be silently overwritten by a bulk operation. `watch_router` is more intentional than `discover_routes` because it targets a specific router; it overrides any defaults that `discover_routes` may have applied but respects manual probes. `discover_routes` is the broadest sweep and therefore has the lowest priority.

### `@probe.watch` — highest priority

Apply directly to a route handler for full per-route control. No other method will override it.

```python
from fastapi_watch import FastAPIRouteProbe

checkout_probe = FastAPIRouteProbe(
    name="checkout",
    description="Payment processing",
    tags=["payments"],
    max_error_rate=0.01,
    slow_call_threshold_ms=200,
)

@app.post("/checkout")
@checkout_probe.watch
async def checkout():
    ...

registry.add(checkout_probe)
```

### `watch_router` — mid priority

Monitors every route on a specific `APIRouter` with shared settings. Call it after `app.include_router` — FastAPI must have already baked the routes into `app.routes` before `watch_router` can find them. Skips routes already decorated with `@probe.watch`. Overrides any `discover_routes` monitoring on the same routes.

```python
app.include_router(users_router, prefix="/users")
app.include_router(orders_router, prefix="/orders")
app.include_router(internal_router, prefix="/internal")

registry.watch_router(users_router, tags=["users"], max_error_rate=0.05)
registry.watch_router(orders_router, tags=["orders"], max_avg_rtt_ms=500)
registry.watch_router(internal_router, tags=["internal"], critical=False)
```

`/health/ready?tag=users` then checks only the user routes, `/health/ready?tag=orders` checks only orders, and so on.

**Args:**

| Argument | Default | Description |
|----------|---------|-------------|
| `router` | required | The `APIRouter` to monitor |
| `tags` | `None` | Extra tags appended to every probe (FastAPI route tags are also merged in automatically) |
| `critical` | `True` | Whether auto-created probes are critical |
| `exclude_paths` | `None` | Route paths to skip; supports fnmatch glob patterns (e.g. `["/admin/*"]`) |
| `include_paths` | `None` | Whitelist of route paths to monitor; supports fnmatch glob patterns (e.g. `["/users/*/profile"]`). `exclude_paths` takes precedence when both are set |
| `include_methods` | `None` | Only monitor routes whose HTTP method matches (e.g. `["GET", "POST"]`); WebSocket routes are always included |
| `ws_probe_kwargs` | `None` | Extra kwargs forwarded to each `FastAPIWebSocketProbe` |
| `name_fn` | `None` | Callable `(route) -> str` for custom probe names; defaults to the handler function name |
| `group` | `False` | When `True`, collects all probes into a `ProbeGroup` before registering so group-level tags propagate to every member |
| `**probe_kwargs` | — | Forwarded to each `FastAPIRouteProbe` |

### `discover_routes` — lowest priority

Scans all registered routes and monitors any that haven't already been covered. Call it from a lifespan startup handler so all `include_router` calls have already run. Routes under the health prefix are always excluded automatically.

```python
@asynccontextmanager
async def lifespan(app):
    registry.discover_routes(tags=["api"])
    registry.set_started()
    yield
```

**Args:**

| Argument | Default | Description |
|----------|---------|-------------|
| `exclude_paths` | `None` | Route paths to skip; supports fnmatch glob patterns (e.g. `["/internal/*", "/admin/*"]`) |
| `include_paths` | `None` | Whitelist of route paths to monitor; supports fnmatch glob patterns (e.g. `["/api/*"]`). Only routes matching at least one pattern are monitored. `exclude_paths` takes precedence when both are set |
| `include_methods` | `None` | Only monitor routes whose HTTP method matches (e.g. `["GET", "POST"]`); WebSocket routes always pass |
| `critical` | `True` | Whether auto-created probes are critical |
| `tags` | `None` | Extra tags appended to every probe (FastAPI route tags are also merged in automatically) |
| `ws_probe_kwargs` | `None` | Extra kwargs forwarded to each `FastAPIWebSocketProbe` (e.g. `{"min_active_connections": 1}`) |
| `refresh` | `False` | Re-monitor previously auto-discovered routes with updated options. `@probe.watch` and `watch_router` routes are never overridden even with `refresh=True` |
| `name_fn` | `None` | Callable `(route) -> str` for custom probe names; defaults to the handler function name |
| `**probe_kwargs` | — | Forwarded to each `FastAPIRouteProbe` (e.g. `max_error_rate=0.05`) |

**Probe naming:** each probe is named after the handler function (`route.name`), which FastAPI sets from the function name by default — so probes have readable names like `list_users` and `create_order` without any configuration. When two routes share a handler name (common across routers), the later one is qualified with its methods and path — e.g. `list_items` and `list_items:GET_orders` — so each keeps its own result. `exclude_paths` / `include_paths` match the full path a client requests, including any `include_router` prefix.

**Probe description:** each probe's description is automatically set to the HTTP method and path (e.g. `GET /users/{user_id}` or `WS /ws/chat`), which appears as a subtitle under the probe name on the dashboard.

**FastAPI route tags:** `discover_routes` and `watch_router` automatically merge each route's existing FastAPI/OpenAPI tags (e.g. `@app.get("/items", tags=["store"])`) into the probe's tags. User-supplied `tags=[...]` are appended on top. This means routes already organized by OpenAPI tags get probe tags for free.

**WebSocket routes:** both `discover_routes` and `watch_router` automatically detect WebSocket routes and create a `FastAPIWebSocketProbe` for each. Use `ws_probe_kwargs` to pass WebSocket-specific options:

```python
registry.discover_routes(
    tags=["api"],
    max_error_rate=0.05,                          # HTTP routes
    ws_probe_kwargs={"min_active_connections": 1}, # WebSocket routes
)
```

**Glob exclusions and method filtering:**

```python
# Skip all internal and admin routes; only monitor read and write endpoints
registry.discover_routes(
    exclude_paths=["/internal/*", "/admin/*"],
    include_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
```

**Tag-based filtering** — comma-separated tags use OR logic (probes matching any of the tags are returned):

```bash
curl http://localhost:8000/health/ready?tag=api            # only route probes
curl http://localhost:8000/health/status?tag=api,payments  # api OR payments probes
curl http://localhost:8000/health/status?tag=infrastructure  # only infra probes
```

**Probe name filtering** — filter by exact probe name with `?probe=` (comma-separated). Both `?tag=` and `?probe=` can be combined (AND logic — a result must match both):

```bash
curl http://localhost:8000/health/status?probe=postgresql,redis   # only these two probes
curl http://localhost:8000/health/ready?probe=checkout            # single probe readiness
curl http://localhost:8000/health/status?tag=payments&probe=checkout  # must match both
```

Works on `/health/ready`, `/health/status`, `/health/ready/stream`, and `/health/status/stream`.

**Dynamic route refresh:**

```python
# Re-run discover_routes after routes are added dynamically
registry.discover_routes(refresh=True)
```

### Combining all three

The three approaches layer cleanly. A typical production setup:

```python
# 1. @probe.watch on routes that need tight, custom thresholds
checkout_probe = FastAPIRouteProbe(
    name="checkout",
    description="Payment processing",
    tags=["payments"],
    max_error_rate=0.01,
    slow_call_threshold_ms=200,
)

@app.post("/checkout")
@checkout_probe.watch
async def checkout(): ...

registry.add(checkout_probe)

# 2. watch_router for routers that share a logical group and settings
app.include_router(admin_router, prefix="/admin")
registry.watch_router(admin_router, tags=["admin"], critical=False)

# 3. discover_routes as the catch-all for everything else
@asynccontextmanager
async def lifespan(app):
    registry.discover_routes(tags=["api"], max_error_rate=0.05)
    registry.set_started()
    yield
```

`/checkout` is covered by `@probe.watch`, all `/admin/*` routes by `watch_router`, and everything else by `discover_routes` — with no conflicts.

---

## Built-in probes

### App-wide request metrics

`RequestMetricsMiddleware` wraps your app at the ASGI layer and collects aggregate stats for every route without touching individual handlers.

It is plain ASGI middleware (not a `BaseHTTPMiddleware`), so it adds no extra task or response copy per request and context variables set by your app propagate normally. A response is timed until its last body chunk is sent, so streaming responses count their full duration while background tasks that run afterwards do not. An unhandled exception, or a response that never starts, counts as a 500 and the exception is re-raised. WebSocket and lifespan traffic pass straight through, and requests to a `HealthRegistry`'s own routes (status, readiness, the dashboard and its streams) are not counted, so health checks never skew your app's numbers.

```python
from fastapi_watch import HealthRegistry, RequestMetricsMiddleware, RequestMetricsProbe

app = FastAPI()
app.add_middleware(RequestMetricsMiddleware, per_route=True)

registry = HealthRegistry(app)
registry.add(RequestMetricsProbe(app, max_error_rate=0.05, max_avg_rtt_ms=500))
```

Pass the **app** to `RequestMetricsProbe`. Starlette builds middleware instances lazily (on the first request or lifespan event), so the probe looks up the live `RequestMetricsMiddleware` in `app.middleware_stack` on its first check. Until then it reports `"no requests observed yet"`; if the stack is built without the middleware it reports UNHEALTHY with a configuration error. Passing a `RequestMetricsMiddleware` instance you serve yourself still works.

`per_route=True` breaks stats down by route template (e.g. `/users/{user_id}`, including any `include_router` or `Mount` prefix). Requests that match no route share one `"<unmatched>"` entry and a `Mount` without inner routes (static files, raw ASGI apps) shares one `"/static/{path:path}"`-style entry, so the table stays bounded however many distinct URLs arrive. A `Mount`'s path parameters are keyed by name as well (`app.mount("/t/{tenant}", sub)` gives `/t/{tenant}/items`, not one key per tenant), so tenant IDs never appear in the metrics. As a backstop the table holds at most `max_routes` entries; requests for further routes share one `"<other>"` entry. `per_route=False` collects aggregates only.

**`RequestMetricsMiddleware` args:**

| Argument | Default | Description |
|----------|---------|-------------|
| `per_route` | `True` | Per-route-template breakdown |
| `window_size` | `200` | Recent requests used for `error_rate`, percentiles, and requests-per-minute. A count, not a time span: on a busy route it covers seconds, on a quiet one it can cover hours. Must be positive |
| `ema_alpha` | `0.1` | EMA smoothing factor, in `(0, 1]` |
| `min_error_status` | `500` | Status codes counted as errors (`500` = 5xx only; `400` = 4xx and 5xx) |
| `max_routes` | `500` | Most per-route entries kept; further routes share one `"<other>"` entry. Must be positive |

**`RequestMetricsProbe` args:**

| Argument | Default | Description |
|----------|---------|-------------|
| `middleware` | required | The app the middleware was added to (recommended), or a `RequestMetricsMiddleware` instance requests pass through |
| `name` | `"request_metrics"` | Probe label |
| `max_error_rate` | `0.1` | Error-rate threshold |
| `max_avg_rtt_ms` | `None` | RTT threshold; `None` disables it |
| `circuit_breaker_enabled` | `False` | The probe only reads in-memory counters, so the circuit breaker is off by default |

---

### FastAPI route probe

`FastAPIRouteProbe` collects real-traffic metrics from a FastAPI route — no synthetic requests.

For most apps, [route auto-discovery](#route-auto-discovery) is the easiest way to monitor all routes at once. Use `FastAPIRouteProbe` directly when a specific route needs its own thresholds, description, or tags.

```python
from fastapi_watch import FastAPIRouteProbe

users_probe = FastAPIRouteProbe(
    name="users-api",
    description="User listing endpoint",
    tags=["users"],
    max_error_rate=0.05,
    max_avg_rtt_ms=300,
)

@app.get("/users")
@users_probe.watch
async def list_users():
    return {"users": [...]}

registry.add(users_probe)
```

Works with both `async def` and `def`. Preserves function signature — FastAPI dependency injection continues to work.

**Metrics:**

| Metric | Description |
|--------|-------------|
| `request_count` | Total requests observed |
| `error_count` | Requests at or above `min_error_status` (lifetime total) |
| `error_rate` | Fraction of the last `window_size` requests that were errors — health is judged on this rolling window, not the lifetime count |
| `consecutive_errors` | Unbroken run of failures |
| `last_status_code` | HTTP status of the most recent request |
| `last_rtt_ms` | RTT for the most recent request |
| `avg_rtt_ms` | Exponential moving average RTT |
| `p50_rtt_ms` | Median RTT over the last `window_size` requests |
| `p95_rtt_ms` | 95th-percentile RTT over the last `window_size` requests |
| `p99_rtt_ms` | 99th-percentile RTT over the last `window_size` requests |
| `min_rtt_ms` / `max_rtt_ms` | All-time bounds |
| `requests_per_minute` | Throughput from the timestamp window; `null` until ≥2 requests |
| `slow_calls` | Requests exceeding `slow_call_threshold_ms` in the last `window_size` requests (only when threshold is set) |
| `status_distribution` | Count per HTTP status family (`2xx`/`3xx`/`4xx`/`5xx`) over the last `cache_window_size` requests |
| `error_types` | Count per exception class over the last `cache_window_size` errors (exceptions only, not status-code-only errors) |
| `cache_hits` / `cache_misses` | Hit and miss counts over the last `cache_window_size` lookups or `cache_time_window_s` seconds (auto-tracked when `@probe.watch` wraps an `@lru_cache` / `@alru_cache` function; also populated manually via `record_cache_hit()` / `record_cache_miss()`) |
| `cache_maxsize` / `cache_currsize` | LRU cache capacity and current fill level (reported when auto-tracking is active) |
| `last_error` | The most recent error: the exception type (`"HTTPException"`, `"IntegrityError"`), or `"HTTP 503"` for an error status returned without raising. The message is not included unless `expose_error_messages=True` |
| `last_error_at` | ISO timestamp of the most recent error; always shown once any error has occurred |
| `last_success_at` | ISO timestamp of the most recent success; shown only when ≥99% of the outcome window are failures |

Timestamps (`last_error_at`, `last_success_at`) are rendered in the dashboard as small subdued text above the details table, not as table rows.

**Health thresholds:**
- `max_error_rate` (default `0.1`) — UNHEALTHY if exceeded.
- `max_avg_rtt_ms` (default `None`) — UNHEALTHY if exceeded.
- `min_error_status` (default `500`) — only 5xx count as errors by default. Set to `400` to include 4xx.

Before the first request, `check()` returns `HEALTHY` with `"no requests observed yet"` — a fresh deployment never starts out unhealthy.

**Constructor args:**

| Argument | Default | Description |
|----------|---------|-------------|
| `name` | `"route"` | Probe label |
| `description` | `None` | Human-readable subtitle shown in the dashboard card header |
| `tags` | `[]` | Tags for filtering `/health/ready?tag=...` and `/health/status?tag=...` |
| `max_error_rate` | `0.1` | Error-rate threshold |
| `max_avg_rtt_ms` | `None` | RTT threshold |
| `window_size` | `100` | Recent requests used for `error_rate`, RTT percentiles, throughput, and slow-call counts (a count, not a time span) |
| `ema_alpha` | `0.1` | EMA smoothing factor |
| `timeout` | `None` | Passed to registry for `check()` |
| `min_error_status` | `500` | Minimum status code counted as an error |
| `slow_call_threshold_ms` | `None` | Requests above this threshold increment `slow_calls`; disabled when `None` |
| `cache_window_size` | `None` | Override the cache hit/miss window to the last N lookups; auto-detected from the LRU cache's `maxsize` when not set |
| `cache_time_window_s` | `120.0` | When `cache_window_size` is not set, count hits/misses over the last T seconds (default 120 s) |
| `cache_reporting` | `True` | Set to `False` to disable automatic cache tracking even when wrapping `@lru_cache` / `@alru_cache` functions |
| `circuit_breaker_enabled` | `False` | Passive probes only read in-memory stats, so the circuit breaker is off by default; set `True` to opt in |
| `expose_error_messages` | `False` | Include the exception message in `last_error` (`"HTTPException: 503: <detail>"`). Messages can carry credentials, connection strings, or the bound parameters of a failed query, so leave this off unless the health endpoints require `auth` |

Out-of-range settings raise `ValueError` when the probe is created: `window_size` and `cache_window_size` must be positive, `ema_alpha` must be in (0, 1], and `max_error_rate` in [0, 1]. The same rules apply to every passive probe and to `FastAPIWebSocketProbe`.

**With ProbeGroup:**

```python
# features/users/probes.py
from fastapi_watch import ProbeGroup, FastAPIRouteProbe

router = ProbeGroup()
users_probe = FastAPIRouteProbe(name="users-api")
router.add(users_probe)
```

```python
# features/users/routes.py
from fastapi import APIRouter
from .probes import users_probe

api = APIRouter(prefix="/users")

@api.get("/")
@users_probe.watch
async def list_users():
    ...
```

---

### WebSocket probe

`FastAPIWebSocketProbe` passively monitors a WebSocket handler.

```python
from fastapi_watch import FastAPIWebSocketProbe
from fastapi import WebSocket

chat_probe = FastAPIWebSocketProbe(name="chat", max_error_rate=0.05)

@app.websocket("/ws/chat")
@chat_probe.watch
async def chat(websocket: WebSocket):
    await websocket.accept()
    while True:
        msg = await websocket.receive_text()
        await websocket.send_text(msg)

registry.add(chat_probe)
```

`WebSocketDisconnect` is treated as a normal close, never counted as an error.

**Metrics:** `active_connections`, `total_connections`, `messages_received`, `messages_sent`, `error_count`, `error_rate`, `consecutive_errors`, `avg_duration_ms`, `min_duration_ms`, `max_duration_ms`.

**Health thresholds:**
- `max_error_rate` (default `0.1`)
- `min_active_connections` (default `0`, disabled) — UNHEALTHY if fewer than N sockets are open.

---

### Event loop lag

`EventLoopProbe` measures asyncio event loop blocking. Its first check starts a lightweight background sampler that sleeps for `sample_interval_ms` (default 100, at least 1) and records how late it wakes up, so stalls that happen *between* checks are caught — a single measurement at check time only runs once the loop is free again.

```python
from fastapi_watch.probes import EventLoopProbe

registry.add(EventLoopProbe(warn_ms=5.0, fail_ms=20.0))
```

Details: `lag_ms` (worst lag since the previous check), `current_lag_ms` (an immediate measurement), `warn_ms`, `fail_ms`. Returns DEGRADED at `warn_ms` or above, UNHEALTHY at `fail_ms` or above.

`await probe.aclose()` cancels the sampler; the registry calls it at shutdown (and `ThresholdProbe` passes it on to the probe it wraps). A later check starts a new sampler.

---

### TCP / DNS reachability

`TCPProbe` resolves a hostname and opens a TCP connection. No extra install.

```python
from fastapi_watch.probes import TCPProbe

registry.add(TCPProbe(host="db.internal", port=5432))
registry.add(TCPProbe(host="redis.internal", port=6379, name="redis-tcp", timeout=2.0))
```

Details: `host`, `port`, `resolved_ips`, `connect_ms`.

The name is resolved once per check, then the probe connects to the resolved addresses in order until one accepts. The lookup is a blocking call, so it runs in fastapi-watch's own small thread pool (see [Blocking checks](#blocking-checks-and-the-thread-pool)); if a DNS server hangs past the probe's deadline, later checks report `"previous check still running"` until that lookup returns.

The probe closes each connection without speaking the server's protocol, and some servers count that as a failed client. PostgreSQL logs `incomplete startup packet` on every check. MySQL and MariaDB count it toward `max_connect_errors` (default 100) and then **block the host**, including your application's new connections from it, until `FLUSH HOSTS`. Don't point `TCPProbe` at a MySQL port; observe real queries with `MySQLProbe` instead.

---

### SMTP

`SMTPProbe` passively monitors outgoing email calls via `@probe.watch`. Works with any SMTP library.

```python
from fastapi_watch.probes import SMTPProbe

smtp_probe = SMTPProbe(name="sendgrid", max_error_rate=0.05)

@smtp_probe.watch
async def send_welcome_email(to: str) -> None:
    async with aiosmtplib.SMTP("smtp.sendgrid.net", port=587) as smtp:
        await smtp.login("apikey", SENDGRID_API_KEY)
        await smtp.sendmail(FROM, to, message.as_string())

registry.add(smtp_probe)
```

Details: `call_count`, `error_count`, `error_rate`, `consecutive_errors`, `last_rtt_ms`, `avg_rtt_ms`, `p50_rtt_ms`, `p95_rtt_ms`, `p99_rtt_ms`, `min_rtt_ms`, `max_rtt_ms`, `error_types`, `last_error_at` (always when errors exist), `last_success_at` (when ≥99% failures).

---

### Threshold wrapper

`ThresholdProbe` wraps any probe and promotes its result to DEGRADED or UNHEALTHY based on values in `details` — without modifying the inner probe.

```python
from fastapi_watch.probes import ThresholdProbe, RedisProbe

redis_probe = RedisProbe(name="redis")  # decorate your Redis calls with @redis_probe.watch

registry.add(ThresholdProbe(
    redis_probe,
    name="redis-latency",
    warn_if=lambda d: d.get("p95_rtt_ms", 0) > 50,
    fail_if=lambda d: d.get("p95_rtt_ms", 0) > 200,
))
```

If the inner probe returns UNHEALTHY, it passes through unchanged. `fail_if` takes precedence over `warn_if`.

---

### PostgreSQL

```bash
pip install "fastapi-watch[postgres]"
```

`PostgreSQLProbe` passively monitors PostgreSQL calls via `@probe.watch`.

```python
from fastapi_watch.probes import PostgreSQLProbe

pg_probe = PostgreSQLProbe(name="primary-db", max_error_rate=0.01)

@pg_probe.watch
async def get_user(user_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)

registry.add(pg_probe)
```

---

### MySQL / MariaDB

```bash
pip install "fastapi-watch[mysql]"
```

```python
from fastapi_watch.probes import MySQLProbe

mysql_probe = MySQLProbe(name="mysql", max_error_rate=0.01)

@mysql_probe.watch
async def get_product(product_id: int):
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT * FROM products WHERE id = %s", (product_id,))
            return await cur.fetchone()

registry.add(mysql_probe)
```

---

### Redis

```bash
pip install "fastapi-watch[redis]"
```

```python
from fastapi_watch.probes import RedisProbe

redis_probe = RedisProbe(name="cache", max_error_rate=0.05)

@redis_probe.watch
async def get_session(session_id: str):
    return await redis_client.hgetall(f"session:{session_id}")

registry.add(redis_probe)
```

---

### Memcached

```bash
pip install "fastapi-watch[memcached]"
```

```python
from fastapi_watch.probes import MemcachedProbe

registry.add(MemcachedProbe(host="localhost", port=11211))
```

---

### RabbitMQ

```bash
pip install "fastapi-watch[rabbitmq]"
```

```python
from fastapi_watch.probes import RabbitMQProbe

# Connectivity only
registry.add(RabbitMQProbe(url="amqp://guest:guest@localhost:5672/"))

# Rich mode — with Management API (queue stats, message rates)
registry.add(RabbitMQProbe(
    url="amqp://guest:guest@localhost:5672/",
    management_url="http://localhost:15672",
))
```

If `management_url` is unreachable, the probe still reports AMQP connectivity. `queues` lists at most the 50 queues holding the most messages (the API pages and sorts them, so the broker does not serialize every queue on each check); `server.queues` is the total. Percent-encoded credentials in `url` are decoded before they are sent to the Management API, as aio-pika does for AMQP.

---

### Kafka

```bash
pip install "fastapi-watch[kafka]"
```

```python
from fastapi_watch.probes import KafkaProbe

registry.add(KafkaProbe(bootstrap_servers="localhost:9092"))
registry.add(KafkaProbe(bootstrap_servers=["b1:9092", "b2:9092", "b3:9092"]))
```

Details: `broker_count`, `controller_id`, `topic_count`, `topics`, `internal_topics`. At most 100 topic names are listed; `topic_count` is the full number of non-internal topics.

---

### MongoDB

```bash
pip install "fastapi-watch[mongo]"
```

```python
from fastapi_watch.probes import MongoProbe

mongo_probe = MongoProbe(name="mongodb", max_error_rate=0.02)

@mongo_probe.watch
async def get_document(doc_id: str):
    return await db.documents.find_one({"_id": doc_id})

registry.add(mongo_probe)
```

---

### HTTP calls

`HttpProbe` passively monitors outgoing HTTP calls — no synthetic requests.

```python
from fastapi_watch.probes import HttpProbe

stripe_probe = HttpProbe(name="stripe", max_error_rate=0.05, max_avg_rtt_ms=500)

@stripe_probe.watch
async def charge_customer(amount: int, currency: str):
    async with aiohttp.ClientSession() as session:
        async with session.post("https://api.stripe.com/v1/charges",
                                json={"amount": amount, "currency": currency}) as r:
            r.raise_for_status()
            return await r.json()

registry.add(stripe_probe)
```

---

### Celery workers

```bash
pip install "fastapi-watch[celery]"
```

```python
from celery_app import celery
from fastapi_watch.probes import CeleryProbe

registry.add(CeleryProbe(celery, min_workers=2))           # ping only
registry.add(CeleryProbe(celery, min_workers=1, detailed=True))  # full inspection
```

`min_workers=0` (default) treats zero online workers as healthy — suitable for scale-to-zero.

With `detailed=True`, each worker lists its `active`, `reserved`, and `scheduled` tasks by `id`, `name`, `time_start`, and `worker_pid`. Task `args` and `kwargs` are left out: they are application data (email addresses, tokens, personal data) and health endpoints are often public. Pass `include_task_args=True` to include them where the endpoints require `auth`. Each list holds at most 50 tasks; when one is cut, the worker's `truncated` dict gives the number left out (e.g. `{"active": 70}`), while `active_tasks` / `reserved_tasks` / `scheduled_tasks` stay the full counts.

The inspect calls are blocking, so they run in fastapi-watch's own thread pool (see below). If the broker hangs past the probe's deadline, later checks report `"previous check still running"` instead of starting another inspect.

#### Blocking checks and the thread pool

`CeleryProbe`, `TCPProbe` (DNS lookup), and the built-in alerters make blocking calls. The probes run them in a private pool of at most 4 daemon threads named `fastapi-watch_N`, and the alerters in a second one of the same size named `fastapi-watch-alert_N`, so a burst of alerts to a slow endpoint cannot hold up the checks. The built-in dashboard renders on a third, single thread (`fastapi-watch-render_0`), so hung checks never delay a page load. None is the event loop's default executor, which your application's own `run_in_executor` calls use. The registry's timeout cancels a check but cannot stop a thread that is already blocked, so each probe has at most one blocking call in flight: while it is still running, the probe reports UNHEALTHY with `"previous check still running"` and submits nothing new. A hung dependency therefore holds one thread, not one per poll, and the threads never delay interpreter exit.

**Known limit: DNS for Kafka, RabbitMQ and Memcached.** `KafkaProbe`, `RabbitMQProbe` and `MemcachedProbe` connect by hostname through their async client libraries (aiokafka, aio-pika, aiomcache). Those libraries resolve the name with the event loop's `getaddrinfo`, which runs in the loop's default executor, the one your application shares, not in fastapi-watch's private pool. fastapi-watch can't resolve the name first and pass an IP, because that would break TLS hostname verification. The probe's `timeout` still bounds each check, but a resolver that hangs can hold a default-executor thread until the system resolver gives up. If that matters, point these probes at hosts that resolve locally (`/etc/hosts`, a node-local DNS cache), or size the default executor with `loop.set_default_executor()`.

---

### SQLAlchemy

```bash
pip install "fastapi-watch[sqlalchemy]"
```

```python
from fastapi_watch.probes import SqlAlchemyProbe

db_probe = SqlAlchemyProbe(name="postgres", max_error_rate=0.01)

@db_probe.watch
async def get_user(user_id: int):
    async with async_session() as session:
        return await session.get(User, user_id)

registry.add(db_probe)
```

---

### All built-in probes (summary table)

#### Application / infrastructure

| Probe | Extra | Key args | Details fields |
|-------|-------|----------|----------------|
| `RequestMetricsMiddleware` + `RequestMetricsProbe` | built-in | `per_route`, `max_routes`, `max_error_rate`, `max_avg_rtt_ms` | `request_count`, `error_count`, `error_rate`, `avg_rtt_ms`, `consecutive_errors`; + `routes` when `per_route=True` |
| `FastAPIRouteProbe` | built-in | `name`, `max_error_rate`, `max_avg_rtt_ms`, `min_error_status`, `slow_call_threshold_ms`, `cache_window_size`, `cache_time_window_s`, `cache_reporting`, `circuit_breaker_enabled`, `expose_error_messages` | `request_count`, `error_count`, `error_rate`, `consecutive_errors`, `last_status_code`, `last_rtt_ms`, `avg_rtt_ms`, `p50_rtt_ms`, `p95_rtt_ms`, `p99_rtt_ms`, `min_rtt_ms`, `max_rtt_ms`, `requests_per_minute`, `slow_calls`\*, `status_distribution`, `error_types`, `cache_hits`\*, `cache_misses`\*, `cache_maxsize`\*, `cache_currsize`\*, `last_error_at`\*, `last_success_at`\* |
| `FastAPIWebSocketProbe` | built-in | `name`, `max_error_rate`, `min_active_connections` | `active_connections`, `total_connections`, `messages_received`, `messages_sent`, `error_count`, `error_rate`, `consecutive_errors`, `avg_duration_ms` |
| `EventLoopProbe` | built-in | `warn_ms`, `fail_ms`, `sample_interval_ms` | `lag_ms`, `current_lag_ms` |
| `TCPProbe` | built-in | `host`, `port`, `timeout` | `host`, `port`, `resolved_ips`, `connect_ms` |
| `SMTPProbe` | built-in | `name`, `max_error_rate`, `max_avg_rtt_ms`, `slow_call_threshold_ms`, `cache_window_size`, `expose_error_messages` | `call_count`, `error_count`, `error_rate`, `avg_rtt_ms`, `p50_rtt_ms`, `p95_rtt_ms`, `p99_rtt_ms`, `error_types`, `last_error_at`\* |
| `ThresholdProbe` | built-in | `probe`, `warn_if`, `fail_if` | delegates to inner probe |

#### Databases

| Probe | Extra | Details |
|-------|-------|---------|
| `PostgreSQLProbe` | `postgres` | `call_count`, `error_count`, `error_rate`, `avg_rtt_ms`, `p50_rtt_ms`, `p95_rtt_ms`, `p99_rtt_ms`, `error_types`, `last_error_at`\* |
| `MySQLProbe` | `mysql` | same |
| `SqlAlchemyProbe` | `sqlalchemy` | same |

#### Caches

| Probe | Extra | Details |
|-------|-------|---------|
| `RedisProbe` | `redis` | `call_count`, `error_count`, `error_rate`, `avg_rtt_ms`, `p50_rtt_ms`, `p95_rtt_ms`, `p99_rtt_ms`, `error_types`, `last_error_at`\* |
| `MemcachedProbe` | `memcached` | — |

#### Queues / messaging

| Probe | Extra | Key args | Details |
|-------|-------|----------|---------|
| `RabbitMQProbe` | `rabbitmq` | `url`, `management_url` | `connected`; + `server`, `totals`, `queues` when `management_url` set |
| `KafkaProbe` | `kafka` | `bootstrap_servers` | `broker_count`, `controller_id`, `topic_count`, `topics` (first 100) |
| `CeleryProbe` | `celery` | `app`, `min_workers`, `detailed`, `include_task_args` | `workers_online`, `workers` |

#### HTTP / document stores

| Probe | Extra | Details |
|-------|-------|---------|
| `HttpProbe` | built-in | `call_count`, `error_count`, `error_rate`, `avg_rtt_ms`, `p50_rtt_ms`, `p95_rtt_ms`, `p99_rtt_ms`, `error_types`, `last_error_at`\* |
| `MongoProbe` | `mongo` | same |

\* Conditional: `last_error_at` appears once any error has occurred; `last_success_at` appears only when ≥99% of the outcome window are failures; `slow_calls` only when `slow_call_threshold_ms` is set; `cache_hits`/`cache_misses` only when `record_cache_hit()`/`record_cache_miss()` have been called.

#### Testing / placeholder

| Probe | Extra | Details |
|-------|-------|---------|
| `NoOpProbe` | built-in | — |

---

## Alerters

Alerters fire on every probe state change. Register one or more at startup:

```python
from fastapi_watch.alerts import SlackAlerter, PagerDutyAlerter, OpsGenieAlerter

registry = HealthRegistry(
    app,
    alerters=[
        SlackAlerter(webhook_url="https://hooks.slack.com/services/..."),
        PagerDutyAlerter(routing_key="your-routing-key"),
        OpsGenieAlerter(api_key="your-api-key"),
    ],
)
```

Alerters are called concurrently from a background worker queue. A failed alerter never blocks health checks or silences other alerters. At shutdown, alerts already queued are still sent (for up to 5 s) before the worker stops.

The built-in alerters post from fastapi-watch's own alert thread pool (see [Blocking checks](#blocking-checks-and-the-thread-pool)), and `timeout` bounds the whole request — connecting, sending, and waiting for the response status — not just each socket read, so an endpoint that answers slowly cannot hold a thread past it. A host that resolves to several unreachable addresses shares that one budget across them. The request then fails with `TimeoutError`, which is logged like any other alerter failure. Resolving the webhook's hostname is bounded only by the system resolver's own timeouts.

### Flap limiting and multi-replica dedupe

Both are off by default.

```python
registry = HealthRegistry(
    app,
    alerters=[PagerDutyAlerter(routing_key="...")],
    storage=my_redis_storage,       # shared by every replica
    alert_min_interval_s=300,       # at most one alert per probe per 5 minutes
    alert_dedupe_window_s=120,      # one replica sends each alert
)
```

**`alert_min_interval_s`** limits each probe to one alert per interval. The first change after a quiet interval alerts immediately. Changes within the interval are coalesced; when it ends, the probe's latest status is sent if it differs from the last status alerters received. The final state is never lost (it is sent even if the probe is not checked again), and a flap that ends where it started (`healthy -> unhealthy -> healthy`) sends nothing.

**`alert_dedupe_window_s`** stops every replica from sending the same alert. Before sending, a replica calls the storage backend's `claim_alert(key, value, ttl_seconds)` with the probe name as key and the new status as value. The backend records the last status announced for that probe and refuses the claim only when it already holds the same status, so the first replica to see each change sends it and the others treat the alert as sent. A probe that fails, recovers and fails again inside the window alerts each time. Set the window to roughly how far apart replicas notice the same change (about one poll interval). If the claim fails or exceeds `storage_timeout`, the replica sends anyway, since a duplicate alert is better than none. With a backend that lacks `claim_alert` (including the default in-process storage), a warning is logged at startup and alerts are not deduplicated.

`on_state_change` callbacks and `/health/alerts` records are unaffected by both settings and still see every transition.

### `SlackAlerter`

Posts a formatted message to a Slack Incoming Webhook.

```python
SlackAlerter(
    webhook_url="https://hooks.slack.com/services/...",
    channel="#ops-alerts",   # optional channel override
    username="fastapi-watch", # bot display name
    timeout=5.0,
)
```

### `PagerDutyAlerter`

Triggers a PagerDuty incident on UNHEALTHY (severity `error`), warning on DEGRADED (severity `warning`), and resolves on HEALTHY. Uses `dedup_key = fastapi-watch:<probe-name>` to prevent duplicate incidents.

```python
PagerDutyAlerter(routing_key="your-32-char-routing-key", source="fastapi-watch")
```

### `OpsGenieAlerter`

Creates an OpsGenie alert on UNHEALTHY (P1) or DEGRADED (P3) and closes it on HEALTHY. Uses `alias = fastapi-watch:<probe-name>` for deduplication. Supports US and EU API regions.

```python
OpsGenieAlerter(api_key="your-api-key")                  # US region (default)
OpsGenieAlerter(api_key="your-api-key", region="eu")     # EU region
```

### `TeamsAlerter`

Posts an Adaptive Card to a Microsoft Teams Incoming Webhook.

```python
TeamsAlerter(webhook_url="https://outlook.office.com/webhook/...")
```

### `WebhookAlerter`

POSTs a JSON payload to any HTTP(S) endpoint.

```python
WebhookAlerter(
    url="https://my-service.example.com/hooks/health",
    headers={"Authorization": "Bearer my-secret-token"},
    timeout=5.0,
)
```

Payload: `{"probe", "old_status", "new_status", "timestamp"}`.

### Custom alerters

```python
from fastapi_watch.alerts import BaseAlerter
from fastapi_watch.models import AlertRecord

class SMSAlerter(BaseAlerter):
    async def notify(self, alert: AlertRecord) -> None:
        message = f"{alert.probe}: {alert.old_status.value} → {alert.new_status.value}"
        # send via your SMS provider
```

---

## Probe introspection

`GET /health/probes` lists all registered probes and their configuration **without running any checks**. Useful for verifying what the registry is monitoring and its per-probe settings.

```bash
curl http://localhost:8000/health/probes
```

```json
{
  "probes": [
    {
      "name": "postgresql",
      "critical": true,
      "tags": ["database"],
      "description": "GET /users/{id}",
      "poll_interval_ms": 30000,
      "circuit_breaker_enabled": true
    }
  ]
}
```

| Field | Description |
|-------|-------------|
| `name` | Probe identifier |
| `critical` | Whether the probe affects overall readiness status |
| `tags` | Tags assigned to this probe |
| `description` | Human-readable subtitle (may be `null`) |
| `poll_interval_ms` | Per-probe poll override; `null` means the probe inherits the registry default |
| `circuit_breaker_enabled` | Whether this probe participates in circuit breaker logic |

The endpoint is subject to the same `auth` as all other health endpoints.

---

## Configuration reference

### `HealthRegistry`

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `app` | `FastAPI` | required | FastAPI application instance |
| `prefix` | `str` | `"/health"` | URL prefix for all health endpoints |
| `tags` | `list[str]` | `["health"]` | OpenAPI tags applied to all routes |
| `poll_interval_ms` | `int \| None` | `60000` | Re-run interval (ms) while SSE clients are connected. `0`/`None` = on-demand. Min `1000`. |
| `logger` | `logging.Logger \| None` | `None` | Logger for warnings and probe exceptions |
| `grace_period_ms` | `int` | `0` | Hold `/ready` as "starting" for this many ms after startup |
| `history_size` | `int` | `120` | Max results per probe. With a custom `storage`, sent to its `configure()` (with the next three) only when at least one of the four is passed; otherwise the backend keeps its own values. See [Retention settings](#retention-settings) |
| `result_ttl_seconds` | `float` | `7200.0` | How long results are retained |
| `alert_ttl_seconds` | `float` | `259200.0` | How long alert records are retained |
| `max_alerts` | `int` | `120` | Hard cap on stored alerts |
| `storage` | `ProbeStorage \| None` | `None` | Storage backend; in-memory when `None`. See [Custom storage backends](#custom-storage-backends). Retention arguments you pass are applied through its `configure()`; without one, a warning names the ignored arguments. Its optional `forget()` only ever receives names this process wrote, so a backend can be shared |
| `storage_timeout` | `float \| None` | `2.0` | Keyword-only. Seconds each storage call may take. Failures and timeouts are logged and never fail a health check; `None` disables the limit |
| `max_stream_connections` | `int \| None` | `100` | Keyword-only. Most SSE streams open at once; more get `503` with `Retry-After`. `None` = unlimited |
| `alert_min_interval_s` | `float` | `0.0` | Keyword-only. At most one alert per probe per this many seconds; changes in between are coalesced and the latest status is sent when the interval ends. `0` = off |
| `alert_dedupe_window_s` | `float \| None` | `None` | Keyword-only. With a shared storage that implements `claim_alert()`, only one replica sends each alert within this window. `None` = off |
| `stream_token_secret` | `str \| bytes \| None` | `None` | Keyword-only. Key that signs dashboard stream tokens. Default: derived from `auth` credentials (scrypt), or per-process random for a callable `auth` |
| `probe_timeout` | `float \| None` | `30.0` | Seconds before a probe without its own `timeout` is reported UNHEALTHY. `None` disables the default |
| `metrics_prefix` | `str` | `""` | Prepended to every `/metrics` name. The default `probe_*` names collide with blackbox_exporter's; `"fastapi_watch_"` namespaces them |
| `timezone` | `str` | `"UTC"` | IANA timezone for `checked_at` timestamps |
| `groups` | `list[ProbeGroup] \| None` | `None` | ProbeGroups to include at startup |
| `dashboard` | `bool \| Callable \| Path` | `True` | `True` = built-in dashboard; `False` = disabled; Callable = custom renderer `(report) -> str` (or `(report, maintenance) -> str` to also receive the maintenance flag); `.html`/`.htm` file path = served as-is. `.py` files are rejected. |
| `serve_routes` | `bool` | `True` | `True` = register all health endpoints on the app. `False` = skip route registration entirely — use `get_report()` to expose status on your own endpoint. |
| `include_in_schema` | `bool` | `False` | Keyword-only. List the health endpoints in the app's OpenAPI schema and `/docs`. Hidden by default: they are operational endpoints, not part of your API. |
| `circuit_breaker` | `bool` | `True` | Enable/disable circuit breaker |
| `circuit_breaker_threshold` | `int` | `5` | Consecutive failures before circuit opens |
| `circuit_breaker_cooldown_ms` | `int` | `600000` | Cooldown before probe is retried (10 min) |
| `webhook_url` | `str \| None` | `None` | Legacy — wraps into `WebhookAlerter` |
| `auth` | `dict \| Callable \| None` | `None` | Authentication for all health endpoints. `None` logs a warning — endpoints are publicly accessible. A callable must return exactly `True` to grant access; `None`, `0`, `""`, or any other truthy value is denied. An empty or whitespace `key`, `username` or `password` raises `ValueError`. |
| `startup_probes` | `list[BaseProbe] \| None` | `None` | Probes required for `/health/startup` to pass |
| `alerters` | `list[BaseAlerter] \| None` | `None` | Alerters to fire on state changes |

### `HealthRegistry` methods

| Method | Description |
|--------|-------------|
| `add(probe, critical=True)` | Add a single probe. Returns `self`. |
| `add_probes(probes, critical=True)` | Add multiple probes. Returns `self`. |
| `include(router)` | Include all probes from a `ProbeGroup`. Returns `self`. |
| `on_state_change(callback)` | Register a state-change callback. Returns `self`. |
| `set_poll_interval(ms)` | Change poll interval at runtime. |
| `set_started()` | Signal startup complete — unblocks `/health/startup`. |
| `set_maintenance(until=None)` | Activate maintenance mode. Returns `self`. |
| `clear_maintenance()` | Deactivate maintenance mode. Returns `self`. |
| `reset_circuit(probe_name)` | Manually reset a probe's open circuit and failure count. Does not clear the lifetime trips counter. Returns `self`. |
| `run_all()` | Async — run all probes concurrently, return `list[ProbeResult]`. |
| `get_report()` | Async — run probes and return a `HealthReport`. Intended for use with `serve_routes=False`. |

### `BaseProbe` attributes

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | `"unnamed"` | Label in health reports. Must contain only `[A-Za-z0-9_\-.:]+` — spaces, quotes, and newlines are rejected at `registry.add()` time. |
| `timeout` | `float \| None` | `None` | Per-probe timeout in seconds. `None` uses the registry's `probe_timeout` (30 s by default) |
| `poll_interval_ms` | `int \| None` | `None` | Per-probe poll interval override |
| `circuit_breaker_threshold` | `int \| None` | `None` | Per-probe circuit threshold |
| `circuit_breaker_cooldown_ms` | `int \| None` | `None` | Per-probe circuit cooldown |
| `aclose()` | async method | no-op | Called once at registry shutdown (5 s limit); override to release clients or background tasks. See [Releasing resources](#releasing-resources) |

### `ProbeResult` fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` | Probe identifier |
| `status` | `ProbeStatus` | `"healthy"`, `"degraded"`, `"unhealthy"`, or `"no_data"` (nothing observed yet) |
| `critical` | `bool` | Affects overall status and readiness |
| `latency_ms` | `float` | Check duration in milliseconds |
| `error` | `str \| None` | Error message on failure |
| `details` | `dict \| None` | Service-specific metadata |
| `is_healthy` | `bool` | `status == "healthy"` |
| `is_degraded` | `bool` | `status == "degraded"` |
| `is_passing` | `bool` | `status != "unhealthy"` |

---

## Security

### Authentication

Health endpoints are publicly accessible by default (`auth=None`). A warning is logged at startup to make this explicit. In production, always set `auth`:

```python
# HTTP Basic auth
registry = HealthRegistry(app, auth={"type": "basic", "username": "ops", "password": "secret"})

# API key header (default header: X-API-Key)
registry = HealthRegistry(app, auth={"type": "apikey", "key": "my-secret-key", "header": "X-API-Key"})

# Custom callable — must return exactly True to grant access
HEALTH_KEY = "load-from-your-secrets-manager"

def my_auth(request: Request) -> bool:
    return request.headers.get("X-Internal-Key") == HEALTH_KEY

registry = HealthRegistry(app, auth=my_auth)
```

The auth callable **must return exactly `True`** (the boolean). Returning `None`, `0`, `""`, or any other truthy value is treated as denial and returns `403`. This prevents accidental access grants from callables that return a user object or empty string on failure.

Comparison of static credentials uses `secrets.compare_digest` to prevent timing attacks. An empty or whitespace-only `key`, `username` or `password` raises `ValueError` at startup (an empty API key would otherwise match a request that simply omits the header).

**Dashboard live updates under auth.** A browser `EventSource` cannot send headers, so the authenticated dashboard page embeds a short-lived signed token (`?token=…`, valid 5 minutes, checked when the stream connects) in its stream URL. `/health/status/stream` and `/health/ready/stream` accept either that token or normal credentials; no other endpoint accepts it, and the credentials themselves never appear in the page. For `basic` and `apikey` auth the signing key is derived from the configured credentials with scrypt (once, at startup), so tokens work across worker processes and replicas, and a token that turns up in an access log is expensive to use for guessing the credentials. A callable `auth` has no shared secret, so by default its tokens only validate on the process that issued them. With several workers behind a load balancer, pass the same `stream_token_secret=` to every one of them (recommended), use `basic`/`apikey`, or route the dashboard with session affinity. `stream_token_secret` also replaces the credential-derived key for `basic`/`apikey`.

A stream authorized by a token closes when the token expires; the reconnect is refused, and the page fetches a fresh token from `GET /health/dashboard/stream-url` and reconnects without reloading (it falls back to a reload if that request fails, e.g. once the browser's credentials are gone). Streams authorized by headers or credentials are not time-limited. `GET /health/dashboard/stream-url` (built-in dashboard with `auth` only) returns a stream URL with a fresh token, `{"stream_url": "...", "expires_at": <unix time>}`; it requires the normal credentials and does not accept a stream token, so a leaked token cannot renew itself.

The dashboard page is sent with `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` and a `Content-Security-Policy` with `frame-ancestors 'none'`, since it can carry a token. That is the whole policy for a custom dashboard; the built-in page's is strict (see [Health dashboard](#health-dashboard)). The built-in page is rendered on fastapi-watch's own render thread, one page at a time, so a long history does not block the app's event loop.

### Response fields and information disclosure

Health responses include probe names in `probes[*].name` and, when probes are failing, in the top-level `failing` array. This means an unauthenticated caller of `/health/ready` on a 503 response can read the names of currently-failing critical infrastructure components (e.g., `"postgres-primary"`, `"redis-session"`). This is intentional — the names are also present in the `probes` array — but it makes enumeration more direct.

If your infrastructure naming is sensitive, configure `auth` so health endpoints are not publicly accessible.

### Webhook alerters and SSRF

`WebhookAlerter`, `SlackAlerter`, and `TeamsAlerter` validate the webhook URL at construction time. URLs targeting private/loopback/link-local IP ranges are rejected with a `ValueError`. `PagerDutyAlerter` and `OpsGenieAlerter` use hardcoded API endpoints and are not affected by URL validation.

```python
# Raises ValueError — private IP rejected at startup
SlackAlerter(webhook_url="https://192.168.1.10/hook")

# Fine — public HTTPS endpoint
SlackAlerter(webhook_url="https://hooks.slack.com/services/...")
```

Rejected: any IP literal that is not publicly routable unicast — private, loopback, link-local (`169.254.0.0/16`, cloud metadata), carrier-grade NAT (`100.64.0.0/10`), unspecified (`0.0.0.0`, `::`), multicast, and reserved ranges — in every spelling a resolver accepts, including IPv4-mapped IPv6 (`::ffff:127.0.0.1`) and integer/octal/hex forms (`2130706433`, `0x7f000001`, `0177.0.0.1`), plus `localhost` / `*.localhost` (with or without a trailing dot). Alerters never follow redirects, so a validated URL cannot hand the request on to an internal address.

This is a construction-time check without DNS resolution: a public hostname that *resolves* to a private address is not caught. Restrict outbound traffic at the network layer if that matters in your environment.

Alerters are dispatched concurrently per state change, each bounded by a 30-second ceiling, so a slow or failing integration never delays the others; failures are logged with their traceback.

### Probe error messages

When a probe's check fails with an exception, HTTP clients see only the exception *type* — e.g. `"probe check failed: ConnectionRefusedError"` — so the failure is diagnosable from the dashboard without exposing the message. Full exception details (which may contain connection strings, passwords, or internal hostnames from third-party libraries) go only to logs: the configured `logger=` for exceptions escaping `check()`, and the `fastapi_watch` logger at debug level for failures the built-in probes catch themselves.

Passive probes follow the same rule for the calls they observe: `last_error` is the exception type (`"IntegrityError"`, `"HTTPException"`) or `"HTTP 503"`, never the message or an `HTTPException`'s `detail`, which can hold the bound parameters of a failed query or other personal data. The message is logged at debug level on the `fastapi_watch` logger. `expose_error_messages=True` opts a probe back in. Likewise `CeleryProbe(detailed=True)` omits task arguments unless `include_task_args=True`.

### Probe names

Probe names must match `[A-Za-z0-9_\-.:]+`. Names with spaces, newlines, or HTML-special characters are rejected at `registry.add()` time with a `ValueError`. This prevents injection into Prometheus metric labels and dashboard rendering.

### Disabling all health routes

If you need full control over authentication, path, or response shape, pass `serve_routes=False` to skip registering any health endpoints. Use `get_report()` to build your own:

```python
from fastapi import Depends, HTTPException, Header

HEALTH_KEY = "load-from-your-secrets-manager"

async def my_auth(x_api_key: str = Header(...)):
    if x_api_key != HEALTH_KEY:
        raise HTTPException(status_code=401)

registry = HealthRegistry(app, serve_routes=False)

@app.get("/internal/health", dependencies=[Depends(my_auth)])
async def my_health():
    report = await registry.get_report()
    return report.model_dump()
```

`get_report()` respects the same poll interval and cache logic as the built-in endpoints — probes are not re-run on every request when a poll interval is configured.

### Custom dashboard

`HealthRegistry(dashboard=...)` accepts `True` (built-in), `False` (disabled), a `Callable`, or a `.html`/`.htm` file path. `.py` files are explicitly rejected to prevent arbitrary code execution from a user-supplied file path.

---

## Kubernetes integration

```yaml
livenessProbe:
  httpGet:
    path: /health/live
    port: 8000
  initialDelaySeconds: 5
  periodSeconds: 10

readinessProbe:
  httpGet:
    path: /health/ready
    port: 8000
  initialDelaySeconds: 10
  periodSeconds: 15
  failureThreshold: 3

startupProbe:
  httpGet:
    path: /health/startup
    port: 8000
  failureThreshold: 30
  periodSeconds: 5
```

Use `/health/live` for liveness (only restart on genuine hang) and `/health/ready` for readiness (stop routing when dependencies are down). Use `/health/startup` to gate rollout until the application and its startup probes have passed.

With `auth=` set, every health endpoint needs credentials, the kubelet's probes included: without them liveness gets `401`/`403` and Kubernetes restarts the pod over and over. Send them with `httpGet.httpHeaders`, for example `{"type": "apikey", ...}` with:

```yaml
livenessProbe:
  httpGet:
    path: /health/live
    port: 8000
    httpHeaders:
      - name: X-API-Key
        value: "<the key>"   # better: inject it from a Secret into the pod spec
```

A `?probe=` or `?tag=` in a probe path must name registered probes: an unknown name gets `404`, which Kubernetes treats as a failed check.

For slow-starting applications:

```python
registry = HealthRegistry(app, grace_period_ms=30_000)
```

```yaml
readinessProbe:
  httpGet:
    path: /health/ready
    port: 8000
  initialDelaySeconds: 5
  periodSeconds: 10
  failureThreshold: 6  # allow up to 60 s of failures before marking unready
```
