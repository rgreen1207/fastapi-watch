<h1 align="center">fastapi-watch</h1>
<p align="center">
  <em>Structured health and readiness check system for <a href="https://fastapi.tiangolo.com/">FastAPI</a>.</em>
</p>
<p align="center">
  <a href="https://github.com/rgreen1207/fastapi-watch/actions/workflows/publish.yml">
    <img src="https://github.com/rgreen1207/fastapi-watch/actions/workflows/publish.yml/badge.svg" alt="Test, Build & Publish">
  </a>
  <a href="https://pypi.org/project/fastapi-watch/">
    <img src="https://img.shields.io/pypi/v/fastapi-watch" alt="PyPI version">
  </a>
  <a href="https://pypi.org/project/fastapi-watch/">
    <img src="https://img.shields.io/pypi/pyversions/fastapi-watch" alt="Supported Python versions">
  </a>
</p>

---

Add `/health/live`, `/health/ready`, `/health/status`, `/health/history`, `/health/metrics`, and `/health/dashboard` endpoints to any FastAPI app with a single registry call. All probes run concurrently, so a slow dependency never blocks the others. Each probe returns rich service-specific details alongside the pass/fail result.

Probes report one of three states: **healthy** (all clear), **degraded** (under stress but still serving traffic), or **unhealthy** (critical failure — stop routing traffic). A degraded critical probe returns 200 on `/health/ready` so load balancers keep sending requests while you investigate.

Instrument individual FastAPI route handlers with `FastAPIRouteProbe` to collect real-traffic metrics — latency percentiles, error rate, throughput, and consecutive failure counts — or attach `RequestMetricsMiddleware` to capture the same metrics for every route in your app without touching individual handlers.

Organize probes across your codebase using `ProbeRouter`, the same file-splitting pattern FastAPI uses for routes. Declare probes in any module, compose them into routers, and pass them to `HealthRegistry` in one line at startup.

Connect a browser or monitoring tool to the Server-Sent Events (SSE) streaming endpoints (`/health/ready/stream`, `/health/status/stream`) and receive live updates as long as you stay connected — the background poll loop starts automatically on the first connection and stops when the last client disconnects.

Open `/health/dashboard` for a live HTML page that shows all probe results, updates in real time over SSE, and requires no extra dependencies. Scrape `/health/metrics` for a Prometheus-compatible text export of every probe's health, latency, and circuit breaker state.

---

## Table of contents

- [Installation](#installation)
- [Quick start](#quick-start)
- [Endpoints](#endpoints)
- [Health Dashboard](#health-dashboard)
- [Probe management](#probe-management)
  - [Adding probes](#adding-probes)
  - [Critical vs non-critical probes](#critical-vs-non-critical-probes)
  - [Per-probe timeout](#per-probe-timeout)
- [ProbeRouter — organizing probes across files](#proberouter--organizing-probes-across-files)
- [Live streaming](#live-streaming)
- [Polling and caching](#polling-and-caching)
- [Per-probe poll frequency](#per-probe-poll-frequency)
- [Three-state health (DEGRADED)](#three-state-health-degraded)
- [Circuit breaker](#circuit-breaker)
- [Circuit breaker metrics](#circuit-breaker-metrics)
- [Maintenance mode](#maintenance-mode)
- [Prometheus metrics](#prometheus-metrics)
- [App-wide request metrics](#app-wide-request-metrics)
- [Webhook on state change](#webhook-on-state-change)
- [Authentication](#authentication)
- [Startup probe](#startup-probe)
- [State-change callbacks](#state-change-callbacks)
- [Startup grace period](#startup-grace-period)
- [Probe result history](#probe-result-history)
- [Alert history](#alert-history)
- [Custom storage backend](#custom-storage-backend)
- [Response format](#response-format)
- [Writing a custom probe](#writing-a-custom-probe)
- [Built-in probes](#built-in-probes)
  - [App-wide request metrics probe](#app-wide-request-metrics-probe)
  - [Watching a FastAPI route](#watching-a-fastapi-route)
  - [Watching a WebSocket handler](#watching-a-websocket-handler)
  - [Event loop lag](#event-loop-lag)
  - [Disk space](#disk-space)
  - [TCP / DNS reachability](#tcp--dns-reachability)
  - [SMTP](#smtp)
  - [Threshold wrapper](#threshold-wrapper)
  - [Watching PostgreSQL](#watching-postgresql)
  - [Watching MySQL / MariaDB](#watching-mysql--mariadb)
  - [Watching Redis](#watching-redis)
  - [Watching Memcached](#watching-memcached)
  - [Watching RabbitMQ](#watching-rabbitmq)
  - [Watching Kafka](#watching-kafka)
  - [Watching MongoDB](#watching-mongodb)
  - [Watching an HTTP endpoint](#watching-an-http-endpoint)
  - [Watching Celery workers](#watching-celery-workers)
  - [SQLAlchemy engine probe](#sqlalchemy-engine-probe)
  - [All built-in probes](#all-built-in-probes)
- [Configuration reference](#configuration-reference)
- [Kubernetes integration](#kubernetes-integration)
- [License](#license)

---

## Installation

Install only the extras you actually use. Nothing is pulled in by default beyond FastAPI and Pydantic.

> **zsh users:** wrap the package name in quotes to prevent the shell from interpreting `[` and `]` as glob patterns.

```bash
# Core package — includes the always-passing NoOpProbe, no other deps
pip install fastapi-watch

# Add individual service probes as needed
pip install "fastapi-watch[postgres]"     # PostgreSQL        (asyncpg)
pip install "fastapi-watch[mysql]"        # MySQL / MariaDB   (aiomysql)
pip install "fastapi-watch[sqlalchemy]"   # Any SQLAlchemy 2.x async engine
pip install "fastapi-watch[redis]"        # Redis             (redis)
pip install "fastapi-watch[memcached]"    # Memcached         (aiomcache)
pip install "fastapi-watch[rabbitmq]"     # RabbitMQ          (aio-pika + aiohttp)
pip install "fastapi-watch[kafka]"        # Kafka             (aiokafka)
pip install "fastapi-watch[mongo]"        # MongoDB           (motor)
pip install "fastapi-watch[http]"         # HTTP endpoint     (aiohttp)
pip install "fastapi-watch[celery]"       # Celery workers    (celery)

# Or pull everything in one shot
pip install "fastapi-watch[all]"
```

Multiple extras can be combined:

```bash
pip install "fastapi-watch[postgres,redis,rabbitmq]"
```

---

## Quick start

Create a `HealthRegistry`, attach it to your FastAPI `app`, and call `.add()` for each service you want to monitor. The registry mounts all health endpoints automatically.

```python
import logging
from fastapi import FastAPI
from fastapi_watch import HealthRegistry
from fastapi_watch.probes import PostgreSQLProbe, RedisProbe

app = FastAPI()

registry = HealthRegistry(
    app,
    poll_interval_ms=60_000,            # re-run probes every 60 s while streaming
    logger=logging.getLogger(__name__), # optional — omit to silence all logging
    grace_period_ms=10_000,             # hold /ready for 10 s while the app warms up
    history_size=20,                    # keep the last 20 results per probe (default: 120)
)

registry.add(PostgreSQLProbe(url="postgresql://user:pass@localhost/mydb"))
registry.add(RedisProbe(url="redis://localhost:6379"), critical=False)
```

That's it. The following routes are now live:

```
GET /health/live          → always 200
GET /health/ready         → 200 (healthy/degraded) / 503 (unhealthy)
GET /health/status        → 200 / 207
GET /health/history       → rolling probe history (TTL: 2 hours)
GET /health/alerts        → probe state-change alert log (TTL: 72 hours)
GET /health/metrics       → Prometheus text format
GET /health/startup       → 200 after set_started(); 503 before
GET /health/dashboard     → HTML dashboard with live SSE updates
GET /health/ready/stream  → SSE stream
GET /health/status/stream → SSE stream
```

### Quick start with ProbeRouter

For larger applications, define probes in each feature module and collect them all in `main.py` via `ProbeRouter`:

```python
# features/database/probes.py
from fastapi_watch import ProbeRouter
from fastapi_watch.probes import PostgreSQLProbe, RedisProbe

router = ProbeRouter()
router.add(PostgreSQLProbe(url="postgresql://..."))
router.add(RedisProbe(url="redis://..."), critical=False)
```

```python
# features/users/probes.py
from fastapi_watch import ProbeRouter, FastAPIRouteProbe

router = ProbeRouter()

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

registry = HealthRegistry(app, routers=[db_router, users_router])

@app.get("/users")
@users_probe.watch
async def list_users():
    ...
```

---

## Endpoints

| Endpoint | Purpose | Healthy | Degraded | Unhealthy |
|---|---|---|---|---|
| `GET /health/live` | **Liveness** — is the process alive? | `200 OK` | `200 OK` | `200 OK` |
| `GET /health/ready` | **Readiness** — are all critical probes passing or degraded? | `200 OK` | `200 OK` | `503 Service Unavailable` |
| `GET /health/status` | **Status** — full detail on every probe | `200 OK` | `207 Multi-Status` | `207 Multi-Status` |
| `GET /health/history` | **History** — last N results per probe (within TTL window) | `200 OK` | `200 OK` | `200 OK` |
| `GET /health/alerts` | **Alerts** — probe state-change log (within alert TTL window) | `200 OK` | `200 OK` | `200 OK` |
| `GET /health/metrics` | **Prometheus metrics** — text format 0.0.4 | `200 OK` | `200 OK` | `200 OK` |
| `GET /health/startup` | **Startup** — has `set_started()` been called and do startup probes pass? | `200 OK` | — | `503 Service Unavailable` |
| `GET /health/dashboard` | **Dashboard** — live HTML page | `200 OK` | `200 OK` (amber banner) | `200 OK` (red header) |
| `GET /health/ready/stream` | **Readiness stream** — SSE; polls while connected | `200 OK` | stream of events | stream of events |
| `GET /health/status/stream` | **Status stream** — SSE; polls while connected | `200 OK` | stream of events | stream of events |

The prefix defaults to `/health` and can be changed at construction time:

```python
registry = HealthRegistry(app, prefix="/ops/health")
# → /ops/health/live
# → /ops/health/ready
# → /ops/health/status
# → /ops/health/history
# → /ops/health/alerts
# → /ops/health/metrics
# → /ops/health/startup
# → /ops/health/dashboard
# → /ops/health/ready/stream
# → /ops/health/status/stream
```

---

## Health Dashboard

`GET /health/dashboard` returns a server-rendered HTML page that shows all probe results in a card grid and updates live over SSE. No extra Python dependencies are required — the page is generated inline and all CSS and JavaScript are embedded.

The dashboard is registered by default. Disable it with `dashboard=False` if you don't want to expose a human-readable view:

```python
registry = HealthRegistry(app)                  # dashboard on — GET /health/dashboard
registry = HealthRegistry(app, dashboard=False) # dashboard off
```

### What the dashboard shows

**Header bar** — the overall status is displayed prominently at the top. The bar is green when all critical probes are healthy, amber when any critical probe is degraded, and red when any critical probe is unhealthy. A pulsing animation signals active degradation or failure. The "Last checked" timestamp and timezone are shown in the top right alongside a live connection indicator. When maintenance mode is active, an amber banner appears below the header.

**Probe cards** — one card per registered probe, arranged in a responsive grid. Each card contains:

- A colored left border (green = healthy, amber = degraded, red = unhealthy) that updates live
- The probe name and an `optional` badge for non-critical probes
- The probe's average latency in milliseconds
- A status pill (`Healthy` / `Degraded` / `Unhealthy`)
- The error message, if the probe is failing
- A details table with all service-specific metadata — connection counts, memory usage, error rates, latency percentiles, and so on

**Footer links** — quick links to the raw JSON endpoints (`/health/status`, `/health/history`, `/health/ready`).

### Live updates

When the page loads, a small embedded JavaScript snippet opens an `EventSource` connection to `/health/status/stream`. Each SSE event surgically updates the DOM — colors, text, and detail table rows — without a full page reload or any visible flash. The live indicator in the header glows green when connected and goes grey on disconnect.

No external JavaScript frameworks or CDN resources are used. The page is fully self-contained.

---

## Probe management

### Adding probes

Add probes one at a time with `add()`, or pass a list with `add_probes()`. Both methods return `self` for chaining. Adding the same instance twice is a no-op.

```python
# Single probe
registry.add(probe_a)

# Multiple probes in one call
registry.add_probes([probe_a, probe_b, probe_c])

# Chained
registry.add(probe_a).add(probe_b).add(probe_c)

# Duplicate ignored — probe_a is only registered once
registry.add(probe_a)
registry.add(probe_a)
```

Probes run **concurrently** on every check — a slow or failing probe never delays the others.

### Critical vs non-critical probes

By default every probe is **critical** — a failing critical probe sets the overall `status` to `"unhealthy"` and causes `/health/ready` to return `503`.

Mark a probe as non-critical when its failure should be visible in reports but shouldn't block traffic:

```python
# Database is essential; fail readiness if it's unreachable
registry.add(PostgreSQLProbe(url="postgresql://..."), critical=True)

# Cache is nice-to-have; don't fail readiness if it's down
registry.add(RedisProbe(url="redis://localhost"), critical=False)
```

Non-critical probes always appear in `/health/status` with their real result and a `"critical": false` field. They simply don't affect the overall `status` or `/ready`.

`add_probes()` accepts the same flag, applied to every probe in the list:

```python
registry.add_probes([probe_a, probe_b], critical=False)
```

### Per-probe timeout

Set a `timeout` (in seconds) on any probe class or instance. If the check doesn't complete within that time, the probe is recorded as unhealthy — all other probes are unaffected and still run concurrently.

**On the class:**

```python
class MyServiceProbe(BaseProbe):
    name = "my-service"
    timeout = 5.0  # fail after 5 seconds

    async def check(self) -> ProbeResult:
        ...
```

**On an instance:**

```python
probe = MyServiceProbe()
probe.timeout = 2.0
registry.add(probe)
```

`timeout = None` (the default) means no limit. Timed-out probes produce an unhealthy result with `error: "TimeoutError: "`.

---

## ProbeRouter — organizing probes across files

As an application grows, defining every probe in `main.py` becomes unwieldy. `ProbeRouter` mirrors the pattern FastAPI uses for `APIRouter`: declare probes in the modules that own them, and include all of them in the registry at startup.

### Basic usage

```python
# features/database/probes.py
from fastapi_watch import ProbeRouter
from fastapi_watch.probes import PostgreSQLProbe, RedisProbe

router = ProbeRouter()
router.add(PostgreSQLProbe(url="postgresql://user:pass@db.internal/app"))
router.add(RedisProbe(url="redis://cache.internal:6379"), critical=False)
```

```python
# features/payments/probes.py
from fastapi_watch import ProbeRouter
from fastapi_watch.probes import HttpProbe

router = ProbeRouter()
router.add(HttpProbe(url="https://api.stripe.com/v1/health", name="stripe"))
```

```python
# main.py
from fastapi import FastAPI
from fastapi_watch import HealthRegistry
from features.database.probes import router as db_router
from features.payments.probes import router as payments_router

app = FastAPI()

registry = HealthRegistry(app, routers=[db_router, payments_router])
```

The `routers=` parameter accepts a list of `ProbeRouter` instances. All probes from every router are registered in the order they were added, preserving each probe's `critical` setting.

If you need to add individual probes alongside routers, `include_router()` is also available after construction and returns `self` for chaining:

```python
registry = HealthRegistry(app)
registry.include_router(db_router).include_router(payments_router).add(some_extra_probe)
```

### Composing routers

Routers can include other routers, letting you build a single top-level aggregator that collects probes from every submodule:

```python
# probes/__init__.py
from fastapi_watch import ProbeRouter
from .database import router as db_router
from .payments import router as payments_router
from .messaging import router as messaging_router

router = ProbeRouter()
router.include_router(db_router)
router.include_router(payments_router)
router.include_router(messaging_router)
```

```python
# main.py — one import, one line
from fastapi_watch import HealthRegistry
from probes import router

registry = HealthRegistry(app, routers=[router])
```

### ProbeRouter API

`ProbeRouter` exposes the same fluent interface as `HealthRegistry`. All methods return `self` for chaining. Duplicate probe instances (same object, identity check) are silently skipped.

```python
router = ProbeRouter()

router.add(probe)                          # single probe, critical by default
router.add(probe, critical=False)          # mark as non-critical
router.add_probes([probe_a, probe_b])      # multiple probes, same criticality
router.add_probes([probe_c], critical=False)
router.include_router(another_router)      # merge another router's probes
```

---

## Live streaming

The two streaming endpoints (`/health/ready/stream`, `/health/status/stream`) use [Server-Sent Events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events) (SSE) to push probe results to connected clients.

**The poll loop is demand-driven** — it starts when the first SSE client connects and stops automatically when the last one disconnects. No background work is done when nobody is watching.

Each event is a JSON-encoded health report on a `data:` line:

```
data: {"status": "healthy", "checked_at": "2024-06-01T12:00:00.123456+00:00", "probes": [...]}

data: {"status": "unhealthy", "checked_at": "2024-06-01T12:00:05.456789+00:00", "probes": [...]}
```

### Connecting from JavaScript

```js
const es = new EventSource('/health/status/stream');

es.onmessage = (event) => {
    const report = JSON.parse(event.data);
    console.log(report.status, report.probes);
};

es.onerror = () => es.close();
```

### Connecting with curl

```bash
curl -N http://localhost:8000/health/status/stream
```

### Configuring the poll interval

```python
# Default — poll every 60 seconds while a client is connected
registry = HealthRegistry(app)

# Custom interval
registry = HealthRegistry(app, poll_interval_ms=10_000)  # every 10 s

# Minimum enforced interval is 1000 ms; lower values are clamped
registry = HealthRegistry(app, poll_interval_ms=500)     # → 1000 ms

# Disable polling — streaming endpoints emit one result then close
registry = HealthRegistry(app, poll_interval_ms=None)
```

The interval can also be changed at any point after startup:

```python
registry.set_poll_interval(30_000)   # switch to every 30 s
registry.set_poll_interval(0)        # disable — single-fetch mode
```

---

## Polling and caching

The regular `GET /health/ready` and `GET /health/status` endpoints always respond immediately:

- **When SSE clients are connected** — the poll loop is running, so these endpoints serve the most recent cached probe results without re-running any probes.
- **When no streaming is active** — probes are run on demand. A built-in lock prevents a thundering herd if multiple requests arrive simultaneously before the first result is cached.

This means your GET endpoints are fast under all conditions, regardless of whether anyone is streaming.

---

## Per-probe poll frequency

By default every probe uses the registry's `poll_interval_ms`. Set `poll_interval_ms` on any probe to override this for that probe only. Probes with their own interval run on their own schedule — a slow probe doesn't delay fast ones.

```python
registry = HealthRegistry(app, poll_interval_ms=60_000)  # global: every 60 s

registry.add(PostgreSQLProbe(url="postgresql://...", poll_interval_ms=30_000))  # every 30 s
registry.add(RedisProbe(url="redis://...", poll_interval_ms=10_000))            # every 10 s
registry.add(NoOpProbe())                                                        # uses global: 60 s
```

The minimum enforced interval is 1000 ms — lower values are clamped. Pass `poll_interval_ms=None` on the probe to explicitly use the registry default. Probes without a custom interval are always in sync with the registry-level setting.

All built-in probes expose `poll_interval_ms` as a constructor argument. Custom probes inherit the attribute from `BaseProbe`:

```python
class MyServiceProbe(BaseProbe):
    name = "my-service"
    poll_interval_ms = 5_000  # class-level default

    async def check(self) -> ProbeResult:
        ...
```

---

## Three-state health (DEGRADED)

Every probe result and the overall health report can be in one of three states:

| State | Meaning | `/health/ready` | `/health/status` |
|-------|---------|-----------------|------------------|
| `"healthy"` | All clear | `200 OK` | `200 OK` |
| `"degraded"` | Under stress — still serving traffic | `200 OK` | `207 Multi-Status` |
| `"unhealthy"` | Critical failure — stop routing traffic | `503 Service Unavailable` | `207 Multi-Status` |

**DEGRADED is a first-class signal.** It lets probes communicate "something is wrong but the service is still responding" without triggering an emergency. Load balancers keep routing traffic (200), the dashboard shows an amber card, and Prometheus scrapes surface a `probe_degraded` gauge.

Built-in probes that emit DEGRADED: `EventLoopProbe`, `ThresholdProbe`. Custom probes can return it at any time:

```python
from fastapi_watch.models import ProbeResult, ProbeStatus

return ProbeResult(
    name=self.name,
    status=ProbeStatus.DEGRADED,
    details={"queue_depth": 950, "threshold": 800},
)
```

**Overall status rules (critical probes only):**

- Any UNHEALTHY critical probe → overall `"unhealthy"`
- Any DEGRADED critical probe (no UNHEALTHY) → overall `"degraded"`
- All healthy → overall `"healthy"`
- Non-critical probes never affect the overall status.

**Circuit breaker interaction:** DEGRADED counts as a passing result for the circuit breaker (`is_passing = True`). A probe oscillating between healthy and degraded never trips its own circuit.

**`ProbeResult` properties:**

| Property | Returns `True` when |
|----------|---------------------|
| `is_healthy` | `status == "healthy"` (strict) |
| `is_degraded` | `status == "degraded"` |
| `is_passing` | `status != "unhealthy"` (healthy or degraded) |

---

## Circuit breaker

The circuit breaker prevents a broken dependency from being called repeatedly when it is clearly failing. After a probe fails a configurable number of consecutive times, the circuit opens and the probe is suspended — it returns the last known result with a `"circuit_breaker_open": true` flag in `details` until the cooldown period expires.

```python
# Defaults: opens after 5 consecutive failures, stays open 10 minutes
registry = HealthRegistry(app)

# Custom threshold and cooldown
registry = HealthRegistry(
    app,
    circuit_breaker_threshold=3,          # open after 3 consecutive failures
    circuit_breaker_cooldown_ms=120_000,  # try again after 2 minutes
)

# Disable entirely — probes always run regardless of failure history
registry = HealthRegistry(app, circuit_breaker=False)
```

Per-probe overrides let you tune the behaviour for individual dependencies without changing the global defaults:

```python
from fastapi_watch.probes import PostgreSQLProbe

probe = PostgreSQLProbe(url="postgresql://...")
probe.circuit_breaker_threshold = 2       # stricter — open after 2 failures
probe.circuit_breaker_cooldown_ms = 60_000  # shorter cooldown — retry after 1 minute
registry.add(probe)
```

All other fields (`status`, `error`, `latency_ms`) reflect the last result before the circuit opened. Once the cooldown expires the probe runs again — if it succeeds, the circuit closes and the error counter resets.

---

## Circuit breaker metrics

When the circuit breaker is enabled (the default), a `circuit_breaker` dict is injected into every probe result's `details` on every check — whether the circuit is open or closed. This gives you live visibility into failure accumulation before a trip occurs.

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
| `consecutive_failures` | Unbroken run of failures since the last success; resets to `0` on any passing result |
| `trips_total` | Lifetime count of times this probe's circuit has tripped |

When the circuit is open, the probe is not called — the dict reflects the state at the time the circuit tripped:

```json
{
  "circuit_breaker": {
    "open": true,
    "consecutive_failures": 5,
    "trips_total": 2
  }
}
```

These fields are also exported via `/health/metrics` as Prometheus gauges (`probe_circuit_open`, `probe_circuit_consecutive_failures`) and a counter (`probe_circuit_trips_total`).

Disable circuit breaker metrics injection with `circuit_breaker=False`:

```python
registry = HealthRegistry(app, circuit_breaker=False)
# → no "circuit_breaker" key in any probe result's details
```

---

## Maintenance mode

Signal to the health system that your application is undergoing planned maintenance. While active:

- `/health/ready` returns `200 {"status": "maintenance"}` regardless of probe results — load balancers keep routing traffic and alerts stay quiet.
- State-change webhooks are suppressed — probe flaps during maintenance don't trigger pages.
- The dashboard shows an amber maintenance banner.

```python
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Maintenance until a specific time
registry.set_maintenance(until=datetime.now(ZoneInfo("UTC")) + timedelta(hours=2))

# Clear maintenance early
registry.clear_maintenance()
```

Both `set_maintenance()` and `clear_maintenance()` return `self` for chaining.

The `until` datetime must be timezone-aware. Once the `until` time passes, maintenance mode deactivates automatically — no explicit `clear_maintenance()` call is needed.

```python
# Check programmatically
if registry._in_maintenance():
    ...
```

---

## Prometheus metrics

`GET /health/metrics` returns a Prometheus text format 0.0.4 export of every probe's current state. Scrape it from your Prometheus instance like any other target — no extra dependencies are required.

```
# HELP probe_healthy 1 if the probe is healthy, 0 otherwise
# TYPE probe_healthy gauge
probe_healthy{name="postgresql",critical="true"} 1
probe_healthy{name="redis",critical="false"} 0

# HELP probe_degraded 1 if the probe is degraded, 0 otherwise
# TYPE probe_degraded gauge
probe_degraded{name="postgresql",critical="true"} 0

# HELP probe_latency_ms Last probe latency in milliseconds
# TYPE probe_latency_ms gauge
probe_latency_ms{name="postgresql",critical="true"} 1.83

# HELP probe_circuit_open 1 if the circuit breaker is open
# TYPE probe_circuit_open gauge
probe_circuit_open{name="redis",critical="false"} 1

# HELP probe_circuit_consecutive_failures Consecutive failure count
# TYPE probe_circuit_consecutive_failures gauge
probe_circuit_consecutive_failures{name="redis",critical="false"} 5

# HELP probe_circuit_trips_total Total circuit breaker trips
# TYPE probe_circuit_trips_total counter
probe_circuit_trips_total{name="redis",critical="false"} 2
```

The endpoint always returns `200 OK` with `Content-Type: text/plain; version=0.0.4; charset=utf-8`.

**Prometheus scrape config:**

```yaml
scrape_configs:
  - job_name: myapp_health
    static_configs:
      - targets: ["localhost:8000"]
    metrics_path: /health/metrics
```

---

## App-wide request metrics

`RequestMetricsMiddleware` wraps your entire FastAPI app at the ASGI layer and records aggregate request statistics across all routes — no decorator needed on individual handlers. Pair it with `RequestMetricsProbe` to surface those statistics as a health probe.

```python
from fastapi import FastAPI
from fastapi_watch import HealthRegistry, RequestMetricsMiddleware, RequestMetricsProbe

app = FastAPI()

# ... define your routes ...

registry = HealthRegistry(app, poll_interval_ms=None)

# Create the middleware manually so the probe shares the same instance
middleware = RequestMetricsMiddleware(app, per_route=True)
probe = RequestMetricsProbe(
    middleware,
    max_error_rate=0.05,     # fail if >5% of requests error
    max_avg_rtt_ms=500,      # fail if average RTT exceeds 500 ms
)
registry.add(probe)

# Use the middleware as the ASGI app so TestClient / uvicorn see the same instance
# In production: uvicorn myapp:middleware
```

> **Important:** Create the middleware yourself rather than using `app.add_middleware()`. `add_middleware()` creates a new internal instance that the probe cannot see. Pass the same `middleware` object to both `RequestMetricsProbe` and your ASGI server.

**Per-route breakdown**

Set `per_route=True` (the default) to track stats broken down by route template. Path parameters are normalized so `/users/1` and `/users/2` both count under `/users/{user_id}`:

```json
{
  "request_count": 1500,
  "error_count": 12,
  "error_rate": 0.008,
  "avg_rtt_ms": 45.2,
  "consecutive_errors": 0,
  "routes": {
    "GET /users": { "request_count": 900, "error_count": 2, "error_rate": 0.0022, "avg_rtt_ms": 38.1 },
    "GET /users/{user_id}": { "request_count": 550, "error_count": 8, "error_rate": 0.0145, "avg_rtt_ms": 61.4 },
    "POST /users": { "request_count": 50, "error_count": 2, "error_rate": 0.04, "avg_rtt_ms": 120.7 }
  }
}
```

Set `per_route=False` to collect only the aggregate totals (lower memory overhead for apps with many routes).

**Constructor arguments — `RequestMetricsMiddleware`:**

| Argument | Default | Description |
|----------|---------|-------------|
| `app` | required | The FastAPI (or any ASGI) app to wrap |
| `per_route` | `True` | Track per-route-template breakdown in addition to aggregate stats |
| `window_size` | `200` | Sliding window size for RTT tracking |
| `ema_alpha` | `0.1` | EMA smoothing factor for `avg_rtt_ms` |

**Constructor arguments — `RequestMetricsProbe`:**

| Argument | Default | Description |
|----------|---------|-------------|
| `middleware` | required | The `RequestMetricsMiddleware` instance to read from |
| `name` | `"request_metrics"` | Probe label |
| `max_error_rate` | `0.1` | Error-rate threshold (0–1) above which the probe is UNHEALTHY |
| `max_avg_rtt_ms` | `None` | Average-RTT threshold in milliseconds. `None` disables the threshold |
| `poll_interval_ms` | `None` | Per-probe poll interval; `None` uses the registry default |

---

## Webhook on state change

Pass `webhook_url` to receive an HTTP POST every time a probe's status changes. The call is fire-and-forget — it never blocks health check execution and failures are logged silently.

```python
registry = HealthRegistry(
    app,
    webhook_url="https://hooks.example.com/health",
)
```

**Payload:**

```json
{
  "probe": "postgresql",
  "old_status": "healthy",
  "new_status": "unhealthy",
  "timestamp": "2024-06-01T12:05:00.000000+00:00"
}
```

The `Content-Type` header is `application/json`. The webhook is called with a 5-second timeout. If the call fails (network error, non-2xx response), the failure is logged via the registry logger and silently discarded — the health check result is unaffected.

---

## Authentication

Protect all health endpoints with optional authentication. The `auth` parameter accepts three forms.

### No authentication (default)

```python
registry = HealthRegistry(app)  # all endpoints are open
```

### HTTP Basic auth

```python
registry = HealthRegistry(
    app,
    auth={"type": "basic", "username": "admin", "password": "s3cr3t"},
)
```

Requests without a valid `Authorization: Basic ...` header receive `401 Unauthorized` with a `WWW-Authenticate` challenge.

### API key header

```python
registry = HealthRegistry(
    app,
    auth={"type": "apikey", "key": "my-secret-key"},
)
```

By default the key is read from the `X-API-Key` header. Use `"header"` to specify a different header name:

```python
auth={"type": "apikey", "key": "my-secret-key", "header": "Authorization"}
```

Requests without the correct key receive `403 Forbidden`.

### Custom callable

For anything more complex — JWT validation, IP allowlists, multi-factor logic — pass a callable that returns `True` to allow or `False` to reject:

```python
from fastapi import Request

def my_auth(request: Request) -> bool:
    token = request.headers.get("X-Internal-Token", "")
    return token == "expected-value"

registry = HealthRegistry(app, auth=my_auth)
```

Async callables are also supported:

```python
async def my_auth(request: Request) -> bool:
    return await verify_token(request.headers.get("Authorization", ""))

registry = HealthRegistry(app, auth=my_auth)
```

Returning `False` results in a `403 Forbidden` response.

---

## Startup probe

`GET /health/startup` returns `503` until `set_started()` is called. Use it as a Kubernetes `startupProbe` target to hold traffic away while the application initialises.

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi_watch import HealthRegistry

@asynccontextmanager
async def lifespan(app: FastAPI):
    registry.set_started()   # signal that startup is complete
    yield

app = FastAPI(lifespan=lifespan)
registry = HealthRegistry(app)
```

**Before `set_started()` is called:**

```json
{"status": "starting"}   → HTTP 503
```

**After `set_started()` is called:**

```json
{"status": "started"}   → HTTP 200
```

### Startup probes

Pass `startup_probes` to run additional checks as part of the startup gate. The `/health/startup` endpoint stays at `503` until both `set_started()` has been called *and* every startup probe passes.

```python
from fastapi_watch.probes import PostgreSQLProbe

db_probe = PostgreSQLProbe(url="postgresql://...")

registry = HealthRegistry(
    app,
    startup_probes=[db_probe],
)
```

Startup probes are separate from the main probe registry — they do not appear in `/health/status` and are not subject to the circuit breaker. They are evaluated on every `/health/startup` request.

```yaml
# Kubernetes — hold traffic until app is fully started and DB is reachable
startupProbe:
  httpGet:
    path: /health/startup
    port: 8000
  failureThreshold: 30
  periodSeconds: 5
```

---

## State-change callbacks

React to probe status transitions in real time. Register one or more callbacks with `on_state_change()`; each receives the probe name, old status, and new status whenever a probe's result changes.

```python
import logging

logger = logging.getLogger(__name__)

def on_change(probe_name: str, old_status, new_status):
    logger.warning("Probe %s changed: %s → %s", probe_name, old_status, new_status)

registry.on_state_change(on_change)
```

Async callbacks are also supported:

```python
async def alert(probe_name, old_status, new_status):
    await send_slack_alert(f"{probe_name} is now {new_status}")

registry.on_state_change(alert)
```

Key behaviours:

- Callbacks fire after every `run_all()` for each probe whose status differs from the previous run.
- The **first run** seeds the initial state — no callbacks are fired until a subsequent run sees a different result.
- Multiple callbacks can be registered; all are called in registration order.
- `on_state_change()` returns `self` for chaining.

---

## Startup grace period

Pass `grace_period_ms` to hold `/health/ready` in a `503 {"status": "starting"}` state for a fixed window after the registry is created. This prevents a load balancer from routing traffic before the application has had time to warm up — without requiring all probes to pass immediately on boot.

```python
registry = HealthRegistry(
    app,
    grace_period_ms=15_000,  # hold readiness for 15 s after startup
)
```

- `/health/ready` returns `503 {"status": "starting"}` while the grace period is active.
- `/health/status` and `/health/live` are **not** affected — they always reflect real probe results.
- After the grace period expires, `/ready` resumes normal probe-based behaviour.
- `grace_period_ms=0` (default) disables the grace period entirely.

Pair with Kubernetes' `initialDelaySeconds` for belt-and-suspenders protection during slow startup:

```yaml
readinessProbe:
  httpGet:
    path: /health/ready
    port: 8000
  initialDelaySeconds: 5   # k8s waits 5 s before its first check
  periodSeconds: 10
```

```python
# App-side grace covers the remaining warmup window
registry = HealthRegistry(app, grace_period_ms=20_000)
```

---

## Probe result history

Every probe result is stored in a rolling per-probe history. Use `GET /health/history` to inspect past runs — useful for debugging flapping probes or tracking latency over time.

```python
registry = HealthRegistry(
    app,
    history_size=20,          # max results kept per probe (default: 120)
    result_ttl_seconds=3600,  # drop results older than 1 hour (default: 7200 = 2 hours)
)
```

Results older than `result_ttl_seconds` are excluded from `/health/history` responses. When the per-probe `history_size` cap is reached, the oldest entry is dropped regardless of TTL.

**`GET /health/history` — response format:**

```json
{
  "probes": {
    "postgresql": [
      {
        "name": "postgresql",
        "status": "healthy",
        "critical": true,
        "latency_ms": 1.8,
        "error": null,
        "details": { "version": "PostgreSQL 16.2 ...", "active_connections": 5 }
      },
      {
        "name": "postgresql",
        "status": "healthy",
        "critical": true,
        "latency_ms": 2.1,
        "error": null,
        "details": { "version": "PostgreSQL 16.2 ...", "active_connections": 6 }
      }
    ],
    "redis": [
      {
        "name": "redis",
        "status": "unhealthy",
        "critical": false,
        "latency_ms": 5002.0,
        "error": "Connection refused",
        "details": null
      },
      {
        "name": "redis",
        "status": "healthy",
        "critical": false,
        "latency_ms": 0.9,
        "error": null,
        "details": { "version": "7.2.4", "total_keys": 312 }
      }
    ]
  }
}
```

Results are ordered oldest-first. History is in-memory by default and resets on process restart. See [Custom storage backend](#custom-storage-backend) to persist across restarts.

---

## Alert history

Every probe state change is recorded as an alert. Use `GET /health/alerts` to retrieve them — useful for auditing when and how often services flapped.

```python
registry = HealthRegistry(
    app,
    alert_ttl_seconds=86400,  # keep alerts for 24 hours (default: 259200 = 72 hours)
    max_alerts=500,           # hard cap on stored alerts (default: 120)
)
```

Alerts are retained for up to `alert_ttl_seconds`. When `max_alerts` is reached the oldest alert is dropped immediately regardless of TTL. Alerts are recorded for every state transition including maintenance-suppressed ones.

**`GET /health/alerts` — response format:**

```json
{
  "alerts": [
    {
      "probe": "redis",
      "old_status": "healthy",
      "new_status": "unhealthy",
      "timestamp": "2026-03-29T14:22:01.843+00:00"
    },
    {
      "probe": "redis",
      "old_status": "unhealthy",
      "new_status": "healthy",
      "timestamp": "2026-03-29T14:25:17.112+00:00"
    }
  ]
}
```

Alerts are ordered oldest-first.

---

## Custom storage backend

By default probe results and alerts are held in memory (`InMemoryProbeStorage`). Pass a custom `storage` to persist across restarts or share state across multiple instances.

```python
from fastapi_watch import HealthRegistry, ProbeStorage

class MyRedisStorage:
    """Minimal example — see ProbeStorage docstring for a full Redis sketch."""

    async def get_latest(self, name): ...
    async def get_all_latest(self): ...
    async def set_latest(self, result): ...
    def clear_latest(self): ...
    async def append_history(self, result): ...
    async def get_history(self): ...
    async def append_alert(self, alert): ...
    async def get_alerts(self): ...

registry = HealthRegistry(app, storage=MyRedisStorage())
```

Any class that implements all eight methods satisfies the `ProbeStorage` protocol — no inheritance required. The `ProbeStorage` docstring in `storage.py` contains a complete annotated Redis implementation sketch.

When `storage` is supplied, `result_ttl_seconds`, `alert_ttl_seconds`, `max_alerts`, and `history_size` are **not** passed to the custom backend — configure those limits inside your own implementation.

---

## Response format

Every response from `/health/ready`, `/health/status`, and the SSE streams shares the same shape.

### Health report

| Field | Type | Description |
|-------|------|-------------|
| `status` | `"healthy"` \| `"degraded"` \| `"unhealthy"` | Overall result — determined by critical probes only |
| `checked_at` | `string` \| `null` | UTC ISO 8601 timestamp of the last probe run; `null` before the first run |
| `timezone` | `string` \| `null` | IANA timezone name used for `checked_at` |
| `probes` | `array` | Individual probe results (see below) |

### Probe result

| Field | Type | Description |
|-------|------|-------------|
| `name` | `string` | Probe identifier |
| `status` | `"healthy"` \| `"degraded"` \| `"unhealthy"` | State for this probe |
| `critical` | `boolean` | `true` if the probe affects overall status and readiness |
| `latency_ms` | `number` | How long the check took in milliseconds |
| `error` | `string` \| `null` | Error message; only present on failure |
| `details` | `object` \| `null` | Service-specific metadata (see each probe's section) |

### Example: all healthy — `200`

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
      "details": {
        "version": "PostgreSQL 16.2 on aarch64-unknown-linux-gnu",
        "active_connections": 5,
        "max_connections": 100,
        "database_size": "42 MB"
      }
    },
    {
      "name": "redis",
      "status": "healthy",
      "critical": false,
      "latency_ms": 0.6,
      "error": null,
      "details": {
        "version": "7.2.4",
        "uptime_seconds": 86400,
        "used_memory_human": "2.50M",
        "connected_clients": 8,
        "total_keys": 312
      }
    }
  ]
}
```

### Example: one critical probe failing — `503` on `/ready`, `207` on `/status`

```json
{
  "status": "unhealthy",
  "checked_at": "2024-06-01T12:00:05.456789+00:00",
  "timezone": "UTC",
  "probes": [
    {
      "name": "postgresql",
      "status": "unhealthy",
      "critical": true,
      "latency_ms": 5002.1,
      "error": "Connection refused",
      "details": null
    },
    {
      "name": "redis",
      "status": "healthy",
      "critical": false,
      "latency_ms": 0.6,
      "error": null,
      "details": { "version": "7.2.4" }
    }
  ]
}
```

### Example: non-critical probe failing — still `200` on `/ready`

```json
{
  "status": "healthy",
  "checked_at": "2024-06-01T12:00:10.000000+00:00",
  "timezone": "UTC",
  "probes": [
    {
      "name": "postgresql",
      "status": "healthy",
      "critical": true,
      "latency_ms": 1.9,
      "error": null,
      "details": { "active_connections": 5 }
    },
    {
      "name": "redis",
      "status": "unhealthy",
      "critical": false,
      "latency_ms": 5001.3,
      "error": "Connection timed out",
      "details": null
    }
  ]
}
```

---

## Writing a custom probe

Any class that extends `BaseProbe` and implements `check()` works as a probe. This is the right approach for internal services, third-party SDKs, business-logic checks, or composite conditions.

### Minimal probe

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

`check()` must be an async method and must return a `ProbeResult`. Any unhandled exception raised by `check()` is caught by the registry, automatically recorded as an unhealthy result, and optionally logged — your probe never needs to worry about crashing the health system.

### Recording latency and details

Use `time.perf_counter()` to measure the check duration and populate `latency_ms`. The `details` dict accepts any JSON-serializable data.

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
                details={
                    "region": info.region,
                    "provider_version": info.version,
                    "response_ms": round(latency, 2),
                },
            )
        except Exception as exc:
            latency = (time.perf_counter() - start) * 1000
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.UNHEALTHY,
                latency_ms=round(latency, 2),
                error=str(exc),
            )
```

### Configurable probe

Pass configuration through `__init__` so the same probe class can be reused with different settings.

```python
class S3BucketProbe(BaseProbe):
    def __init__(self, bucket: str, region: str = "us-east-1", name: str = "s3") -> None:
        self.bucket = bucket
        self.region = region
        self.name = name

    async def check(self) -> ProbeResult:
        import time
        import aiobotocore.session

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
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.UNHEALTHY,
                latency_ms=round(latency, 2),
                error=str(exc),
            )

# Register multiple buckets as separate probes
registry.add(S3BucketProbe(bucket="my-app-uploads", region="eu-west-1", name="s3-uploads"))
registry.add(S3BucketProbe(bucket="my-app-backups", region="us-east-1", name="s3-backups"))
```

### Adding a timeout

Set the `timeout` attribute (in seconds) on the class or instance. The registry will cancel the check and record it as unhealthy if it runs too long.

```python
class SlowExternalProbe(BaseProbe):
    name = "slow-external"
    timeout = 3.0  # class-level default

    async def check(self) -> ProbeResult:
        result = await call_slow_external_api()
        return ProbeResult(name=self.name, status=ProbeStatus.HEALTHY)

# Override on a specific instance
probe = SlowExternalProbe()
probe.timeout = 1.5
registry.add(probe)
```

### Composite probe

Wrap multiple inner probes to build custom aggregation logic — for example, reporting unhealthy only when both a primary and replica are down simultaneously.

```python
import asyncio
from fastapi_watch.probes import BaseProbe, RedisProbe
from fastapi_watch.models import ProbeResult, ProbeStatus

class RedisHAProbe(BaseProbe):
    name = "redis-ha"

    def __init__(self, primary_url: str, replica_url: str) -> None:
        self._primary = RedisProbe(url=primary_url, name="primary")
        self._replica = RedisProbe(url=replica_url, name="replica")

    async def check(self) -> ProbeResult:
        primary, replica = await asyncio.gather(
            self._primary.check(), self._replica.check()
        )
        if primary.is_healthy or replica.is_healthy:
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.HEALTHY,
                details={
                    "primary": primary.status.value,
                    "replica": replica.status.value,
                },
            )
        return ProbeResult(
            name=self.name,
            status=ProbeStatus.UNHEALTHY,
            error=f"both nodes down — primary: {primary.error}, replica: {replica.error}",
        )

registry.add(RedisHAProbe(
    primary_url="redis://primary.internal:6379",
    replica_url="redis://replica.internal:6379",
))
```

### Exception handling

If `check()` raises an unhandled exception, the registry catches it and returns an unhealthy result automatically — you do **not** need to wrap your entire probe body in a try/except for this purpose. The auto-generated result looks like:

```json
{
  "name": "my-service",
  "status": "unhealthy",
  "critical": true,
  "latency_ms": 0.0,
  "error": "RuntimeError: connection pool exhausted",
  "details": null
}
```

If a `logger` was passed to `HealthRegistry`, the exception is also logged with full traceback via `logger.exception()`.

You should still catch exceptions yourself inside `check()` if you want to record partial details, a meaningful `latency_ms`, or a more specific `error` message.

### Testing a custom probe

Use `pytest-asyncio` to test `check()` directly without needing to spin up an HTTP server.

```python
import pytest
from fastapi_watch.models import ProbeStatus
from myapp.probes import MyServiceProbe

@pytest.mark.asyncio
async def test_healthy_when_service_responds():
    probe = MyServiceProbe()
    result = await probe.check()
    assert result.status == ProbeStatus.HEALTHY
    assert result.name == "my-service"

@pytest.mark.asyncio
async def test_unhealthy_when_service_raises(monkeypatch):
    async def fail():
        raise ConnectionError("refused")

    monkeypatch.setattr("myapp.probes.call_my_service", fail)
    probe = MyServiceProbe()
    result = await probe.check()
    assert result.status == ProbeStatus.UNHEALTHY
    assert "refused" in result.error
```

You can also run the full registry against a real or mock dependency:

```python
from fastapi import FastAPI
from fastapi_watch import HealthRegistry

@pytest.mark.asyncio
async def test_registry_run_all():
    app = FastAPI()
    registry = HealthRegistry(app, poll_interval_ms=None)
    registry.add(MyServiceProbe())

    results = await registry.run_all()
    assert results[0].status == ProbeStatus.HEALTHY
```

### Probe implementation checklist

- `name` must be set — either as a class attribute or in `__init__` via `self.name`.
- `check()` must be `async` and return a `ProbeResult`.
- Set `latency_ms` for probes where response time matters.
- Populate `details` with any data useful for diagnosis.
- Set `timeout` if the underlying call can hang indefinitely.
- Do not call `registry.run_all()` or other registry methods from inside `check()`.

---

## Built-in probes

### Probe details

Every built-in probe populates the `details` field with service-specific metadata. Details are always best-effort — if the metadata query fails after a successful connectivity check, `details` will contain whatever was collected up to that point. The probe status reflects connectivity only, not the completeness of `details`.

---

### App-wide request metrics probe

See [App-wide request metrics](#app-wide-request-metrics) above for full documentation. Quick reference:

```python
from fastapi_watch import RequestMetricsMiddleware, RequestMetricsProbe

middleware = RequestMetricsMiddleware(app, per_route=True)
probe = RequestMetricsProbe(middleware, max_error_rate=0.05)
registry.add(probe)
```

No extra install required. `RequestMetricsMiddleware` and `RequestMetricsProbe` are included in the base package.

---

### Watching a FastAPI route

`FastAPIRouteProbe` is a passive observer — it instruments an existing route handler using the `@probe.watch` decorator and collects real-traffic metrics on every request. No test requests are made; the probe reports on what your actual users are hitting.

No extra install is required. `FastAPIRouteProbe` is included in the base package.

```python
from fastapi import FastAPI
from fastapi_watch import HealthRegistry, FastAPIRouteProbe

app = FastAPI()

users_probe = FastAPIRouteProbe(name="users-api")

@app.get("/users")
@users_probe.watch
async def list_users():
    return {"users": [...]}

registry = HealthRegistry(app)
registry.add(users_probe)
```

The `@watch` decorator wraps the handler function and preserves its signature — FastAPI's dependency injection continues to work exactly as before.

#### Metrics collected

Every time the decorated handler is called, `FastAPIRouteProbe` records:

| Metric | Description |
|--------|-------------|
| `last_rtt_ms` | Handler execution time for the most recent request |
| `avg_rtt_ms` | Exponential moving average RTT across all requests; also used as the probe's `latency_ms` |
| `p95_rtt_ms` | 95th-percentile RTT calculated over the last `window_size` requests |
| `min_rtt_ms` / `max_rtt_ms` | All-time RTT bounds |
| `last_status_code` | HTTP status code of the most recent response |
| `request_count` | Total requests observed since the probe was created |
| `error_count` | Requests that returned a 4xx or 5xx status code |
| `error_rate` | `error_count / request_count` |
| `consecutive_errors` | Unbroken run of failing requests; resets to `0` on any success |
| `requests_per_minute` | Throughput derived from the sliding request timestamp window; `null` until at least 2 requests have been observed |

`HTTPException` is caught, its status code recorded, and then it is re-raised so FastAPI's normal exception handling is unaffected. Any other unhandled exception is recorded as a `500` and re-raised.

#### Health thresholds

`FastAPIRouteProbe` declares itself `UNHEALTHY` when either configured threshold is exceeded:

- **`max_error_rate`** (default `0.1`) — if more than 10 % of observed requests result in a 4xx/5xx, the probe fails.
- **`max_avg_rtt_ms`** (default `None`) — if the exponential moving average latency exceeds this value in milliseconds, the probe fails.

```python
# Tighter thresholds for a latency-sensitive endpoint
payments_probe = FastAPIRouteProbe(
    name="checkout",
    max_error_rate=0.01,      # fail if >1% of requests error
    max_avg_rtt_ms=200,       # fail if average latency exceeds 200 ms
)

@app.post("/checkout")
@payments_probe.watch
async def checkout(body: CheckoutRequest):
    ...
```

#### Example probe result

```json
{
  "name": "users-api",
  "status": "healthy",
  "critical": true,
  "latency_ms": 45.23,
  "error": null,
  "details": {
    "request_count": 1042,
    "error_count": 3,
    "error_rate": 0.0029,
    "consecutive_errors": 0,
    "last_status_code": 200,
    "last_rtt_ms": 38.12,
    "avg_rtt_ms": 45.23,
    "p95_rtt_ms": 120.10,
    "min_rtt_ms": 12.04,
    "max_rtt_ms": 843.21,
    "requests_per_minute": 142.7
  }
}
```

#### Before the first request

Until at least one request has been handled, `FastAPIRouteProbe.check()` returns `HEALTHY` with a `"no requests observed yet"` message in `details`. This prevents a fresh deployment from immediately showing as unhealthy simply because traffic hasn't arrived yet.

#### Watching sync handlers

`@watch` supports both `async def` and `def` route handlers — the wrapper detects the function type automatically.

#### Constructor arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `name` | `"route"` | Probe label |
| `max_error_rate` | `0.1` | Error-rate threshold above which the probe is UNHEALTHY (0–1) |
| `max_avg_rtt_ms` | `None` | Average-RTT threshold in milliseconds. `None` disables this threshold |
| `window_size` | `100` | Number of recent requests used for percentile and throughput calculations |
| `ema_alpha` | `0.1` | Smoothing factor for the exponential moving average (0–1). Higher = reacts faster to changes |
| `timeout` | `None` | Passed to the registry for the `check()` call; not used internally |

#### Using FastAPIRouteProbe with ProbeRouter

Because `FastAPIRouteProbe` is both a probe and a decorator, the instance needs to be accessible in both the module that owns the route and the module that owns the router. The most ergonomic pattern is to declare the probe in a `probes.py` alongside the routes and import it in both places:

```python
# features/users/probes.py
from fastapi_watch import ProbeRouter, FastAPIRouteProbe

router = ProbeRouter()

users_list_probe = FastAPIRouteProbe(name="users-list", max_error_rate=0.05)
users_detail_probe = FastAPIRouteProbe(name="users-detail", max_avg_rtt_ms=150)

router.add(users_list_probe)
router.add(users_detail_probe)
```

```python
# features/users/routes.py
from fastapi import APIRouter
from .probes import users_list_probe, users_detail_probe

router = APIRouter(prefix="/users")

@router.get("/")
@users_list_probe.watch
async def list_users():
    ...

@router.get("/{user_id}")
@users_detail_probe.watch
async def get_user(user_id: int):
    ...
```

```python
# main.py
from fastapi import FastAPI
from fastapi_watch import HealthRegistry
from features.users.probes import router as users_health_router
from features.users.routes import router as users_api_router

app = FastAPI()
app.include_router(users_api_router)

registry = HealthRegistry(app, routers=[users_health_router])
```

---

### Watching a WebSocket handler

`FastAPIWebSocketProbe` is a passive observer — it instruments a WebSocket handler using the `@probe.watch` decorator and collects real-traffic statistics on every connection without making synthetic connections.

No extra install required. `FastAPIWebSocketProbe` is included in the base package.

```python
from fastapi import FastAPI, WebSocket
from fastapi_watch import HealthRegistry, FastAPIWebSocketProbe

app = FastAPI()

chat_probe = FastAPIWebSocketProbe(name="chat", max_error_rate=0.05)

@app.websocket("/ws/chat")
@chat_probe.watch
async def chat(websocket: WebSocket):
    await websocket.accept()
    while True:
        msg = await websocket.receive_text()
        await websocket.send_text(msg)

registry = HealthRegistry(app)
registry.add(chat_probe)
```

The `@watch` decorator injects a transparent proxy around the real `WebSocket` object. The proxy counts every `receive_*` and `send_*` call; all other WebSocket behaviour (`accept`, `close`, headers, state, etc.) is forwarded to the underlying socket unchanged.

`WebSocketDisconnect` is treated as a normal close and is never counted as an error. Any other unhandled exception increments `error_count`.

#### Metrics collected

| Metric | Description |
|--------|-------------|
| `active_connections` | Sockets currently open |
| `total_connections` | All connections since the probe was created |
| `messages_received` | Total messages received across all connections |
| `messages_sent` | Total messages sent across all connections |
| `error_count` | Connections that closed due to an unhandled exception |
| `error_rate` | `error_count / total_connections` |
| `consecutive_errors` | Unbroken run of error closes; resets to 0 on any clean close |
| `avg_duration_ms` | Exponential moving average of connection lifetimes |
| `min_duration_ms` / `max_duration_ms` | All-time connection duration bounds |

#### Health thresholds

- **`max_error_rate`** (default `0.1`) — UNHEALTHY if more than 10 % of connections close with an error.
- **`min_active_connections`** (default `0`, disabled) — UNHEALTHY if fewer than N sockets are open at check time. Useful for services that maintain persistent connections (live dashboards, data feeds).

```python
FastAPIWebSocketProbe(
    name="feed",
    max_error_rate=0.01,          # fail if >1% of connections error
    min_active_connections=5,     # fail if fewer than 5 sockets are open
)
```

#### Example probe result

```json
{
  "name": "chat",
  "status": "healthy",
  "critical": true,
  "latency_ms": 312.5,
  "error": null,
  "details": {
    "active_connections": 14,
    "total_connections": 502,
    "messages_received": 18340,
    "messages_sent": 18290,
    "error_count": 2,
    "error_rate": 0.004,
    "consecutive_errors": 0,
    "avg_duration_ms": 312.5,
    "min_duration_ms": 0.8,
    "max_duration_ms": 95412.1
  }
}
```

#### Before the first connection

Until at least one connection has been handled, `FastAPIWebSocketProbe.check()` returns `HEALTHY` with a `"no connections observed yet"` message in `details`.

#### Constructor arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `name` | `"websocket"` | Probe label |
| `max_error_rate` | `0.1` | Error-rate threshold (0–1) above which the probe is UNHEALTHY |
| `min_active_connections` | `0` | Minimum open sockets required. `0` disables the check |
| `window_size` | `100` | Number of recent connection durations kept for EMA calculations |
| `ema_alpha` | `0.1` | EMA smoothing factor (0–1). Higher = reacts faster to changes |
| `timeout` | `None` | Passed to the registry for the `check()` call; not used internally |

---

### Event loop lag

`EventLoopProbe` measures how long the asyncio event loop was blocked by scheduling a zero-delay coroutine (`asyncio.sleep(0)`) and timing how long it actually takes to resume. A lag spike means CPU-bound work or slow synchronous calls are blocking the loop.

No extra install required.

```python
from fastapi_watch.probes import EventLoopProbe

registry.add(EventLoopProbe(
    name="event_loop",   # default
    warn_ms=5.0,         # DEGRADED if lag >= 5 ms (default)
    fail_ms=20.0,        # UNHEALTHY if lag >= 20 ms (default)
))
```

**Details returned:**

```json
{ "lag_ms": 2.4, "warn_ms": 5.0, "fail_ms": 20.0 }
```

**Constructor arguments:**

| Argument | Default | Description |
|----------|---------|-------------|
| `name` | `"event_loop"` | Probe label |
| `warn_ms` | `5.0` | Lag threshold for DEGRADED in milliseconds |
| `fail_ms` | `20.0` | Lag threshold for UNHEALTHY in milliseconds |
| `poll_interval_ms` | `None` | Uses registry default |

---

### TCP / DNS reachability

`TCPProbe` resolves a hostname and opens a TCP connection to verify that a host and port are reachable. Both DNS resolution and the TCP connect run in an executor so they never block the event loop. No extra install required — uses only the standard library.

```python
from fastapi_watch.probes import TCPProbe

registry.add(TCPProbe(host="db.internal", port=5432))
registry.add(TCPProbe(host="redis.internal", port=6379, name="redis-tcp", timeout=2.0))
```

The default probe name is `"tcp:{host}:{port}"`.

**Details returned:**

```json
{
  "host": "db.internal",
  "port": 5432,
  "resolved_ips": ["10.0.1.5"],
  "connect_ms": 1.23
}
```

**Constructor arguments:**

| Argument | Default | Description |
|----------|---------|-------------|
| `host` | required | Hostname or IP address |
| `port` | required | TCP port |
| `timeout` | `5.0` | Connection timeout in seconds |
| `name` | `"tcp:{host}:{port}"` | Probe label |
| `poll_interval_ms` | `None` | Uses registry default |

---

### SMTP

`SMTPProbe` connects to an SMTP server, sends `EHLO`, and optionally negotiates STARTTLS and authenticates. The connection is opened and closed on every check — no persistent session is held. Runs in a thread-pool executor. No extra install required — uses only the standard library.

```python
from fastapi_watch.probes import SMTPProbe

# Connectivity only — EHLO handshake
registry.add(SMTPProbe(host="smtp.internal", port=25))

# With STARTTLS
registry.add(SMTPProbe(host="smtp.gmail.com", port=587, use_tls=True))

# With STARTTLS and authentication
registry.add(SMTPProbe(
    host="smtp.sendgrid.net",
    port=587,
    use_tls=True,
    username="apikey",
    password="SG.xxxxx",
    name="sendgrid",
))
```

**Details returned:**

```json
{
  "host": "smtp.sendgrid.net",
  "port": 587,
  "server_banner": "220 smtp.sendgrid.net ESMTP service ready",
  "tls": true,
  "extensions": ["STARTTLS", "AUTH LOGIN PLAIN", "SIZE 31457280"]
}
```

**Constructor arguments:**

| Argument | Default | Description |
|----------|---------|-------------|
| `host` | required | SMTP server hostname |
| `port` | `25` | SMTP port |
| `timeout` | `5.0` | Connection + handshake timeout in seconds |
| `use_tls` | `False` | Upgrade to TLS with STARTTLS after EHLO |
| `username` | `None` | SMTP auth username. `None` skips authentication |
| `password` | `None` | SMTP auth password |
| `name` | `"smtp"` | Probe label |
| `poll_interval_ms` | `None` | Uses registry default |

---

### Threshold wrapper

`ThresholdProbe` wraps any existing probe and promotes or overrides its result based on values in the probe's `details` dict. This is the right tool when you want a probe to go DEGRADED or UNHEALTHY based on a metric it already reports — without modifying the probe itself.

No extra install required.

```python
from fastapi_watch.probes import ThresholdProbe, RedisProbe

redis = RedisProbe(url="redis://localhost:6379")

registry.add(ThresholdProbe(
    probe=redis,
    name="redis-keys",
    warn_if=lambda d: d.get("total_keys", 0) > 500_000,   # DEGRADED
    fail_if=lambda d: d.get("total_keys", 0) > 1_000_000, # UNHEALTHY
))
```

**Semantics:**

- If the inner probe returns UNHEALTHY, the result passes through unchanged — `fail_if` and `warn_if` are not evaluated.
- If `fail_if(details)` returns `True`, the result is UNHEALTHY.
- If `warn_if(details)` returns `True` (and `fail_if` was `False`), the result is DEGRADED.
- If both return `False`, the inner probe's status is preserved.
- If either callable raises an exception, it is swallowed and treated as `False`.

```python
# Monitor Celery queue depth
from fastapi_watch.probes import CeleryProbe, ThresholdProbe

celery_probe = CeleryProbe(celery_app)

registry.add(ThresholdProbe(
    probe=celery_probe,
    name="celery-queue",
    warn_if=lambda d: sum(
        w.get("active_tasks", 0) + w.get("reserved_tasks", 0)
        for w in d.get("workers", {}).values()
    ) > 100,
    fail_if=lambda d: d.get("workers_online", 1) == 0,
))
```

**Constructor arguments:**

| Argument | Default | Description |
|----------|---------|-------------|
| `probe` | required | Any `BaseProbe` instance to wrap |
| `name` | inner probe's name | Probe label for the wrapper |
| `warn_if` | `None` | Callable `(details: dict) -> bool`; `True` → DEGRADED |
| `fail_if` | `None` | Callable `(details: dict) -> bool`; `True` → UNHEALTHY |
| `poll_interval_ms` | `None` | Uses registry default |

---

### Watching PostgreSQL

```bash
pip install "fastapi-watch[postgres]"
```

`PostgreSQLProbe` uses `asyncpg` directly — no SQLAlchemy required. It opens a connection, runs `SELECT version()` and a set of metadata queries concurrently, then closes the connection.

```python
from fastapi_watch.probes import PostgreSQLProbe

registry.add(
    PostgreSQLProbe(
        url="postgresql://app_user:secret@localhost:5432/mydb",
        name="primary-db",  # default: "postgresql"
    )
)
```

**Details returned:**

```json
{
  "version": "PostgreSQL 16.2 on aarch64-unknown-linux-gnu, compiled by gcc 12.2.0",
  "active_connections": 5,
  "max_connections": 100,
  "database_size": "42 MB"
}
```

**Checking a read replica separately:**

```python
registry.add(PostgreSQLProbe(url="postgresql://reader:secret@replica.host/mydb", name="replica-db"))
```

**With a connection timeout** (default 5 seconds):

```python
registry.add(PostgreSQLProbe(url="postgresql://...", timeout=2.0))
```

> If you are already using SQLAlchemy, see [SQLAlchemy engine probe](#sqlalchemy-engine-probe) to reuse your existing engine instead.

---

### Watching MySQL / MariaDB

```bash
pip install "fastapi-watch[mysql]"
```

`MySQLProbe` accepts either a URL or explicit connection kwargs.

```python
from fastapi_watch.probes import MySQLProbe

# URL form
registry.add(MySQLProbe(url="mysql://app_user:secret@localhost:3306/mydb"))

# Keyword form
registry.add(MySQLProbe(host="localhost", port=3306, user="app_user", password="secret", db="mydb"))
```

**Details returned:**

```json
{
  "version": "8.0.36",
  "connected_threads": 4,
  "uptime_seconds": 172800,
  "max_used_connections": 12
}
```

**Constructor arguments:**

| Argument | Default | Description |
|----------|---------|-------------|
| `url` | `None` | Full DSN — overrides all other kwargs when set |
| `host` | `"localhost"` | |
| `port` | `3306` | |
| `user` | `"root"` | |
| `password` | `""` | |
| `db` | `""` | |
| `name` | `"mysql"` | Probe label |
| `connect_timeout` | `5` | Seconds |

---

### Watching Redis

```bash
pip install "fastapi-watch[redis]"
```

`RedisProbe` sends `PING`, then collects server info and scans key prefixes to build a cluster breakdown.

```python
from fastapi_watch.probes import RedisProbe

registry.add(RedisProbe(url="redis://localhost:6379"))
```

**Details returned:**

```json
{
  "version": "7.2.4",
  "uptime_seconds": 86400,
  "used_memory_human": "2.50M",
  "connected_clients": 8,
  "role": "master",
  "total_keys": 312,
  "clusters": {
    "session": 150,
    "cache":   162
  }
}
```

`clusters` groups keys by the segment before the first `:` and reports a key count per group. For example, a key named `session:abc123` falls into the `session` cluster. Up to 1000 keys are scanned; if the keyspace exceeds that limit a `"clusters_truncated": true` field is added.

**Common URL forms:**

```python
# Password-protected
RedisProbe(url="redis://:mypassword@localhost:6379")

# Specific database index
RedisProbe(url="redis://localhost:6379/2", name="task-queue")

# TLS
RedisProbe(url="rediss://redis.internal:6380")

# Watching Redis as both a cache and a queue
registry.add(RedisProbe(url="redis://localhost:6379/0", name="cache"))
registry.add(RedisProbe(url="redis://localhost:6379/1", name="task-queue"))
```

---

### Watching Memcached

```bash
pip install "fastapi-watch[memcached]"
```

`MemcachedProbe` calls `stats()` to verify the server is reachable and responding.

```python
from fastapi_watch.probes import MemcachedProbe

registry.add(MemcachedProbe(host="localhost", port=11211))
```

**Constructor arguments:**

| Argument | Default | Description |
|----------|---------|-------------|
| `host` | `"localhost"` | |
| `port` | `11211` | |
| `name` | `"memcached"` | Probe label |
| `pool_size` | `1` | aiomcache connection pool size |

---

### Watching RabbitMQ

```bash
pip install "fastapi-watch[rabbitmq]"
```

`RabbitMQProbe` has two modes:

- **Connectivity only** (default) — opens and closes an AMQP connection. No channels or queues are touched.
- **Rich mode** — when `management_url` is set, the probe also calls the RabbitMQ Management HTTP API and returns per-queue stats, message rates, and cluster metadata.

#### Connectivity only

```python
from fastapi_watch.probes import RabbitMQProbe

registry.add(
    RabbitMQProbe(
        url="amqp://guest:guest@localhost:5672/",
        name="rabbitmq",  # default
    )
)
```

**Details returned (connectivity only):**

```json
{ "connected": true }
```

#### Rich mode — with Management API

Pass `management_url` pointing at the RabbitMQ Management plugin (default port `15672`). Credentials are taken from the AMQP URL automatically.

```python
registry.add(
    RabbitMQProbe(
        url="amqp://guest:guest@localhost:5672/",
        management_url="http://localhost:15672",
    )
)
```

**Details returned (rich mode):**

```json
{
  "connected": true,
  "server": {
    "rabbitmq_version": "3.12.0",
    "erlang_version": "26.0",
    "cluster_name": "rabbit@my-node",
    "node": "rabbit@my-node",
    "connections": 4,
    "channels": 8,
    "exchanges": 14,
    "queues": 3,
    "consumers": 6
  },
  "totals": {
    "messages": 142,
    "messages_ready": 140,
    "messages_unacknowledged": 2,
    "publish_rate": 12.5,
    "deliver_rate": 11.8,
    "ack_rate": 11.8
  },
  "queues": {
    "tasks": {
      "state": "running",
      "messages": 120,
      "messages_ready": 118,
      "messages_unacknowledged": 2,
      "consumers": 4,
      "memory_bytes": 32768,
      "publish_rate": 10.0,
      "deliver_rate": 9.5,
      "ack_rate": 9.5,
      "durable": true,
      "auto_delete": false,
      "idle_since": null
    }
  }
}
```

If the Management API is unreachable, a `management_api_error` key is added to `details` and the probe still reports the AMQP connection status.

**Other connection forms:**

```python
# Dedicated monitoring vhost
RabbitMQProbe(url="amqp://monitor:secret@rabbitmq.internal/monitoring", management_url="http://rabbitmq.internal:15672")

# TLS / AMQPS
RabbitMQProbe(url="amqps://user:secret@rabbitmq.internal/", name="rabbitmq-tls")

# Multiple cluster nodes — one probe per node
for i, host in enumerate(["rmq-1.internal", "rmq-2.internal", "rmq-3.internal"], start=1):
    registry.add(RabbitMQProbe(url=f"amqp://guest:guest@{host}/", name=f"rabbitmq-node-{i}"))
```

**Constructor arguments:**

| Argument | Default | Description |
|----------|---------|-------------|
| `url` | `"amqp://guest:guest@localhost/"` | AMQP(S) connection URL |
| `name` | `"rabbitmq"` | Probe label |
| `management_url` | `None` | Base URL of the Management HTTP API. When set, enables rich queue-level details. Credentials are taken from `url`. |

---

### Watching Kafka

```bash
pip install "fastapi-watch[kafka]"
```

`KafkaProbe` starts an `AIOKafkaAdminClient` to verify broker reachability, then lists topics and describes the cluster.

```python
from fastapi_watch.probes import KafkaProbe

# Single broker
registry.add(KafkaProbe(bootstrap_servers="localhost:9092"))

# Multiple brokers
registry.add(KafkaProbe(bootstrap_servers=["b1:9092", "b2:9092", "b3:9092"]))
```

**Details returned:**

```json
{
  "broker_count": 3,
  "controller_id": 1,
  "topics": ["orders", "payments", "notifications"],
  "internal_topics": ["__consumer_offsets"]
}
```

`topics` contains user-defined topics only. `internal_topics` lists Kafka-managed topics (those prefixed with `__`).

**Constructor arguments:**

| Argument | Default | Description |
|----------|---------|-------------|
| `bootstrap_servers` | `"localhost:9092"` | String or list of `host:port` entries |
| `name` | `"kafka"` | Probe label |
| `request_timeout_ms` | `5000` | Admin client metadata request timeout |

---

### Watching MongoDB

```bash
pip install "fastapi-watch[mongo]"
```

`MongoProbe` runs `serverStatus` on the `admin` database to collect version, connection pool stats, memory, and storage engine.

```python
from fastapi_watch.probes import MongoProbe

registry.add(MongoProbe(url="mongodb://localhost:27017"))
```

**Details returned:**

```json
{
  "version": "7.0.5",
  "uptime_seconds": 172800,
  "connections": {
    "current": 12,
    "available": 838,
    "total_created": 150
  },
  "memory_mb": {
    "resident": 128,
    "virtual": 1024
  },
  "storage_engine": "wiredTiger"
}
```

**Constructor arguments:**

| Argument | Default | Description |
|----------|---------|-------------|
| `url` | `"mongodb://localhost:27017"` | MongoDB connection URI |
| `name` | `"mongodb"` | Probe label |
| `server_selection_timeout_ms` | `2000` | How long to wait for a server before giving up |

---

### Watching an HTTP endpoint

```bash
pip install "fastapi-watch[http]"
```

`HttpProbe` performs an HTTP request against an upstream URL and checks the response status code. All standard HTTP methods are supported, so you can probe read and write endpoints alike.

```python
from fastapi_watch.probes import HttpProbe

# GET (default) — simple health check
registry.add(HttpProbe(url="https://api.upstream.com/health"))

# POST — probe a write endpoint, expect 201
registry.add(HttpProbe(
    url="https://api.example.com/items",
    method="POST",
    json={"name": "probe-test"},
    expected_status=201,
    name="items-write",
))

# PUT — probe a replace endpoint
registry.add(HttpProbe(
    url="https://api.example.com/items/probe-test",
    method="PUT",
    json={"name": "probe-test", "active": True},
    name="items-replace",
))

# PATCH — probe a partial-update endpoint
registry.add(HttpProbe(
    url="https://api.example.com/items/probe-test",
    method="PATCH",
    json={"active": False},
    name="items-update",
))

# DELETE — probe a delete endpoint, expect 204
registry.add(HttpProbe(
    url="https://api.example.com/items/probe-test",
    method="DELETE",
    headers={"Authorization": "Bearer <token>"},
    expected_status=204,
    name="items-delete",
))
```

**Details returned:**

```json
{
  "method": "POST",
  "status_code": 201,
  "content_type": "application/json",
  "response_bytes": 43
}
```

`details` is populated for both healthy and unhealthy responses so you can see what status code an upstream actually returned.

**Constructor arguments:**

| Argument | Default | Description |
|----------|---------|-------------|
| `url` | required | URL to request |
| `method` | `"GET"` | HTTP method: `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`, `OPTIONS` |
| `json` | `None` | JSON body sent with the request (ignored for `GET`, `HEAD`, `OPTIONS`) |
| `headers` | `None` | Dict of HTTP headers to include |
| `timeout` | `5.0` | Request timeout in seconds |
| `name` | URL host | Probe label |
| `expected_status` | `200` | HTTP status code considered healthy |

---

### Watching Celery workers

```bash
pip install "fastapi-watch[celery]"
```

`CeleryProbe` uses Celery's [control broadcast API](https://docs.celeryq.dev/en/stable/userguide/monitoring.html#inspection) to inspect every live worker — no extra infrastructure required beyond the broker your app already uses.

Pass your existing Celery application instance directly:

```python
from celery_app import celery
from fastapi_watch.probes import CeleryProbe

registry.add(CeleryProbe(celery))
```

**Details returned (workers online):**

```json
{
  "workers_online": 2,
  "workers": {
    "celery@host1": {
      "status": "online",
      "queues": ["celery", "high_priority"],
      "active_tasks": 1,
      "reserved_tasks": 3,
      "scheduled_tasks": 0,
      "pool": {
        "implementation": "celery.concurrency.prefork:TaskPool",
        "max_concurrency": 4,
        "processes": [101, 102, 103, 104]
      },
      "total_tasks_executed": { "myapp.tasks.send_email": 99 },
      "registered_tasks": ["myapp.tasks.cleanup", "myapp.tasks.send_email"],
      "active": [
        { "id": "abc-123", "name": "myapp.tasks.send_email", "args": ["user@example.com"], "kwargs": {}, "time_start": 1700000000.0, "worker_pid": 101 }
      ],
      "reserved": [],
      "scheduled": []
    }
  }
}
```

**Details returned (no workers online):**

```json
{
  "workers_online": 0,
  "reason": "no workers online — they may be scaled down because there are no pending tasks"
}
```

#### Scale-to-zero and `min_workers`

`min_workers` controls whether the probe considers having zero (or too few) online workers a failure.

| `min_workers` | No workers online | Fewer than `min_workers` |
|---|---|---|
| `0` (default) | **Healthy** — silently explains workers may be scaled down | N/A |
| `≥ 1` | **Unhealthy** — error lists how many are expected | **Unhealthy** — error lists count found vs. expected |

Use `min_workers=0` (the default) for deployments that scale Celery workers to zero when there is no work to do. The probe will report healthy and note the reason, rather than raising a false alarm.

Use `min_workers=1` (or higher) when at least that many workers must always be running — for example, a background processor that needs to be ready at all times regardless of queue depth.

```python
# Workers come and go — zero online is acceptable
registry.add(CeleryProbe(celery))

# At least one worker must always be online
registry.add(CeleryProbe(celery, min_workers=1))

# Fleet of workers — alert if fewer than 2 are responding
registry.add(CeleryProbe(celery, min_workers=2))
```

**Constructor arguments:**

| Argument | Default | Description |
|----------|---------|-------------|
| `app` | required | Your Celery application instance |
| `name` | `"celery"` | Probe label |
| `timeout` | `1.0` | Seconds to wait for each inspector broadcast reply |
| `min_workers` | `0` | Minimum number of workers that must be online. `0` means scale-to-zero is acceptable |

---

### SQLAlchemy engine probe

```bash
pip install "fastapi-watch[sqlalchemy]"
```

`SqlAlchemyProbe` reuses your existing `AsyncEngine` so no extra connections are opened. Works with any database SQLAlchemy supports (PostgreSQL, MySQL, SQLite, etc.).

```python
from sqlalchemy.ext.asyncio import create_async_engine
from fastapi_watch.probes import SqlAlchemyProbe

engine = create_async_engine("postgresql+asyncpg://app_user:secret@localhost/mydb")

registry.add(SqlAlchemyProbe(engine=engine, name="primary-db"))
```

**Details returned:**

```json
{
  "dialect": "postgresql",
  "driver": "asyncpg",
  "server_version": "16.2.0"
}
```

**Constructor arguments:**

| Argument | Default | Description |
|----------|---------|-------------|
| `engine` | required | A SQLAlchemy 2.x `AsyncEngine` instance |
| `name` | `"database"` | Probe label |

---

### All built-in probes

#### Application / infrastructure

| Probe | Extra | Key constructor args | Details fields |
|-------|-------|---------------------|----------------|
| `RequestMetricsMiddleware` + `RequestMetricsProbe` | built-in | `per_route`, `max_error_rate`, `max_avg_rtt_ms` | `request_count`, `error_count`, `error_rate`, `avg_rtt_ms`, `consecutive_errors`; + `routes` dict when `per_route=True` |
| `FastAPIRouteProbe` | built-in | `name`, `max_error_rate`, `max_avg_rtt_ms`, `window_size`, `ema_alpha` | `request_count`, `error_count`, `error_rate`, `consecutive_errors`, `last_status_code`, `last_rtt_ms`, `avg_rtt_ms`, `p95_rtt_ms`, `min_rtt_ms`, `max_rtt_ms`, `requests_per_minute` |
| `FastAPIWebSocketProbe` | built-in | `name`, `max_error_rate`, `min_active_connections`, `window_size`, `ema_alpha` | `active_connections`, `total_connections`, `messages_received`, `messages_sent`, `error_count`, `error_rate`, `consecutive_errors`, `avg_duration_ms`, `min_duration_ms`, `max_duration_ms` |
| `EventLoopProbe` | built-in | `name`, `warn_ms`, `fail_ms` | `lag_ms` |
| `TCPProbe` | built-in | `host`, `port`, `timeout`, `name` | `host`, `port`, `resolved_ips`, `connect_ms` |
| `SMTPProbe` | built-in | `host`, `port`, `timeout`, `use_tls`, `username`, `password`, `name` | `host`, `port`, `server_banner`, `tls`, `extensions` |
| `ThresholdProbe` | built-in | `probe`, `name`, `warn_if`, `fail_if` | (delegates to inner probe) |

#### Databases

| Probe | Extra | Key constructor args | Details fields |
|-------|-------|---------------------|----------------|
| `PostgreSQLProbe` | `postgres` | `url`, `name`, `timeout` | `version`, `active_connections`, `max_connections`, `database_size` |
| `MySQLProbe` | `mysql` | `url` or `host`/`port`/`user`/`password`/`db`, `name`, `connect_timeout` | `version`, `connected_threads`, `uptime_seconds`, `max_used_connections` |
| `SqlAlchemyProbe` | `sqlalchemy` | `engine`, `name` | `dialect`, `driver`, `server_version` |

#### Caches

| Probe | Extra | Key constructor args | Details fields |
|-------|-------|---------------------|----------------|
| `RedisProbe` | `redis` | `url`, `name` | `version`, `uptime_seconds`, `used_memory_human`, `connected_clients`, `role`, `total_keys`, `clusters` |
| `MemcachedProbe` | `memcached` | `host`, `port`, `name`, `pool_size` | — |

#### Queues / messaging

| Probe | Extra | Key constructor args | Details fields |
|-------|-------|---------------------|----------------|
| `RabbitMQProbe` | `rabbitmq` | `url`, `name`, `management_url` | `connected`; + `server`, `totals`, `queues` when `management_url` is set |
| `KafkaProbe` | `kafka` | `bootstrap_servers`, `name`, `request_timeout_ms` | `broker_count`, `controller_id`, `topics`, `internal_topics` |
| `CeleryProbe` | `celery` | `app`, `name`, `timeout`, `min_workers` | `workers_online`, per-worker `active`, `reserved`, `scheduled`, `registered_tasks`, `pool`, `total_tasks_executed`, `queues` |

#### Document stores

| Probe | Extra | Key constructor args | Details fields |
|-------|-------|---------------------|----------------|
| `MongoProbe` | `mongo` | `url`, `name`, `server_selection_timeout_ms` | `version`, `uptime_seconds`, `connections`, `memory_mb`, `storage_engine` |

#### HTTP

| Probe | Extra | Key constructor args | Details fields |
|-------|-------|---------------------|----------------|
| `HttpProbe` | `http` | `url`, `method`, `json`, `headers`, `timeout`, `name`, `expected_status` | `method`, `status_code`, `content_type`, `response_bytes` |

#### Testing / placeholder

| Probe | Extra | Key constructor args | Details fields |
|-------|-------|---------------------|----------------|
| `NoOpProbe` | built-in | `name` | — |

---

## Configuration reference

### `HealthRegistry`

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `app` | `FastAPI` | required | The FastAPI application instance |
| `prefix` | `str` | `"/health"` | URL prefix for all health endpoints |
| `tags` | `list[str]` | `["health"]` | OpenAPI tags applied to all health routes |
| `poll_interval_ms` | `int \| None` | `60000` | How often (ms) to re-run probes while an SSE client is connected. `0` or `None` disables polling — each request or stream event runs probes on demand. Values below `1000` are clamped to `1000`. |
| `logger` | `logging.Logger \| None` | `None` | Logger for warnings (e.g. clamped interval) and probe exception messages. Pass `None` to emit no logs. |
| `grace_period_ms` | `int` | `0` | How long (ms) after startup to return `503 {"status": "starting"}` from `/ready`. `0` disables the grace period. |
| `history_size` | `int` | `120` | Maximum number of past probe results retained per probe. Oldest entry dropped when full. Retrieved via `GET /health/history`. Minimum `1`. |
| `result_ttl_seconds` | `float` | `7200.0` | How long (seconds) probe results are retained. Results older than this are excluded from `/health/history`. Set to `0` to disable time-based expiry. |
| `alert_ttl_seconds` | `float` | `259200.0` | How long (seconds) alert records (state-change events) are retained. Set to `0` to keep until evicted by `max_alerts`. |
| `max_alerts` | `int` | `120` | Hard cap on stored alert records. When full, the oldest alert is dropped before the new one is appended. |
| `storage` | `ProbeStorage \| None` | `None` | Custom storage backend. `None` uses `InMemoryProbeStorage`. When supplied, `history_size`, `result_ttl_seconds`, `alert_ttl_seconds`, and `max_alerts` are ignored — configure limits inside the backend. |
| `timezone` | `str` | `"UTC"` | IANA timezone name for all `checked_at` timestamps. Reflected in the `timezone` field of every response. |
| `routers` | `list[ProbeRouter] \| None` | `None` | One or more `ProbeRouter` instances to include at startup. Equivalent to calling `include_router()` for each. |
| `dashboard` | `bool \| Callable` | `True` | `True` — built-in HTML dashboard at `GET /health/dashboard`. `False` — omit the route. Callable `(report: HealthReport) -> str` — custom renderer. |
| `circuit_breaker` | `bool` | `True` | Enable the circuit breaker. When a probe fails `circuit_breaker_threshold` consecutive times it is suspended for `circuit_breaker_cooldown_ms` ms. |
| `circuit_breaker_threshold` | `int` | `5` | Consecutive failures before the circuit opens. |
| `circuit_breaker_cooldown_ms` | `int` | `600000` | How long (ms) the circuit stays open before the probe is retried (default 10 minutes). |
| `webhook_url` | `str \| None` | `None` | HTTP(S) URL that receives a JSON `POST` whenever a probe changes state. Fire-and-forget; never blocks health checks. Suppressed during maintenance mode. |
| `auth` | `dict \| Callable \| None` | `None` | Authentication for all health endpoints. `None` = open. See [Authentication](#authentication) for accepted forms. |
| `startup_probes` | `list[BaseProbe] \| None` | `None` | Probes that must pass for `/health/startup` to return 200. Evaluated separately from the main registry. |

### `HealthRegistry.set_maintenance(until=None)`

Activates maintenance mode until the given timezone-aware `datetime`. While active, `/health/ready` returns `200 {"status": "maintenance"}` and state-change webhooks are suppressed. Pass `until=None` (default) to set no expiry — use `clear_maintenance()` to exit. Returns `self`.

### `HealthRegistry.clear_maintenance()`

Deactivates maintenance mode immediately. Returns `self`.

### `HealthRegistry.add(probe, critical=True)`

Adds a single probe. Returns `self` for chaining. Adding the same instance more than once is a no-op.

`critical=True` (default) — a failing probe causes the overall status to be `"unhealthy"` and `/ready` to return `503`. Set `critical=False` to include the probe in reports without affecting readiness.

### `HealthRegistry.add_probes(probes, critical=True)`

Adds a list of probes. The `critical` flag applies to every probe in the list. Returns `self` for chaining. Duplicate instances are silently skipped.

### `HealthRegistry.include_router(router)`

Includes all probes from a `ProbeRouter`, preserving each probe's criticality setting. Returns `self` for chaining. Duplicate instances are silently skipped.

```python
registry.include_router(db_router).include_router(payments_router)
```

### `HealthRegistry.on_state_change(callback)`

Registers a callback invoked whenever a probe's status changes between runs. The callback receives `(probe_name: str, old_status: ProbeStatus, new_status: ProbeStatus)` and may be a plain function or an `async` coroutine. Returns `self` for chaining.

### `HealthRegistry.set_poll_interval(ms)`

Updates the poll interval at runtime. Pass `0` or `None` to switch to single-fetch mode. If SSE clients are currently connected the poll task is restarted immediately with the new interval.

```python
registry.set_poll_interval(30_000)   # every 30 s
registry.set_poll_interval(0)        # disable — each event runs probes on demand
```

### `HealthRegistry.set_started()`

Signals that application startup is complete. After this is called, `GET /health/startup` returns `200` (provided all startup probes pass). Call this at the end of your lifespan startup block or application boot sequence.

### `HealthRegistry.run_all()`

Async method — runs every registered probe concurrently and returns `list[ProbeResult]`. Probe exceptions are caught and converted to unhealthy results. Useful for testing or building custom aggregation outside of the mounted routes.

```python
results = await registry.run_all()
for r in results:
    print(r.name, r.status, r.latency_ms, r.details)
```

---

### `ProbeRouter`

Collects probe registrations defined across multiple modules so they can be passed to `HealthRegistry` at startup. Mirrors the `APIRouter` pattern from FastAPI.

| Method | Description |
|--------|-------------|
| `add(probe, critical=True)` | Add a single probe. Returns `self`. Duplicate instances are silently skipped. |
| `add_probes(probes, critical=True)` | Add multiple probes with the same criticality. Returns `self`. |
| `include_router(router)` | Merge another `ProbeRouter`'s probes into this one, preserving each probe's criticality. Returns `self`. |

---

### `FastAPIRouteProbe`

Instruments a FastAPI route handler via the `@probe.watch` decorator and reports real-traffic metrics as a `ProbeResult`.

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `name` | `str` | `"route"` | Probe label shown in health reports |
| `max_error_rate` | `float` | `0.1` | Error-rate threshold (0–1). The probe becomes UNHEALTHY when exceeded. |
| `max_avg_rtt_ms` | `float \| None` | `None` | Average-RTT threshold in milliseconds. `None` disables this threshold. |
| `window_size` | `int` | `100` | Number of recent requests kept for percentile and throughput calculations |
| `ema_alpha` | `float` | `0.1` | EMA smoothing factor (0–1). Higher values make `avg_rtt_ms` react faster to changes. |
| `timeout` | `float \| None` | `None` | Passed to the registry for the `check()` call; not used internally |

**`FastAPIRouteProbe.watch(func)`** — decorator that wraps an `async def` or `def` route handler. Preserves the function's signature so FastAPI dependency injection continues to work. `HTTPException` is caught, its status code recorded, and then re-raised. Any other exception is recorded as a 500 and re-raised.

---

### `BaseProbe`

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | `"unnamed"` | Label used in health reports. Override as a class attribute or set in `__init__`. |
| `timeout` | `float \| None` | `None` | Per-probe timeout in seconds. The check is cancelled and recorded as unhealthy if it exceeds this value. `None` means no limit. |
| `poll_interval_ms` | `int \| None` | `None` | Per-probe poll interval override. When set, this probe runs on its own schedule independent of the registry default. `None` uses the registry `poll_interval_ms`. |
| `circuit_breaker_threshold` | `int \| None` | `None` | Per-probe consecutive-failure threshold before opening the circuit. `None` uses the registry default. |
| `circuit_breaker_cooldown_ms` | `int \| None` | `None` | Per-probe circuit-open cooldown in milliseconds. `None` uses the registry default. |

---

### `ProbeResult`

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` | Probe identifier |
| `status` | `ProbeStatus` | `"healthy"`, `"degraded"`, or `"unhealthy"` |
| `critical` | `bool` | `True` if the probe was registered as critical; affects overall status and readiness |
| `latency_ms` | `float` | Duration of the check in milliseconds |
| `error` | `str \| None` | Error message; only present on failure |
| `details` | `dict \| None` | Service-specific metadata |
| `is_healthy` | `bool` (property) | `True` when `status == "healthy"` (strict) |
| `is_degraded` | `bool` (property) | `True` when `status == "degraded"` |
| `is_passing` | `bool` (property) | `True` when `status != "unhealthy"` (healthy or degraded) |

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
```

Use `/health/ready` for the readiness probe — Kubernetes stops routing traffic to a pod the moment any critical dependency becomes unreachable. Use `/health/live` for liveness so the process is only restarted when it is genuinely stuck, not because an external service is temporarily down.

For applications that need time to warm up (loading models, seeding caches, running migrations), combine `grace_period_ms` with a short `initialDelaySeconds`:

```yaml
readinessProbe:
  httpGet:
    path: /health/ready
    port: 8000
  initialDelaySeconds: 5
  periodSeconds: 10
  failureThreshold: 6   # allow up to 60 s of failures before marking unready
```

```python
# App holds /ready as "starting" for 30 s regardless of probe results
registry = HealthRegistry(app, grace_period_ms=30_000)
```

---

## License

MIT

---

*Claude used to write README, code annotation, help with test case coverage, and clean up my messy thoughts into readable code.*
