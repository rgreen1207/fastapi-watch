# Release Notes

## v1.5.9

**Automatic cache hit/miss tracking for `@lru_cache` and `@alru_cache`.**

### New Features
- **Automatic cache tracking** — `@probe.watch` now auto-detects `@lru_cache` and `@alru_cache` decorated functions. Cache hits, misses, `cache_maxsize`, and `cache_currsize` are reported in probe details with no additional setup required
- **Time-based cache window** — cache hit/miss counts default to the last 120 seconds (`cache_time_window_s=120.0`). Old events outside the window are excluded automatically
- **Count-based cache window** — set `cache_window_size=N` to use the last N lookups instead of a time window. When wrapping a `@lru_cache` function and `cache_window_size` is not set, the window is auto-sized to the cache's `maxsize`
- **`cache_reporting=False`** — opt out of automatic cache tracking per-probe
- **`cache_maxsize` / `cache_currsize`** — LRU cache capacity and current fill level included in probe details, useful for monitoring cache saturation

### Bug Fixes
- **Dashboard tooltip positioning** — clicking "Error Count" on a scrolled dashboard now correctly shows the error tooltip. Previously, `window.scrollY` was incorrectly added to the fixed-position tooltip's `top` coordinate, pushing it off-screen whenever the page was scrolled

### Breaking Changes
- `record_cache_hit()` and `record_cache_miss()` are still available for manual tracking (e.g. Redis or custom caches), but the `track_cache()` method has been removed — apply `@probe.watch` directly to the cached function instead

---

## v1.5.8

**Probe telemetry, dashboard improvements, performance optimizations, and custom dashboard support.**

### New Features
- **Circuit breaker toggle** — `FastAPIRouteProbe` accepts `circuit_breaker_enabled` to opt out of circuit breaker behavior
- **Cache hit/miss tracking** — `record_cache_hit()` / `record_cache_miss()` methods on `FastAPIRouteProbe` and passive probes; reported as `cache_hits` / `cache_misses` in probe details
- **Slow call counter** — `slow_call_threshold_ms` parameter on all probes; requests exceeding the threshold are counted as `slow_calls` in details
- **Error type breakdown** — `error_types` dict in probe details tracks exception class counts over the rolling window
- **Status distribution** — `status_distribution` dict in `FastAPIRouteProbe` details groups responses by family (`2xx`, `4xx`, `5xx`, etc.)
- **p50 / p99 RTT percentiles** — added alongside the existing p95 in all probe detail outputs
- **Timestamps** — `last_error_at` and `last_success_at` ISO timestamps in probe details; `last_success_at` only appears when ≥99% of the rolling window are failures
- **Dashboard timestamp display** — timestamps rendered as subtle meta-text beneath each probe card header; no extra table rows
- **Error count tooltip** — error count in the dashboard now shows a tooltip with the last error message
- **Custom dashboard** — `HealthRegistry(dashboard=...)` now accepts a file path: `.html`/`.htm` files are served as-is; `.py` files are loaded and their `render_dashboard(report, maintenance)` function is called
- **`**kwargs` subclass simplification** — all `PassiveProbe` subclasses (`HttpProbe`, `PostgreSQLProbe`, `MySQLProbe`, `RedisProbe`, `MongoProbe`, `SQLAlchemyProbe`, `SMTPProbe`) now forward `**kwargs` to the base class, so new base-class parameters are available to all subclasses automatically

### Performance
- All rolling-window counters (slow calls, cache hits, error types, status distribution) are now O(1) incremental updates — no per-poll O(n) scans
- `_RouteStats.snapshot()` in `RequestMetricsMiddleware` moves the O(n log n) RTT sort outside the lock
- `RequestMetricsProbe.check()` releases `_routes_lock` before calling `snapshot()` on each route, reducing lock contention

---

## v1.5.6

**Fix signal handler chaining for uvicorn reload.**

- Corrected SIGTERM handler to chain to the previous handler rather than calling it unconditionally, preventing double-shutdown on non-uvicorn deployments

---

## v1.5.5

**Fix uvicorn hot-reload blocking (poll tasks and SSE streams).**

- Fixed poll task blocking clean shutdown on uvicorn reload — background poll tasks now exit promptly on shutdown signal
- Fixed uvicorn reload blocking by registering the shutdown handler correctly on startup
- Fixed SSE streams holding connections open during reload — SSE connections now close cleanly when a shutdown signal is received

---

## v1.5.1

**Dashboard usability improvements.**

- Collapsible field reference glossary added to the health dashboard
- Removed tooltip popups (replaced by the glossary)

---

## v1.5.0

**Maintenance mode endpoints, dashboard tooltips, `min_error_status`, and docs overhaul.**

- `GET /health/maintenance` and `POST /health/maintenance` endpoints to toggle maintenance mode via the API
- Per-field tooltips on the health dashboard
- `min_error_status` parameter on `FastAPIRouteProbe` and `RequestMetricsMiddleware` — controls which HTTP status codes are counted as errors (default `500`; set to `400` to include 4xx)
- Comprehensive DOCS.md overhaul covering all probes, parameters, and detail fields

---

## v1.4.0

**Passive probes, multi-target alerters, ProbeGroup, and alert webhooks.**

- `PassiveProbe` base class — instruments your own functions via `@probe.watch` instead of making synthetic requests
- `HttpProbe`, `SMTPProbe`, `PostgreSQLProbe`, `MySQLProbe`, `RedisProbe`, `MongoProbe`, `SQLAlchemyProbe` refactored as passive observers
- `ProbeGroup` — aggregate multiple probes under a single name
- Multi-target alert webhook system — send alerts to multiple endpoints simultaneously
- `NoOpProbe` — placeholder probe that always reports healthy (replaces the removed `MemoryProbe`)
- `FastAPIRouteProbe` and `FastAPIWebSocketProbe` renamed from `RouteProbe` / `WebSocketProbe`
- Optional label argument on `@route_probe.watch("GET /users")` included in probe details as `description`
- `PassiveProbe` exported publicly for users building custom passive probes
- Pluggable alert storage backend with TTL, alert history, and `max_alerts` cap
- Removed `DiskProbe` (was introduced in v1.3.0 but replaced by the more flexible `ThresholdProbe` pattern)
- `MemoryProbe` renamed to `NoOpProbe`

---

## v1.3.0

**10 new probe types and significant feature expansion.**

- `DEGRADED` probe status — a middle state between `HEALTHY` and `UNHEALTHY`
- Maintenance mode — suppress unhealthy probes during planned downtime
- Prometheus metrics endpoint at `/health/metrics`
- `RequestMetricsMiddleware` + `RequestMetricsProbe` — app-wide HTTP request telemetry without per-route decoration
- Circuit breaker metrics in `FastAPIRouteProbe`
- `EventLoopProbe` — monitors asyncio event loop lag
- `DiskProbe` — monitors disk usage
- `TCPProbe` — checks TCP connectivity to a host/port
- `SMTPProbe` — checks SMTP server connectivity
- `ThresholdProbe` — triggers unhealthy when a user-supplied metric crosses a threshold
- Performance, correctness, and safety improvements across probes and registry internals

---

## v1.2.0

**Per-probe polling, circuit breaker, webhook alerts, auth, and new probe types.**

- Per-probe `poll_interval_ms` override — each probe can poll at its own frequency
- Circuit breaker — probes can open after consecutive failures and recover automatically
- Webhook alerts — POST to a configured URL on probe status changes
- HTTP basic auth on the health endpoints
- `StartupProbe` — reports unhealthy until explicitly marked ready
- `WebSocketProbe` — checks WebSocket endpoint connectivity
- `RouteProbe` (later renamed `FastAPIRouteProbe`) — instruments FastAPI route handlers
- `ProbeRouter` — mounts all health routes under a configurable prefix
- HTML health dashboard at `/health/dashboard`

---

## v1.1.0

**CeleryProbe, timezone support, and serialization fix.**

- `CeleryProbe` — pings a Celery worker pool; `detailed=True` for full broadcast stats
- Timezone-aware timestamps throughout
- `model_dump_json()` used for serialization (Pydantic v2 compatibility fix)

---

## v1.0.1

**Bug fixes.**

- Fixed Redis probe: removed erroneous `await` on `from_url`, dropped `asyncio` extra dependency
- Quoted `pip install` extras in README to fix zsh glob expansion issue
- Bumped minimum versions for `asyncpg` and `aio-pika`

---

## v1.0.0

**Background polling with configurable interval.**

- Probes now run in the background at a configurable `poll_interval_ms`
- Results are cached and served instantly from `/health` without blocking on probe execution

---

## v0.1.1

**Bug fixes and documentation.**

- Fixed `asyncpg.gather` bug in PostgreSQL probe
- Updated tests to cover the fix
- Documented `registry.add()` list support (pass multiple probes at once)

---

## v0.1.0

**Initial release.**

- `HealthRegistry` — central registry that mounts `/health`, `/health/live`, and `/health/ready` on a FastAPI app
- Probe results include rich `details` dicts for all built-in probes
- Built-in probes: `PostgreSQLProbe`, `MySQLProbe`, `RedisProbe`, `MongoProbe`, `KafkaProbe`, `RabbitMQProbe`, `MemcachedProbe`, `HttpProbe`, `CeleryProbe`
- GitHub Actions CI/CD pipeline
