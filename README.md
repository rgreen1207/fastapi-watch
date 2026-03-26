# fastapi-watch

Structured health and readiness check system for [FastAPI](https://fastapi.tiangolo.com/).

Add `/health/live`, `/health/ready`, and `/health/status` endpoints to any FastAPI app with a single registry call. All probes run concurrently, so a slow dependency never blocks the others. Each probe returns rich service-specific details alongside the pass/fail result.

---

## Table of contents

- [Installation](#installation)
- [How it works](#how-it-works)
- [Endpoints](#endpoints)
- [Response format](#response-format)
- [Probe details](#probe-details)
- [Watching PostgreSQL](#watching-postgresql)
- [Watching MySQL / MariaDB](#watching-mysql--mariadb)
- [Watching Redis](#watching-redis)
- [Watching Memcached](#watching-memcached)
- [Watching RabbitMQ](#watching-rabbitmq)
- [Watching Kafka](#watching-kafka)
- [Watching MongoDB](#watching-mongodb)
- [Watching an HTTP endpoint](#watching-an-http-endpoint)
- [SQLAlchemy engine probe](#sqlalchemy-engine-probe)
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
pip install fastapi-watch[postgres]     # PostgreSQL        (asyncpg)
pip install fastapi-watch[mysql]        # MySQL / MariaDB   (aiomysql)
pip install fastapi-watch[sqlalchemy]   # Any SQLAlchemy 2.x async engine
pip install fastapi-watch[redis]        # Redis             (aioredis)
pip install fastapi-watch[memcached]    # Memcached         (aiomcache)
pip install fastapi-watch[rabbitmq]     # RabbitMQ          (aio-pika + aiohttp)
pip install fastapi-watch[kafka]        # Kafka             (aiokafka)
pip install fastapi-watch[mongo]        # MongoDB           (motor)
pip install fastapi-watch[http]         # HTTP endpoint     (aiohttp)

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

Add probes one at a time or chain them:

```python
registry.add(probe_a)
registry.add(probe_b)

# chained
registry.add(probe_a).add(probe_b).add(probe_c)
```

---

## Endpoints

| Endpoint | Purpose | Healthy | Degraded |
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

Every probe result has the same base shape:

| Field | Type | Description |
|-------|------|-------------|
| `name` | `string` | Probe identifier |
| `status` | `"healthy"` \| `"unhealthy"` | Pass/fail result |
| `latency_ms` | `number` | How long the check took in milliseconds |
| `error` | `string` \| `null` | Error message, only present on failure |
| `details` | `object` \| `null` | Service-specific metadata (see [Probe details](#probe-details)) |

**All healthy — `200`**

```json
{
  "status": "healthy",
  "probes": [
    {
      "name": "postgresql",
      "status": "healthy",
      "latency_ms": 1.8,
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
      "latency_ms": 0.6,
      "details": {
        "version": "7.2.4",
        "uptime_seconds": 86400,
        "used_memory_human": "2.50M",
        "connected_clients": 8,
        "total_keys": 312,
        "clusters": {
          "session": { "keys": 150, "ttl_seconds": 3600 },
          "cache":   { "keys": 162, "ttl_seconds": 900  }
        }
      }
    }
  ]
}
```

**One probe failing — `503` on `/ready`, `207` on `/status`**

```json
{
  "status": "unhealthy",
  "probes": [
    { "name": "postgresql", "status": "healthy",   "latency_ms": 1.8, "details": { ... } },
    { "name": "redis",      "status": "unhealthy", "latency_ms": 5002.1, "error": "Connection refused" }
  ]
}
```

---

## Probe details

Every built-in probe populates the `details` field with service-specific metadata. Details are always best-effort — if the metadata query fails after a successful connectivity check, `details` will contain whatever was collected up to that point. The probe status reflects connectivity only, not the completeness of `details`.

---

## Watching PostgreSQL

```bash
pip install fastapi-watch[postgres]
```

`PostgreSQLProbe` uses `asyncpg` directly — no SQLAlchemy required. It opens a connection, runs `SELECT 1` to verify the server is responsive, collects metadata, then closes the connection.

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

## Watching MySQL / MariaDB

```bash
pip install fastapi-watch[mysql]
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

## Watching Redis

```bash
pip install fastapi-watch[redis]
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
    "session": { "keys": 150, "ttl_seconds": 3600 },
    "cache":   { "keys": 162, "ttl_seconds": 900  }
  }
}
```

`clusters` groups keys by the segment before the first `:`. For example, a key named `session:abc123` falls into the `session` cluster. `ttl_seconds` is sampled from one key in the group; `null` means the key has no expiry.

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

## Watching Memcached

```bash
pip install fastapi-watch[memcached]
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

## Watching RabbitMQ

```bash
pip install fastapi-watch[rabbitmq]
```

`RabbitMQProbe` has two modes:

- **Connectivity only** (default) — opens and closes an AMQP connection. No channels or queues are touched.
- **Rich mode** — when `management_url` is set, the probe also calls the RabbitMQ Management HTTP API and returns per-queue stats, message rates, and cluster metadata.

### Connectivity only

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

### Rich mode — with Management API

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
    },
    "dead-letter": {
      "state": "running",
      "messages": 22,
      "consumers": 0,
      ...
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

## Watching Kafka

```bash
pip install fastapi-watch[kafka]
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

## Watching MongoDB

```bash
pip install fastapi-watch[mongo]
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

## Watching an HTTP endpoint

```bash
pip install fastapi-watch[http]
```

`HttpProbe` performs an HTTP GET and checks the response status code.

```python
from fastapi_watch.probes import HttpProbe

registry.add(HttpProbe(url="https://api.upstream.com/health"))
```

**Details returned:**

```json
{
  "status_code": 200,
  "content_type": "application/json",
  "response_bytes": 43
}
```

`details` is populated for both healthy and unhealthy responses so you can see what status code an upstream actually returned.

**Constructor arguments:**

| Argument | Default | Description |
|----------|---------|-------------|
| `url` | required | URL to GET |
| `timeout` | `5.0` | Request timeout in seconds |
| `name` | URL host | Probe label |
| `expected_status` | `200` | HTTP status code considered healthy |

```python
# Expect a 204 instead of 200
registry.add(HttpProbe(url="https://api.example.com/ping", expected_status=204))

# Shorter timeout, explicit name
registry.add(HttpProbe(url="https://api.payments.com/health", timeout=2.0, name="payments-api"))
```

---

## SQLAlchemy engine probe

```bash
pip install fastapi-watch[sqlalchemy]
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

## Writing a custom probe

Any class that extends `BaseProbe` and implements `check()` works as a probe. This is the right approach for internal services, third-party SDKs, business-logic checks, or composite conditions.

### Minimal example

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
                details={
                    "region": info.region,
                    "provider_version": info.version,
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

registry.add(S3BucketProbe(bucket="my-app-uploads", region="eu-west-1"))
```

### Composite probe

Report unhealthy only when both Redis nodes are down simultaneously:

```python
class CompositeRedisProbe(BaseProbe):
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

| Probe | Extra | Key constructor args | Details fields |
|-------|-------|---------------------|----------------|
| `PostgreSQLProbe` | `postgres` | `url`, `name`, `timeout` | `version`, `active_connections`, `max_connections`, `database_size` |
| `MySQLProbe` | `mysql` | `url` or `host`/`port`/`user`/`password`/`db`, `name`, `connect_timeout` | `version`, `connected_threads`, `uptime_seconds`, `max_used_connections` |
| `SqlAlchemyProbe` | `sqlalchemy` | `engine`, `name` | `dialect`, `driver`, `server_version` |

### Caches

| Probe | Extra | Key constructor args | Details fields |
|-------|-------|---------------------|----------------|
| `RedisProbe` | `redis` | `url`, `name` | `version`, `uptime_seconds`, `used_memory_human`, `connected_clients`, `role`, `total_keys`, `clusters` |
| `MemcachedProbe` | `memcached` | `host`, `port`, `name`, `pool_size` | — |

### Queues / messaging

| Probe | Extra | Key constructor args | Details fields |
|-------|-------|---------------------|----------------|
| `RabbitMQProbe` | `rabbitmq` | `url`, `name`, `management_url` | `connected`; + `server`, `totals`, `queues` when `management_url` is set |
| `KafkaProbe` | `kafka` | `bootstrap_servers`, `name`, `request_timeout_ms` | `broker_count`, `controller_id`, `topics`, `internal_topics` |

### Document stores

| Probe | Extra | Key constructor args | Details fields |
|-------|-------|---------------------|----------------|
| `MongoProbe` | `mongo` | `url`, `name`, `server_selection_timeout_ms` | `version`, `uptime_seconds`, `connections`, `memory_mb`, `storage_engine` |

### HTTP

| Probe | Extra | Key constructor args | Details fields |
|-------|-------|---------------------|----------------|
| `HttpProbe` | `http` | `url`, `timeout`, `name`, `expected_status` | `status_code`, `content_type`, `response_bytes` |

### Testing / placeholder

| Probe | Extra | Key constructor args | Details fields |
|-------|-------|---------------------|----------------|
| `MemoryProbe` | built-in | `name` | — |

---

## Configuration reference

### `HealthRegistry`

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `app` | `FastAPI` | required | The FastAPI application instance |
| `prefix` | `str` | `"/health"` | URL prefix for all three endpoints |
| `tags` | `list[str]` | `["health"]` | OpenAPI tags applied to the health routes |

### `HealthRegistry.add(probe)`

Returns `self` for chaining. Probes run concurrently on every request in the order they were added.

### `HealthRegistry.run_all()`

Async method — runs every registered probe and returns `list[ProbeResult]`. Useful for testing or building custom aggregation logic outside of the mounted routes.

```python
results = await registry.run_all()
for r in results:
    print(r.name, r.status, r.details)
```

### `ProbeResult`

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` | Probe identifier |
| `status` | `ProbeStatus` | `"healthy"` or `"unhealthy"` |
| `latency_ms` | `float` | Duration of the check in milliseconds |
| `error` | `str \| None` | Error message; only present on failure |
| `details` | `dict \| None` | Service-specific metadata; see each probe's section above |
| `is_healthy` | `bool` (property) | `True` when `status == "healthy"` |

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

Use `/health/ready` for the readiness probe — Kubernetes stops routing traffic to a pod the moment any dependency becomes unreachable. Use `/health/live` for liveness so the process is only restarted when it is genuinely stuck, not because an external service is temporarily down.

---

## License

MIT
