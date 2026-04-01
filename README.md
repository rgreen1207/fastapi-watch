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
</p>

---

Add `/health/*` endpoints to any FastAPI app in one line. Probes run concurrently, stream live results over SSE, and expose a Prometheus-compatible metrics endpoint. Built-in probes cover databases, caches, queues, HTTP services, and FastAPI routes. Passive probes observe real traffic — no synthetic requests.

For full documentation see **[DOCS.md](DOCS.md)**.

---

## Installation

```bash
pip install fastapi-watch

# With service-specific extras
pip install "fastapi-watch[postgres]"
pip install "fastapi-watch[redis]"
pip install "fastapi-watch[postgres,redis,rabbitmq]"
pip install "fastapi-watch[all]"
```

> **zsh users:** quote the package name to avoid glob expansion: `pip install "fastapi-watch[redis]"`

Available extras: `postgres`, `mysql`, `sqlalchemy`, `redis`, `memcached`, `rabbitmq`, `kafka`, `mongo`, `celery`

---

## Quick start

```python
import logging
from fastapi import FastAPI
from fastapi_watch import HealthRegistry
from fastapi_watch.probes import PostgreSQLProbe, RedisProbe

app = FastAPI()

registry = HealthRegistry(
    app,
    poll_interval_ms=60_000,
    logger=logging.getLogger(__name__),
)

registry.add(PostgreSQLProbe(url="postgresql://user:pass@localhost/mydb"))
registry.add(RedisProbe(url="redis://localhost:6379"), critical=False)
```

Health endpoints are now live. See [Endpoints](#endpoints) for the full list.

---

## Endpoints

| Endpoint | Purpose | Healthy | Unhealthy |
|---|---|---|---|
| `GET /health/live` | Liveness — process is alive | `200` | `200` |
| `GET /health/ready` | Readiness — all critical probes passing | `200` | `503` |
| `GET /health/status` | Full probe detail | `200` | `207` |
| `GET /health/history` | Rolling result history per probe | `200` | `200` |
| `GET /health/alerts` | Probe state-change log | `200` | `200` |
| `GET /health/metrics` | Prometheus text format 0.0.4 | `200` | `200` |
| `GET /health/startup` | Startup gate; 503 until `set_started()` | `200` | `503` |
| `GET /health/dashboard` | Live HTML dashboard (SSE) | `200` | `200` |
| `GET /health/ready/stream` | SSE stream of readiness | stream | stream |
| `GET /health/status/stream` | SSE stream of full probe detail | stream | stream |
| `GET /health/maintenance` | Maintenance mode status | `200` | `200` |
| `POST /health/maintenance` | Enable maintenance mode | `200` | `200` |
| `DELETE /health/maintenance` | Disable maintenance mode | `200` | `200` |

The prefix defaults to `/health` and is configurable: `HealthRegistry(app, prefix="/ops/health")`.

### Prometheus

`GET /health/metrics` returns Prometheus text format 0.0.4. Scrape it directly — no extra dependencies.

```yaml
# prometheus.yml
scrape_configs:
  - job_name: myapp
    static_configs:
      - targets: ["localhost:8000"]
    metrics_path: /health/metrics
```

Exported metrics: `probe_healthy`, `probe_degraded`, `probe_latency_ms`, `probe_circuit_open`, `probe_circuit_consecutive_failures`, `probe_circuit_trips_total`.

---

## Probes

### Active probe — polls a dependency on a timer

Active probes make outgoing calls to verify a dependency is reachable.

```python
from fastapi_watch.probes import PostgreSQLProbe, TCPProbe

# Built-in database probe
registry.add(PostgreSQLProbe(url="postgresql://user:pass@localhost/mydb"))

# TCP reachability — no extra install
registry.add(TCPProbe(host="redis.internal", port=6379, timeout=2.0))
```

### Passive probe — instruments real traffic via `@probe.watch`

Passive probes observe calls your code already makes — no synthetic requests, no rate limit risk.

```python
from fastapi_watch import FastAPIRouteProbe
from fastapi_watch.probes import RedisProbe

# Instrument a FastAPI route handler
users_probe = FastAPIRouteProbe(name="users-api", max_error_rate=0.05, max_avg_rtt_ms=300)

@app.get("/users")
@users_probe.watch
async def list_users():
    return {"users": [...]}

registry.add(users_probe)

# Instrument outgoing Redis calls
redis_probe = RedisProbe(name="cache", max_error_rate=0.05)

@redis_probe.watch
async def get_session(session_id: str):
    return await redis_client.hgetall(f"session:{session_id}")

registry.add(redis_probe)
```

Passive probes collect: `call_count`, `error_count`, `error_rate`, `avg_rtt_ms`, `p50_rtt_ms`, `p95_rtt_ms`, `p99_rtt_ms`, `min_rtt_ms`, `max_rtt_ms`, `consecutive_errors`, `error_types`, plus `last_error_at` (always after first error) and `last_success_at` (when mostly failing). `FastAPIRouteProbe` additionally tracks `last_status_code`, `requests_per_minute`, and `status_distribution`. Optional: `slow_calls` (when `slow_call_threshold_ms` is set) and `cache_hits`/`cache_misses` (via `record_cache_hit()` / `record_cache_miss()`).

### Custom probe

Extend `BaseProbe` and implement `check()`. Any unhandled exception is caught by the registry and recorded as unhealthy automatically.

```python
import time
from fastapi_watch.probes import BaseProbe
from fastapi_watch.models import ProbeResult, ProbeStatus

class PaymentGatewayProbe(BaseProbe):
    name = "payment-gateway"
    timeout = 5.0  # fail if check takes longer than 5 s

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

registry.add(PaymentGatewayProbe())
```

### Critical vs non-critical

```python
registry.add(PostgreSQLProbe(url="..."))              # critical — 503 if it fails
registry.add(RedisProbe(url="..."), critical=False)   # non-critical — visible but never causes 503
```

### Probe management

```python
registry.add(probe)                      # add one
registry.add_probes([a, b, c])           # add many
registry.add(probe).add(other)           # chainable
```

### ProbeGroup — split probes across files

```python
# db/probes.py
from fastapi_watch import ProbeGroup
from fastapi_watch.probes import PostgreSQLProbe

router = ProbeGroup()
router.add(PostgreSQLProbe(url="postgresql://..."))
```

```python
# main.py
from fastapi_watch import HealthRegistry
from db.probes import router as db_probes

registry = HealthRegistry(app, groups=[db_probes])
```

---

## Authentication

```python
# HTTP Basic
registry = HealthRegistry(app, auth={"type": "basic", "username": "admin", "password": "s3cr3t"})

# API key header (default header: X-API-Key)
registry = HealthRegistry(app, auth={"type": "apikey", "key": "my-secret-key"})

# Custom callable — sync or async
from fastapi import Request

def my_auth(request: Request) -> bool:
    return request.headers.get("X-Internal-Token") == "expected"

registry = HealthRegistry(app, auth=my_auth)
```

---

## Alerting

Alerters fire on every probe state transition (`healthy → unhealthy`, etc.). They are fire-and-forget — a failing alerter never blocks health checks.

```python
from fastapi_watch.alerts import SlackAlerter, PagerDutyAlerter, WebhookAlerter

registry = HealthRegistry(
    app,
    alerters=[
        SlackAlerter(webhook_url="https://hooks.slack.com/services/T.../B.../..."),
        PagerDutyAlerter(routing_key="your-routing-key"),
    ],
)
```

### Slack

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **Incoming Webhooks** → toggle on → **Add New Webhook to Workspace**.
2. Copy the webhook URL.

```python
import os
from fastapi_watch.alerts import SlackAlerter

SlackAlerter(
    webhook_url=os.environ["SLACK_WEBHOOK_URL"],
    channel="#ops-alerts",    # optional — overrides the webhook default
    username="fastapi-watch", # optional
)
```

Messages are color-coded: green for recovery, amber for degraded, red for unhealthy.

### Custom alerter

```python
from fastapi_watch.alerts import BaseAlerter
from fastapi_watch.models import AlertRecord

class SMSAlerter(BaseAlerter):
    async def notify(self, alert: AlertRecord) -> None:
        await send_sms(
            f"[health] {alert.probe}: "
            f"{alert.old_status.value} → {alert.new_status.value}"
        )

registry = HealthRegistry(app, alerters=[SMSAlerter()])
```

`AlertRecord` fields: `probe` (str), `old_status` (ProbeStatus), `new_status` (ProbeStatus), `timestamp` (datetime).

---

## Custom storage backend

By default results and alerts are in-memory. Pass a custom `storage` to persist across restarts or share state across instances.

```python
from fastapi_watch import HealthRegistry

class MyRedisStorage:
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

Any class implementing all eight methods satisfies the `ProbeStorage` protocol — no inheritance required. See `storage.py` for an annotated Redis implementation sketch.

---

## Maintenance mode

While active, `/health/ready` returns `200 {"status": "maintenance"}` and alerters are suppressed.

```bash
# Enable (indefinite)
curl -X POST https://your-app/health/maintenance

# Enable for 30 minutes
curl -X POST https://your-app/health/maintenance \
  -H "Content-Type: application/json" -d '{"minutes": 30}'

# Disable
curl -X DELETE https://your-app/health/maintenance
```

```python
# Python API
registry.set_maintenance()                          # indefinite
registry.set_maintenance(until=datetime.now(UTC) + timedelta(hours=2))
registry.clear_maintenance()
```

---

## License

MIT

---

*Claude used to write README, code annotation, help with test case coverage, and clean up my messy thoughts into readable code.*
