<h1 align="center">fastapi-watch</h1>
<p align="center">
  <em>Structured health and readiness checks for <a href="https://fastapi.tiangolo.com/">FastAPI</a>.</em>
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
  <a href="https://pepy.tech/projects/fastapi-watch">
    <img src="https://static.pepy.tech/personalized-badge/fastapi-watch?period=total&units=NONE&left_color=GREY&right_color=GREEN&left_text=downloads" alt="PyPI Downloads">
  </a>
</p>

---

Add `/health/*` endpoints to any FastAPI app in minutes. Probes observe real traffic — no synthetic requests — and stream live results to a built-in dashboard and Prometheus endpoint.

For full documentation see **[DOCS.md](DOCS.md)**.

---

## Installation

```bash
pip install fastapi-watch

# With service-specific extras
pip install "fastapi-watch[postgres,redis]"
pip install "fastapi-watch[all]"
```

> **zsh users:** quote extras to avoid glob expansion: `pip install "fastapi-watch[redis]"`

Available extras: `postgres`, `mysql`, `sqlalchemy`, `redis`, `memcached`, `rabbitmq`, `kafka`, `mongo`, `celery`

---

## Quick start

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi_watch import HealthRegistry
from fastapi_watch.probes import PostgreSQLProbe, RedisProbe

app = FastAPI()
registry = HealthRegistry(app)

# Infrastructure probes
registry.add(PostgreSQLProbe(url="postgresql://user:pass@localhost/mydb"))
registry.add(RedisProbe(url="redis://localhost:6379"), critical=False)

@asynccontextmanager
async def lifespan(app):
    # Automatically instrument every route — no decorators needed
    registry.discover_routes(tags=["api"], max_error_rate=0.05)
    registry.set_started()
    yield

app = FastAPI(lifespan=lifespan)
```

Every route is now monitored with real-traffic data. Health endpoints are live at `/health/*`.

---

## Endpoints

| Endpoint | Purpose | Status |
|---|---|---|
| `GET /health/live` | Liveness — process is alive | always `200` |
| `GET /health/ready` | Readiness — all critical probes passing | `200` / `503` |
| `GET /health/status` | Full probe detail | `200` / `207` |
| `GET /health/dashboard` | Live HTML dashboard (SSE) | `200` |
| `GET /health/metrics` | Prometheus text format 0.0.4 | `200` |
| `GET /health/history` | Rolling result history per probe | `200` |
| `GET /health/alerts` | Probe state-change log | `200` |
| `GET /health/startup` | Startup gate; 503 until `set_started()` | `200` / `503` |
| `GET /health/ready/stream` | SSE stream of readiness | stream |
| `GET /health/status/stream` | SSE stream of full probe detail | stream |
| `GET /health/maintenance` | Maintenance mode status | `200` |
| `POST /health/maintenance` | Enable maintenance mode | `200` |
| `DELETE /health/maintenance` | Disable maintenance mode | `200` |

The prefix defaults to `/health` and is configurable: `HealthRegistry(app, prefix="/ops/health")`.

---

## Route monitoring

The fastest way to monitor your API is auto-discovery. fastapi-watch instruments real traffic — no synthetic polling, no wasted requests.

### `discover_routes` — instrument everything at once

One call after all routers are included. Every route gets a passive probe with no decorators required.

```python
@asynccontextmanager
async def lifespan(app):
    registry.discover_routes(
        tags=["api"],           # tag all probes for filtering
        max_error_rate=0.05,    # alert if error rate exceeds 5%
        exclude_paths=["/internal/*"],
    )
    registry.set_started()
    yield
```

Auto-discovered probes use `GET /items/{id}` style descriptions so they're immediately recognizable in the dashboard.

### `watch_router` — instrument a specific router

Scope monitoring to one router with its own tags, thresholds, and criticality. Call it after `app.include_router`.

```python
app.include_router(users_router, prefix="/users")
app.include_router(orders_router, prefix="/orders")

registry.watch_router(users_router, tags=["users"], max_error_rate=0.01)
registry.watch_router(orders_router, tags=["orders"], critical=False)
```

Then filter health checks by router: `GET /health/ready?tag=users`

### `@probe.watch` — full control on one route

For routes that need custom thresholds, a specific name, or tight SLOs.

```python
from fastapi_watch import FastAPIRouteProbe

checkout_probe = FastAPIRouteProbe(
    name="checkout",
    description="Payment processing",
    tags=["payments"],
    max_error_rate=0.001,
    slow_call_threshold_ms=200,
)

@app.post("/checkout")
@checkout_probe.watch
async def checkout():
    ...

registry.add(checkout_probe)
```

### Priority system

When all three approaches are used together, each route is instrumented exactly once — the highest-priority wins:

| Priority | Method | When to use |
|----------|--------|-------------|
| 1 — highest | `@probe.watch` | One route needs its own thresholds or name |
| 2 | `watch_router` | A whole router shares settings |
| 3 — lowest | `discover_routes` | Catch-all for everything not handled explicitly |

`discover_routes` and `watch_router` skip any route already covered by a higher-priority method — no conflicts, no double counting.

---

## Tag-based filtering

All probes accept `tags=[...]`. FastAPI route tags (`@app.get("/items", tags=["store"])`) are automatically merged in. Filter any endpoint by tag:

```bash
GET /health/ready?tag=payments         # only payment probes
GET /health/status?tag=users,orders    # users OR orders probes
GET /health/status/stream?tag=payments # filtered live stream
```

The dashboard shows tag chips on each probe card and a clickable filter bar to isolate groups at a glance.

---

## Infrastructure probes

```python
from fastapi_watch.probes import PostgreSQLProbe, RedisProbe, HttpProbe, TCPProbe

registry.add(PostgreSQLProbe(url="postgresql://..."))
registry.add(RedisProbe(url="redis://..."), critical=False)
registry.add(HttpProbe(name="payments-api"), critical=True)
registry.add(TCPProbe(host="kafka.internal", port=9092))
```

Passive probes observe real calls — use `@probe.watch` on any function to track its latency, error rate, and throughput without making synthetic requests.

---

## Alerting

```python
from fastapi_watch.alerts import SlackAlerter, PagerDutyAlerter

registry = HealthRegistry(
    app,
    alerters=[
        SlackAlerter(webhook_url="https://hooks.slack.com/..."),
        PagerDutyAlerter(routing_key="your-routing-key"),
    ],
)
```

---

## License

MIT

---

*Claude used to write README, code annotation, help with test case coverage, and clean up my messy thoughts into readable code.*
