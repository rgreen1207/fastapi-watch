# fastapi-watch

Structured health and readiness check system for [FastAPI](https://fastapi.tiangolo.com/).

Add `/health/live`, `/health/ready`, and `/health/status` endpoints to any FastAPI app with a single registry call. All probes run concurrently so a slow dependency never blocks the others.

---

## Table of contents

- [Installation](#installation)
- [How it works](#how-it-works)
- [Endpoints](#endpoints)
- [Response format](#response-format)
- [Watching PostgreSQL](#watching-postgresql)
- [Watching RabbitMQ](#watching-rabbitmq)
- [Watching Redis](#watching-redis)
- [Writing a custom probe](#writing-a-custom-probe)
- [All built-in probes](#all-built-in-probes)
- [Configuration reference](#configuration-reference)
- [Kubernetes integration](#kubernetes-integration)
- [License](#license)

---

## Installation

Install only the extras you actually use. Nothing is pulled in by default beyond FastAPI and Pydantic.

```bash
# Core package — includes the always-passing MemoryProbe, no other deps
pip install fastapi-watch

# Add individual service probes as needed
pip install fastapi-watch[postgres]     # PostgreSQL  (asyncpg)
pip install fastapi-watch[mysql]        # MySQL / MariaDB  (aiomysql)
pip install fastapi-watch[sqlalchemy]   # Any SQLAlchemy 2.x async engine
pip install fastapi-watch[redis]        # Redis  (aioredis)
pip install fastapi-watch[memcached]    # Memcached  (aiomcache)
pip install fastapi-watch[rabbitmq]     # RabbitMQ  (aio-pika)
pip install fastapi-watch[kafka]        # Kafka  (aiokafka)
pip install fastapi-watch[mongo]        # MongoDB  (motor)
pip install fastapi-watch[http]         # Upstream HTTP endpoint  (aiohttp)

# Or pull everything in one shot
pip install fastapi-watch[all]
```

Multiple extras can be combined:

```bash
pip install fastapi-watch[postgres,redis,rabbitmq]
```

---

## How it works

Create a `HealthRegistry`, attach it to your FastAPI `app`, and call `.add()` for each service you want to monitor. The registry mounts the three health endpoints automatically and runs every probe concurrently on each request.

```python
from fastapi import FastAPI
from fastapi_watch import HealthRegistry

app = FastAPI()
registry = HealthRegistry(app)
```

That's enough to get working liveness/readiness endpoints. Add probes one at a time:

```python
registry.add(probe_a)
registry.add(probe_b)

# or chain them
registry.add(probe_a).add(probe_b).add(probe_c)
```

---

## Endpoints

| Endpoint | Purpose | Status when healthy | Status when degraded |
|---|---|---|---|
| `GET /health/live` | **Liveness** — is the process alive? | `200 OK` | never fails |
| `GET /health/ready` | **Readiness** — are all probes passing? | `200 OK` | `503 Service Unavailable` |
| `GET /health/status` | **Status** — full detail on every probe | `200 OK` | `207 Multi-Status` |

The prefix defaults to `/health` and can be changed:

```python
registry = HealthRegistry(app, prefix="/ops/health")
# → /ops/health/live, /ops/health/ready, /ops/health/status
```

---

## Response format

All three endpoints return the same JSON shape.

**All healthy — `200`**
```json
{
  "status": "healthy",
  "probes": [
    { "name": "postgresql", "status": "healthy",   "latency_ms": 1.8 },
    { "name": "redis",      "status": "healthy",   "latency_ms": 0.6 },
    { "name": "rabbitmq",   "status": "healthy",   "latency_ms": 3.2 }
  ]
}
```

**One probe failing — `503` (ready) / `207` (status)**
```json
{
  "status": "unhealthy",
  "probes": [
    { "name": "postgresql", "status": "healthy",   "latency_ms": 1.8 },
    { "name": "redis",      "status": "unhealthy", "latency_ms": 5002.1, "error": "Connection refused" },
    { "name": "rabbitmq",   "status": "healthy",   "latency_ms": 3.2 }
  ]
}
```

The `error` field is only present when a probe fails. `latency_ms` always reflects how long that individual probe took, regardless of outcome.

---

## Watching PostgreSQL

**Install the extra:**

```bash
pip install fastapi-watch[postgres]
```

`PostgreSQLProbe` uses `asyncpg` directly — no SQLAlchemy required. It opens a connection, runs a configurable query, then closes the connection on every check.

```python
from fastapi import FastAPI
from fastapi_watch import HealthRegistry
from fastapi_watch.probes import PostgreSQLProbe

app = FastAPI()
registry = HealthRegistry(app)

registry.add(
    PostgreSQLProbe(
        url="postgresql://app_user:secret@localhost:5432/mydb",
        name="primary-db",   # label shown in health responses (default: "postgresql")
    )
)
```

**Checking a read replica separately:**

```python
registry.add(PostgreSQLProbe(url="postgresql://reader:secret@replica.host/mydb", name="replica-db"))
```

**Custom health query** — useful when you want to verify something beyond connectivity, such as checking that a migration has run:

```python
registry.add(
    PostgreSQLProbe(
        url="postgresql://app_user:secret@localhost/mydb",
        name="primary-db",
        query="SELECT COUNT(*) FROM schema_migrations",
    )
)
```

**With a connection timeout** (default is 5 seconds):

```python
registry.add(
    PostgreSQLProbe(
        url="postgresql://app_user:secret@localhost/mydb",
        timeout=2.0,
    )
)
```

**If you are already using SQLAlchemy**, use `SqlAlchemyProbe` instead to reuse your existing engine and avoid opening extra connections:

```bash
pip install fastapi-watch[sqlalchemy]
```

```python
from sqlalchemy.ext.asyncio import create_async_engine
from fastapi_watch.probes import SqlAlchemyProbe

engine = create_async_engine("postgresql+asyncpg://app_user:secret@localhost/mydb")

registry.add(SqlAlchemyProbe(engine=engine, name="primary-db"))
```

---

## Watching RabbitMQ

**Install the extra:**

```bash
pip install fastapi-watch[rabbitmq]
```

`RabbitMQProbe` opens a TCP connection to the broker and immediately closes it. No channels, queues, or messages are created.

```python
from fastapi import FastAPI
from fastapi_watch import HealthRegistry
from fastapi_watch.probes import RabbitMQProbe

app = FastAPI()
registry = HealthRegistry(app)

registry.add(
    RabbitMQProbe(
        url="amqp://guest:guest@localhost:5672/",
        name="rabbitmq",   # default
    )
)
```

**Using a dedicated monitoring vhost** (recommended for production so the health check is isolated from application traffic):

```python
registry.add(RabbitMQProbe(url="amqp://monitor:secret@rabbitmq.internal:5672/monitoring"))
```

**TLS / AMQPS:**

```python
registry.add(RabbitMQProbe(url="amqps://user:secret@rabbitmq.internal/", name="rabbitmq-tls"))
```

**Checking multiple nodes** in a cluster — add one probe per node:

```python
for i, host in enumerate(["rmq-1.internal", "rmq-2.internal", "rmq-3.internal"], start=1):
    registry.add(RabbitMQProbe(url=f"amqp://guest:guest@{host}/", name=f"rabbitmq-node-{i}"))
```

---

## Watching Redis

**Install the extra:**

```bash
pip install fastapi-watch[redis]
```

`RedisProbe` sends a `PING` command and expects a `PONG` response.

```python
from fastapi import FastAPI
from fastapi_watch import HealthRegistry
from fastapi_watch.probes import RedisProbe

app = FastAPI()
registry = HealthRegistry(app)

registry.add(
    RedisProbe(
        url="redis://localhost:6379",
        name="redis",   # default
    )
)
```

**Password-protected Redis:**

```python
registry.add(RedisProbe(url="redis://:mypassword@localhost:6379"))
```

**Specific database index:**

```python
registry.add(RedisProbe(url="redis://localhost:6379/2", name="redis-db2"))
```

**TLS / Rediss:**

```python
registry.add(RedisProbe(url="rediss://redis.internal:6380", name="redis-tls"))
```

**Sentinel / cluster** — use `SqlAlchemyProbe` or a custom probe (see below) for advanced topologies where a plain `PING` to a single node is not sufficient.

**Watching Redis as both a cache and a queue** — add two probes pointing to different databases:

```python
registry.add(RedisProbe(url="redis://localhost:6379/0", name="cache"))
registry.add(RedisProbe(url="redis://localhost:6379/1", name="task-queue"))
```

---

## Writing a custom probe

Any class that extends `BaseProbe` and implements `check()` works as a probe. This is the right approach for:

- internal services (gRPC, internal REST APIs)
- third-party SDKs that don't fit a built-in probe
- business-logic health checks (e.g. "is the payment processor reachable?")
- composite checks (pass only when multiple conditions hold)

### Minimal example

```python
from fastapi_watch.probes import BaseProbe
from fastapi_watch.models import ProbeResult, ProbeStatus

class MyServiceProbe(BaseProbe):
    name = "my-service"

    async def check(self) -> ProbeResult:
        # replace with your real check
        ok = await call_my_service()
        return ProbeResult(
            name=self.name,
            status=ProbeStatus.HEALTHY if ok else ProbeStatus.UNHEALTHY,
        )

registry.add(MyServiceProbe())
```

### Capturing latency

Use `time.perf_counter()` to measure and report how long your check took:

```python
import time
from fastapi_watch.probes import BaseProbe
from fastapi_watch.models import ProbeResult, ProbeStatus

class PaymentGatewayProbe(BaseProbe):
    name = "payment-gateway"

    async def check(self) -> ProbeResult:
        start = time.perf_counter()
        try:
            await ping_payment_gateway()
            latency = (time.perf_counter() - start) * 1000
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.HEALTHY,
                latency_ms=round(latency, 2),
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

Accept constructor arguments the same way the built-in probes do:

```python
class S3BucketProbe(BaseProbe):
    """Checks that an S3 bucket is reachable and the credentials are valid."""

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
            return ProbeResult(name=self.name, status=ProbeStatus.HEALTHY, latency_ms=round(latency, 2))
        except Exception as exc:
            latency = (time.perf_counter() - start) * 1000
            return ProbeResult(name=self.name, status=ProbeStatus.UNHEALTHY, latency_ms=round(latency, 2), error=str(exc))

registry.add(S3BucketProbe(bucket="my-app-uploads", region="eu-west-1"))
```

### Composite probe

Report unhealthy only when more than one dependency is down at the same time:

```python
class CompositeProbe(BaseProbe):
    """Passes unless both Redis nodes are unreachable simultaneously."""

    name = "redis-composite"

    def __init__(self, primary_url: str, replica_url: str) -> None:
        self._primary = RedisProbe(url=primary_url, name="primary")
        self._replica = RedisProbe(url=replica_url, name="replica")

    async def check(self) -> ProbeResult:
        import asyncio
        primary, replica = await asyncio.gather(
            self._primary.check(), self._replica.check()
        )
        if primary.is_healthy or replica.is_healthy:
            return ProbeResult(name=self.name, status=ProbeStatus.HEALTHY)
        return ProbeResult(
            name=self.name,
            status=ProbeStatus.UNHEALTHY,
            error=f"both nodes down — primary: {primary.error}, replica: {replica.error}",
        )
```

---

## All built-in probes

### Databases

| Probe | Extra | Constructor |
|-------|-------|-------------|
| `PostgreSQLProbe` | `postgres` | `url`, `name`, `query`, `timeout` |
| `MySQLProbe` | `mysql` | `url` **or** `host`, `port`, `user`, `password`, `db`, `name`, `connect_timeout` |
| `SqlAlchemyProbe` | `sqlalchemy` | `engine`, `name`, `query` |

### Caches

| Probe | Extra | Constructor |
|-------|-------|-------------|
| `RedisProbe` | `redis` | `url`, `name` |
| `MemcachedProbe` | `memcached` | `host`, `port`, `name`, `pool_size` |

### Queues / messaging

| Probe | Extra | Constructor |
|-------|-------|-------------|
| `RabbitMQProbe` | `rabbitmq` | `url`, `name` |
| `KafkaProbe` | `kafka` | `bootstrap_servers`, `name`, `request_timeout_ms` |

### Document stores

| Probe | Extra | Constructor |
|-------|-------|-------------|
| `MongoProbe` | `mongo` | `url`, `name`, `server_selection_timeout_ms` |

### HTTP

| Probe | Extra | Constructor |
|-------|-------|-------------|
| `HttpProbe` | `http` | `url`, `timeout`, `name`, `expected_status` |

### Testing / placeholder

| Probe | Extra | Constructor |
|-------|-------|-------------|
| `MemoryProbe` | built-in | `name` |

---

## Configuration reference

### `HealthRegistry`

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `app` | `FastAPI` | required | The FastAPI application instance |
| `prefix` | `str` | `"/health"` | URL prefix for all three endpoints |
| `tags` | `list[str]` | `["health"]` | OpenAPI tags applied to the health routes |

### `HealthRegistry.add(probe)`

Returns `self` so calls can be chained. Probes are run in the order they were added (concurrently, not sequentially).

### `HealthRegistry.run_all()`

Async method — runs every registered probe and returns `list[ProbeResult]`. Useful for testing or for building custom aggregation logic outside of the mounted routes.

---

## Kubernetes integration

The three endpoints map directly to Kubernetes probe types.

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

Use `/health/ready` for the readiness probe so Kubernetes stops routing traffic to a pod the moment any dependency becomes unreachable. Use `/health/live` for the liveness probe so the process is only restarted when it is genuinely stuck, not just because an external service is temporarily down.

---

## License

MIT
