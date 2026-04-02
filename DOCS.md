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
- [Configuration reference](#configuration-reference)
- [Kubernetes integration](#kubernetes-integration)

---

## Health dashboard

`GET /health/dashboard` returns a server-rendered HTML page that shows all probe results in a card grid and updates live over SSE. No extra Python dependencies are required.

```python
registry = HealthRegistry(app)                  # dashboard on — GET /health/dashboard
registry = HealthRegistry(app, dashboard=False) # dashboard off
```

**Header bar** — overall status at the top. Green when all critical probes are healthy, amber when any is degraded, red when any is unhealthy. A maintenance banner appears when maintenance mode is active.

**Probe cards** — one card per probe with: colored left border, probe name, optional badge for non-critical probes, average latency, status pill, error message (if failing), and a details table. Hovering a field label shows a one-line tooltip.

**Live updates** — the embedded JavaScript opens an `EventSource` connection to `/health/status/stream` and surgically updates the DOM on each event — no full page reload.

---

## Live streaming

`/health/ready/stream` and `/health/status/stream` use SSE to push probe results to connected clients.

The poll loop starts when the first SSE client connects and stops when the last disconnects. No background work runs when nobody is watching.

Each event is a JSON-encoded health report on a `data:` line:

```
data: {"status": "healthy", "checked_at": "2024-06-01T12:00:00.123456+00:00", "probes": [...]}
```

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

---

## Polling and caching

`GET /health/ready` and `GET /health/status` always respond immediately:

- **With SSE clients connected** — serves the most recent cached result without re-running probes.
- **With no streaming active** — runs probes on demand with a built-in lock to prevent thundering herd.

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

DEGRADED lets a probe communicate "something is wrong but we're still responding" without triggering an emergency. Load balancers keep routing traffic.

Overall status rules (critical probes only):
- Any UNHEALTHY → overall `"unhealthy"`
- Any DEGRADED (no UNHEALTHY) → overall `"degraded"`
- All passing → overall `"healthy"`

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
probe = PostgreSQLProbe(url="postgresql://...")
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

---

## Maintenance mode

While active: `/health/ready` returns `200 {"status": "maintenance"}`, alerters are suppressed, and the dashboard shows an amber banner.

### HTTP endpoints

```bash
# Enable (indefinite)
curl -X POST https://your-app/health/maintenance

# Enable for 30 minutes
curl -X POST https://your-app/health/maintenance \
  -H "Content-Type: application/json" -d '{"minutes": 30}'

# Enable until a specific time
curl -X POST https://your-app/health/maintenance \
  -H "Content-Type: application/json" \
  -d '{"until": "2026-04-01T02:00:00+00:00"}'

# Disable
curl -X DELETE https://your-app/health/maintenance

# Check status
curl https://your-app/health/maintenance
# → {"active": true, "until": "2026-04-01T02:00:00+00:00"}
```

### Deployment workflow

```bash
# 1. Enable maintenance before deploying
curl -X POST https://your-app/health/maintenance -d '{"minutes": 10}'

# 2. Deploy / restart
kubectl rollout restart deployment/my-app

# 3. Clear maintenance (or let the window expire)
curl -X DELETE https://your-app/health/maintenance
```

### Python API

```python
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

registry.set_maintenance()                                                          # indefinite
registry.set_maintenance(until=datetime.now(ZoneInfo("UTC")) + timedelta(hours=2)) # timed
registry.clear_maintenance()
```

Both methods return `self` for chaining. Once `until` passes, maintenance auto-clears.

---

## ProbeGroup — organizing probes across files

`ProbeGroup` mirrors FastAPI's `APIRouter` pattern — declare probes in the modules that own them and compose them at startup.

```python
# features/database/probes.py
from fastapi_watch import ProbeGroup
from fastapi_watch.probes import PostgreSQLProbe, RedisProbe

router = ProbeGroup()
router.add(PostgreSQLProbe(url="postgresql://user:pass@db.internal/app"))
router.add(RedisProbe(url="redis://cache.internal:6379"), critical=False)
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
db_probes.add(PostgreSQLProbe(url="postgresql://..."))
db_probes.add(RedisProbe(url="redis://..."), critical=False)

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
from fastapi_watch.probes import PostgreSQLProbe

registry = HealthRegistry(
    app,
    startup_probes=[PostgreSQLProbe(url="postgresql://...")],
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

Only `/health/ready` is affected — `/health/status` and `/health/live` always reflect real probe results.

---

## State-change callbacks

Callbacks fire whenever a probe's status changes between runs. Receives `(probe_name: str, old_status: ProbeStatus, new_status: ProbeStatus)`.

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

Results are ordered oldest-first and are in-memory by default. See [Custom storage backend](README.md#custom-storage-backend) to persist across restarts.

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

Alerts are ordered oldest-first.

---

## Response format

Every response from `/health/ready`, `/health/status`, and the SSE streams shares this shape.

**Health report:**

| Field | Type | Description |
|-------|------|-------------|
| `status` | `"healthy"` \| `"degraded"` \| `"unhealthy"` | Overall result — critical probes only |
| `checked_at` | `string \| null` | UTC ISO 8601 timestamp of the last probe run |
| `timezone` | `string \| null` | IANA timezone name |
| `probes` | `array` | Individual probe results |

**Probe result:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | `string` | Probe identifier |
| `status` | `"healthy"` \| `"degraded"` \| `"unhealthy"` | State for this probe |
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
                error=str(exc),
            )
```

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
                               latency_ms=round(latency, 2), error=str(exc))

registry.add(S3BucketProbe("my-uploads", "eu-west-1", name="s3-uploads"))
registry.add(S3BucketProbe("my-backups", "us-east-1", name="s3-backups"))
```

### Composite (custom aggregation)

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
                details={"primary": primary.status.value, "replica": replica.status.value},
            )
        return ProbeResult(
            name=self.name,
            status=ProbeStatus.UNHEALTHY,
            error=f"both nodes down — primary: {primary.error}, replica: {replica.error}",
        )
```

### Testing a custom probe

```python
import pytest
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
    result = await MyServiceProbe().check()
    assert result.status == ProbeStatus.UNHEALTHY
    assert "refused" in result.error
```

### Checklist

- `name` must be set — class attribute or `self.name = ...` in `__init__`.
- `check()` must be `async` and return `ProbeResult`.
- Set `latency_ms` for probes where response time matters.
- Set `timeout` if the underlying call can hang indefinitely.
- Do not call `registry.run_all()` from inside `check()`.

---

## Route auto-discovery

fastapi-watch provides three ways to instrument FastAPI routes. They have a fixed priority order and are safe to combine — when multiple approaches target the same route, the highest-priority one wins and the others skip it silently.

### Instrumentation priority

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

Instruments every route on a specific `APIRouter` with shared settings. Call it after `app.include_router` — FastAPI must have already baked the routes into `app.routes` before `watch_router` can find them. Skips routes already decorated with `@probe.watch`. Overrides any `discover_routes` instrumentation on the same routes.

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
| `router` | required | The `APIRouter` to instrument |
| `tags` | `None` | Extra tags appended to every probe (FastAPI route tags are also merged in automatically) |
| `critical` | `True` | Whether auto-created probes are critical |
| `exclude_paths` | `None` | Route paths to skip; supports fnmatch glob patterns (e.g. `["/admin/*"]`) |
| `include_methods` | `None` | Only instrument routes whose HTTP method matches (e.g. `["GET", "POST"]`); WebSocket routes are always included |
| `ws_probe_kwargs` | `None` | Extra kwargs forwarded to each `FastAPIWebSocketProbe` |
| `**probe_kwargs` | — | Forwarded to each `FastAPIRouteProbe` |

### `discover_routes` — lowest priority

Scans all registered routes and instruments any that haven't already been covered. Call it from a lifespan startup handler so all `include_router` calls have already run. Routes under the health prefix are always excluded automatically.

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
| `include_methods` | `None` | Only instrument routes whose HTTP method matches (e.g. `["GET", "POST"]`); WebSocket routes always pass |
| `critical` | `True` | Whether auto-created probes are critical |
| `tags` | `None` | Extra tags appended to every probe (FastAPI route tags are also merged in automatically) |
| `ws_probe_kwargs` | `None` | Extra kwargs forwarded to each `FastAPIWebSocketProbe` (e.g. `{"min_active_connections": 1}`) |
| `refresh` | `False` | Re-instrument previously auto-discovered routes with updated options. `@probe.watch` and `watch_router` routes are never overridden even with `refresh=True` |
| `**probe_kwargs` | — | Forwarded to each `FastAPIRouteProbe` (e.g. `max_error_rate=0.05`) |

**Probe naming:** each probe is named after the handler function (`route.name`), which FastAPI sets from the function name by default — so probes have readable names like `list_users` and `create_order` without any configuration.

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

```python
from fastapi_watch import HealthRegistry, RequestMetricsMiddleware, RequestMetricsProbe

app = FastAPI()
middleware = RequestMetricsMiddleware(app, per_route=True)
probe = RequestMetricsProbe(middleware, max_error_rate=0.05, max_avg_rtt_ms=500)
registry = HealthRegistry(app, poll_interval_ms=None)
registry.add(probe)

# In production: uvicorn myapp:middleware
```

> Create the middleware yourself rather than via `app.add_middleware()` — `add_middleware()` creates an internal copy the probe can't see.

`per_route=True` breaks stats down by route template (e.g. `/users/{user_id}`). `per_route=False` collects aggregates only.

**`RequestMetricsMiddleware` args:**

| Argument | Default | Description |
|----------|---------|-------------|
| `per_route` | `True` | Per-route-template breakdown |
| `window_size` | `200` | RTT sliding window size |
| `ema_alpha` | `0.1` | EMA smoothing factor |
| `min_error_status` | `500` | Status codes counted as errors (`500` = 5xx only; `400` = 4xx and 5xx) |

**`RequestMetricsProbe` args:**

| Argument | Default | Description |
|----------|---------|-------------|
| `middleware` | required | The `RequestMetricsMiddleware` instance |
| `name` | `"request_metrics"` | Probe label |
| `max_error_rate` | `0.1` | Error-rate threshold |
| `max_avg_rtt_ms` | `None` | RTT threshold; `None` disables it |

---

### FastAPI route probe

`FastAPIRouteProbe` collects real-traffic metrics from a FastAPI route — no synthetic requests.

For most apps, [route auto-discovery](#route-auto-discovery) is the easiest way to instrument all routes at once. Use `FastAPIRouteProbe` directly when a specific route needs its own thresholds, description, or tags.

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
| `error_count` | Requests at or above `min_error_status` |
| `error_rate` | `error_count / request_count` |
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
| `window_size` | `100` | Window for RTT percentiles, throughput, slow-call counts, and outcome tracking |
| `ema_alpha` | `0.1` | EMA smoothing factor |
| `timeout` | `None` | Passed to registry for `check()` |
| `min_error_status` | `500` | Minimum status code counted as an error |
| `slow_call_threshold_ms` | `None` | Requests above this threshold increment `slow_calls`; disabled when `None` |
| `cache_window_size` | `None` | Override the cache hit/miss window to the last N lookups; auto-detected from the LRU cache's `maxsize` when not set |
| `cache_time_window_s` | `120.0` | When `cache_window_size` is not set, count hits/misses over the last T seconds (default 120 s) |
| `cache_reporting` | `True` | Set to `False` to disable automatic cache tracking even when wrapping `@lru_cache` / `@alru_cache` functions |
| `circuit_breaker_enabled` | `True` | Set to `False` to disable the circuit breaker for this probe regardless of the registry setting |

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

`FastAPIWebSocketProbe` passively instruments a WebSocket handler.

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

`EventLoopProbe` measures asyncio event loop blocking by timing a zero-delay `asyncio.sleep(0)`.

```python
from fastapi_watch.probes import EventLoopProbe

registry.add(EventLoopProbe(warn_ms=5.0, fail_ms=20.0))
```

Details: `lag_ms`, `warn_ms`, `fail_ms`. Returns DEGRADED above `warn_ms`, UNHEALTHY above `fail_ms`.

---

### TCP / DNS reachability

`TCPProbe` resolves a hostname and opens a TCP connection. No extra install.

```python
from fastapi_watch.probes import TCPProbe

registry.add(TCPProbe(host="db.internal", port=5432))
registry.add(TCPProbe(host="redis.internal", port=6379, name="redis-tcp", timeout=2.0))
```

Details: `host`, `port`, `resolved_ips`, `connect_ms`.

---

### SMTP

`SMTPProbe` passively instruments outgoing email calls via `@probe.watch`. Works with any SMTP library.

```python
from fastapi_watch.probes import SMTPProbe

smtp_probe = SMTPProbe(name="sendgrid", max_error_rate=0.05)

@smtp_probe.watch
async def send_welcome_email(to: str) -> None:
    async with aiosmtplib.SMTP("smtp.sendgrid.net", port=587) as smtp:
        await smtp.login("apikey", os.environ["SENDGRID_API_KEY"])
        await smtp.sendmail(FROM, to, message.as_string())

registry.add(smtp_probe)
```

Details: `call_count`, `error_count`, `error_rate`, `consecutive_errors`, `last_rtt_ms`, `avg_rtt_ms`, `p50_rtt_ms`, `p95_rtt_ms`, `p99_rtt_ms`, `min_rtt_ms`, `max_rtt_ms`, `error_types`, `last_error_at` (always when errors exist), `last_success_at` (when ≥99% failures).

---

### Threshold wrapper

`ThresholdProbe` wraps any probe and promotes its result to DEGRADED or UNHEALTHY based on values in `details` — without modifying the inner probe.

```python
from fastapi_watch.probes import ThresholdProbe, RedisProbe

redis = RedisProbe(url="redis://localhost:6379")

registry.add(ThresholdProbe(
    probe=redis,
    name="redis-keys",
    warn_if=lambda d: d.get("total_keys", 0) > 500_000,
    fail_if=lambda d: d.get("total_keys", 0) > 1_000_000,
))
```

If the inner probe returns UNHEALTHY, it passes through unchanged. `fail_if` takes precedence over `warn_if`.

---

### PostgreSQL

```bash
pip install "fastapi-watch[postgres]"
```

`PostgreSQLProbe` passively instruments PostgreSQL calls via `@probe.watch`.

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

If `management_url` is unreachable, the probe still reports AMQP connectivity.

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

Details: `broker_count`, `controller_id`, `topics`, `internal_topics`.

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

`HttpProbe` passively instruments outgoing HTTP calls — no synthetic requests.

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
| `RequestMetricsMiddleware` + `RequestMetricsProbe` | built-in | `per_route`, `max_error_rate`, `max_avg_rtt_ms` | `request_count`, `error_count`, `error_rate`, `avg_rtt_ms`, `consecutive_errors`; + `routes` when `per_route=True` |
| `FastAPIRouteProbe` | built-in | `name`, `max_error_rate`, `max_avg_rtt_ms`, `min_error_status`, `slow_call_threshold_ms`, `cache_window_size`, `cache_time_window_s`, `cache_reporting`, `circuit_breaker_enabled` | `request_count`, `error_count`, `error_rate`, `consecutive_errors`, `last_status_code`, `last_rtt_ms`, `avg_rtt_ms`, `p50_rtt_ms`, `p95_rtt_ms`, `p99_rtt_ms`, `min_rtt_ms`, `max_rtt_ms`, `requests_per_minute`, `slow_calls`\*, `status_distribution`, `error_types`, `cache_hits`\*, `cache_misses`\*, `cache_maxsize`\*, `cache_currsize`\*, `last_error_at`\*, `last_success_at`\* |
| `FastAPIWebSocketProbe` | built-in | `name`, `max_error_rate`, `min_active_connections` | `active_connections`, `total_connections`, `messages_received`, `messages_sent`, `error_count`, `error_rate`, `consecutive_errors`, `avg_duration_ms` |
| `EventLoopProbe` | built-in | `warn_ms`, `fail_ms` | `lag_ms` |
| `TCPProbe` | built-in | `host`, `port`, `timeout` | `host`, `port`, `resolved_ips`, `connect_ms` |
| `SMTPProbe` | built-in | `name`, `max_error_rate`, `max_avg_rtt_ms`, `slow_call_threshold_ms`, `cache_window_size` | `call_count`, `error_count`, `error_rate`, `avg_rtt_ms`, `p50_rtt_ms`, `p95_rtt_ms`, `p99_rtt_ms`, `error_types`, `last_error_at`\* |
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
| `KafkaProbe` | `kafka` | `bootstrap_servers` | `broker_count`, `controller_id`, `topics` |
| `CeleryProbe` | `celery` | `app`, `min_workers`, `detailed` | `workers_online`, `workers` |

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
| `history_size` | `int` | `120` | Max results per probe |
| `result_ttl_seconds` | `float` | `7200.0` | How long results are retained |
| `alert_ttl_seconds` | `float` | `259200.0` | How long alert records are retained |
| `max_alerts` | `int` | `120` | Hard cap on stored alerts |
| `storage` | `ProbeStorage \| None` | `None` | Custom storage backend |
| `timezone` | `str` | `"UTC"` | IANA timezone for `checked_at` timestamps |
| `groups` | `list[ProbeGroup] \| None` | `None` | ProbeGroups to include at startup |
| `dashboard` | `bool \| Callable` | `True` | `True` = built-in dashboard; `False` = disabled; Callable = custom renderer |
| `circuit_breaker` | `bool` | `True` | Enable/disable circuit breaker |
| `circuit_breaker_threshold` | `int` | `5` | Consecutive failures before circuit opens |
| `circuit_breaker_cooldown_ms` | `int` | `600000` | Cooldown before probe is retried (10 min) |
| `webhook_url` | `str \| None` | `None` | Legacy — wraps into `WebhookAlerter` |
| `auth` | `dict \| Callable \| None` | `None` | Authentication for all health endpoints |
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
| `run_all()` | Async — run all probes concurrently, return `list[ProbeResult]`. |

### `BaseProbe` attributes

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | `"unnamed"` | Label in health reports |
| `timeout` | `float \| None` | `None` | Per-probe timeout in seconds |
| `poll_interval_ms` | `int \| None` | `None` | Per-probe poll interval override |
| `circuit_breaker_threshold` | `int \| None` | `None` | Per-probe circuit threshold |
| `circuit_breaker_cooldown_ms` | `int \| None` | `None` | Per-probe circuit cooldown |

### `ProbeResult` fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` | Probe identifier |
| `status` | `ProbeStatus` | `"healthy"`, `"degraded"`, or `"unhealthy"` |
| `critical` | `bool` | Affects overall status and readiness |
| `latency_ms` | `float` | Check duration in milliseconds |
| `error` | `str \| None` | Error message on failure |
| `details` | `dict \| None` | Service-specific metadata |
| `is_healthy` | `bool` | `status == "healthy"` |
| `is_degraded` | `bool` | `status == "degraded"` |
| `is_passing` | `bool` | `status != "unhealthy"` |

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
