# Release Notes

## v1.7.2

**Correctness and hardening release.**

### Upgrading: behavior changes in this release

Most fixes need no action. These change what you see or what is accepted:

- **`details["last_error"]` holds the exception type only** (`"IntegrityError"`, `"HTTPException"`), not its message. Pass `expose_error_messages=True` to a passive or route probe to restore the message. See Security Fixes.
- **`CeleryProbe(detailed=True)` drops task `args`/`kwargs`**; pass `include_task_args=True` to keep them. Task lists are capped at 50 per worker.
- **Readiness reflects this process only.** With a shared `storage=`, each replica reports its own registered probes as it checked them; results written by other replicas or apps are no longer served.
- **Blank credentials are rejected.** An empty or whitespace `key`, `username`, or `password` in `auth=` raises `ValueError` at startup.
- **`POST`/`DELETE /health/maintenance` refuse cross-site browser requests (`403`) and non-JSON bodies (`415`).** curl and other non-browser clients are unaffected.
- **Retention arguments reach custom backends.** `history_size`, `result_ttl_seconds`, `alert_ttl_seconds`, and `max_alerts` passed together with `storage=` are applied through the backend's `configure()`; a backend without one logs a warning naming the ignored arguments.
- **Request metrics no longer count fastapi-watch's own routes**, and streaming responses are timed until their last chunk, so their RTT rises.
- **Invalid probe and middleware settings raise `ValueError`** (`window_size <= 0`, `cache_window_size <= 0`, `ema_alpha` outside (0, 1], `max_error_rate` outside [0, 1], `max_routes < 1`) instead of silently recording nothing.
- **Dashboard stream tokens** are signed with a new key, so tokens minted before the upgrade stop validating (they lasted at most 5 minutes). When a page's token expires it fetches a fresh one from the new `GET /health/dashboard/stream-url` and reconnects without reloading.
- **`/health/history` and `/health/alerts` return `503 {"error": "storage unavailable"}`** when the storage backend fails, instead of `500`.
- **Results are listed in registration order**, not storage insertion order.
- **`claim_alert(key, value, ttl_seconds)`** is the storage hook for cross-replica alert dedupe (a compare-and-set of the last status announced per probe). A custom backend written against a 2-argument `claim_alert(key, ttl_seconds)` from a pre-release build must be updated.
- **`RedisProbeStorage` indexes probe names in `<prefix>:names`** (a sorted set) instead of `<prefix>:probes`. New instances delete the old set on their first write. During a rolling deploy, old and new replicas list only the names their own version has written.
- **Writes to shared storage happen in the background.** With any backend other than the default in-memory one, health checks no longer wait for `set_latest`/`append_history`/`append_alert`; while the store is slow only the newest unwritten result per probe is kept, and shutdown waits up to 2 s for pending writes.
- **Stored results are capped by size.** `RedisProbeStorage` now also caps *latest* results at `max_entry_bytes`, and `InMemoryProbeStorage(max_entry_bytes=8192)` caps history entries; oversized entries lose their largest `details` values and are marked `details["_truncated"] = True`.
- **`RedisProbeStorage(max_connections=4)`** limits the commands it has in flight at once; pass `None` for no limit. The `redis` extra now requires `redis>=4.5.4`.
- **Per-route keys for mounted apps use the Mount's parameter names**: `app.mount("/t/{tenant}", sub)` reports `/t/{tenant}/items/{item_id}` instead of one key per tenant.
- **`RabbitMQProbe` reports the 50 deepest queues**, not every queue; **`KafkaProbe`** now actually reports `broker_count`/`controller_id` and lists at most 100 topics plus `topic_count`.
- **`EventLoopProbe(sample_interval_ms=...)` below 1 raises `ValueError`**; `alert_dedupe_window_s` and `alert_min_interval_s` must be finite.
- **A custom `check()` that returns something other than a `ProbeResult`** is reported as that probe's `UNHEALTHY` result instead of failing every health endpoint with `500`.
- **A result is always filed under its probe's registered name.** A `check()` returning a `ProbeResult` with another `name` is now reported, stored and alerted under the probe's name.
- **The dashboard is served with `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` and `frame-ancestors 'none'`.**
- **The built-in dashboard sends a strict `Content-Security-Policy`**: only its own script runs (by a per-page nonce), and it connects only to its own origin. A proxy that injects scripts into HTML pages will find them blocked there. Custom dashboards keep `frame-ancestors 'none'` only.
- **`/health/metrics` writes non-finite values as `NaN`, `+Inf`, `-Inf`**, as the Prometheus text format spells them, and a non-numeric value as `NaN`.
- **Alerters reject settings that failed every alert, at construction (`ValueError`)**: credentials in a webhook URL (`https://user:pw@host/`), a header value or OpsGenie `api_key` containing a line break or NUL (often a trailing newline from a secrets file), and a `timeout` that is zero, negative, NaN or infinite (`None` still means unbounded).
- **`POST`/`DELETE /health/maintenance` read the body only after authentication** and refuse one over 4 KiB with `413`. An unauthenticated request now gets `401`/`403` whatever its body (a malformed one used to get `422`); non-JSON bodies from authenticated clients still get `415`.
- **`auth={"type": "apikey", "header": ...}`** must be a non-empty string; a blank or non-string header name raises `ValueError` at startup.
- **A negative or NaN `result_ttl_seconds` / `alert_ttl_seconds` raises `ValueError`** with the default in-memory storage too, as it already did with `RedisProbeStorage`; it used to switch time-based expiry off silently.
- **A client that hangs up before the response starts is recorded as `499`, not `500`**, by `RequestMetricsMiddleware` and route probes (an aborted upload, where Starlette raises `ClientDisconnect`). With the default `min_error_status=500` it no longer counts as an error.

### Bug Fixes

#### Third review round: dashboard and metrics

- **Rendering the dashboard no longer stalls the app.** The page renders in a worker thread, but its embedded data was encoded in one `json.dumps` call, which holds the GIL throughout: with 200 probes and 2,000 history points each, the app's event loop stopped for 447 ms per page load (about 30 ms at the default `history_size`). Data is now encoded a probe at a time; the same page stalls the loop for at most 30 ms.
- **A probe named `constructor`, `toString` or `__proto__` no longer breaks the dashboard.** With no history stored for it yet, the page script found `Object.prototype` members instead and threw on its first render: no charts and no live updates for any probe.
- **One probe can no longer fail the whole Prometheus scrape.** A custom probe's own `circuit_breaker` detail that is not a dict (with the breaker off, the registry leaves it alone) turned `/health/metrics` into a `500`; a boolean there was written as `True`, which Prometheus rejects along with every other sample in the scrape.
- **Free-form route breakdowns no longer `500` the dashboard.** Mixed-type route keys or a non-numeric `request_count` in a custom probe's `routes` detail failed the page; the latter also failed every live update.

#### Third review round: registry

- **Shutdown never ran for apps with a `lifespan=`.** Starlette skips `on_shutdown` handlers once an app has its own lifespan, which is how every documented example is set up, so queued alerts were dropped, background storage writes abandoned, and probe `aclose()` never called. The registry now wraps the app's lifespan and shuts down before the app's own teardown. Each lifespan also starts it afresh: after one shutdown, streams of a second run in the same process (`with TestClient(app)` per test) returned nothing and alerts stopped.
- **Unauthenticated clients could make the app parse arbitrary JSON.** FastAPI reads and parses a declared request body before any dependency runs, so `POST /health/maintenance` parsed bodies of any size on the event loop before checking credentials (40 MB took 0.6 s). The body is now read after authentication and capped at 4 KiB.
- **Alerts keep flowing when the app runs again on a new event loop.** The alert queue stayed bound to the loop of the first run, so in a second `with TestClient(app)` the alert worker died after each alert ("bound to a different event loop") and coalesced alerts (`alert_min_interval_s`), such as a recovery, were never sent. The queue now moves to the running loop, keeping anything queued.
- **Concurrent `/health/startup` requests share one check.** Each request ran every startup probe, so a burst of requests (from anyone, with `auth=None`) became that many concurrent checks of each startup dependency.

#### Third review round: storage

- **Storage no longer grows with a renamed result.** A probe whose `check()` returned a result under another name (an inner probe's result passed through, or a name that changed per check) was stored under that name, which the registry never forgets: each new name left a latest-result key and a history key behind until `result_ttl_seconds`, or forever with `result_ttl_seconds=0` (200 checks wrote 401 Redis keys). Results are now filed under the probe's registered name.
- **One unreadable entry no longer fails history.** A history entry, alert or latest result that `RedisProbeStorage` could not parse (written by a newer version sharing the prefix during a rolling deploy, e.g. with a new status value, or corrupted) made every read raise, so `/health/history` and `/health/alerts` answered `503` until it aged out. Such entries are skipped.

#### Third review round: probe timeouts, storage TTLs and documentation

- **A probe raising its own `TimeoutError` no longer fails every health endpoint.** Since Python 3.11 `asyncio.TimeoutError` is the builtin `TimeoutError` that socket and client libraries raise on a connect or read timeout.  With `probe_timeout=None` the registry tried to format the missing deadline and `/health/ready`, `/status`, `/metrics` and the dashboard answered `500`; with a deadline the error claimed the probe had run for the full timeout.  Such a probe is now reported `UNHEALTHY` with `"probe check failed: TimeoutError"`; only the registry's own deadline reports `"probe timed out after N s"`.
- **The dashboard no longer waits behind hung probe threads.** It rendered in the same 4-thread pool as blocking probe calls (TCP DNS lookups, Celery inspect), so a DNS outage holding four threads stalled every page load for as long as the lookups hung.  It now renders on a thread of its own.
- **In-memory TTLs are validated.** `InMemoryProbeStorage` (and so `HealthRegistry` without `storage=`) accepted a negative or NaN TTL, which disabled time-based expiry without a word.
- **Documentation examples run.** The README quickstart called `PostgreSQLProbe(url=...)` and `RedisProbe(url=...)`, which raise `TypeError` (they are passive probes, fed by `@probe.watch`), and attached the registry to an `app` it then replaced with `FastAPI(lifespan=...)`, so the served app had no health routes.  `ThresholdProbe`'s example passed `probe=` instead of `inner_probe`.  A test now checks every documented constructor call against its signature.

#### Third review round: hot path

- **Watched `yield` dependencies work on older FastAPI.** `probe.watch()` wrapped a generator function in a plain function, so FastAPI before it followed `__wrapped__` (0.100, for one) injected the generator object itself into the route instead of the session it yields. Newer releases ran it, but the probe timed only the creation of the generator (~0 ms) and missed any error raised while it ran, including mid-stream failures of streaming (generator) endpoints. Watched generators now stay generators, forward every send, throw and close, and are recorded when they finish: RTT is the time spent running the generator (a dependency's setup and teardown, not the endpoint's work in between), and an endpoint's exception passing back out of a dependency is not counted against it.
- **Aborted uploads no longer make the app unhealthy.** A client that disconnected while a handler read its body was counted as a server error (500) by `RequestMetricsMiddleware` and route probes, so clients on flaky networks could push `RequestMetricsProbe`, and with it readiness, to `UNHEALTHY`. Such requests are now recorded as `499`, and a response the server could not finish delivering keeps the status it started with.
- **`RequestMetricsProbe` no longer stalls the event loop.** Building the per-route breakdown sorted every route's latency window in one go: about 8 ms of blocked event loop per check at the 500-route cap. The check now yields between batches of 50 routes (under 1 ms each).

#### Second review round

- **Alert dedupe could swallow a repeat outage.** With `alert_dedupe_window_s`, claims were keyed on `<probe>:<status>`, so a probe that failed, recovered and failed again inside the window never alerted the second failure; the last thing your pager saw was "healthy". Claims are now a compare-and-set of the last announced status per probe.
- **Queued alerts survive shutdown.** The SIGTERM hook cancelled the alert worker, dropping queued alerts, and a check finishing during shutdown could start a worker nothing awaited. Shutdown now drains the queue for up to 5 s.
- **SIGTERM stays fatal without a server handler.** With SIGTERM at its default disposition, the early-shutdown hook replaced "terminate" with "set a flag", so the process ignored SIGTERM until SIGKILL.
- **Watched handlers with string annotations work on older FastAPI.** With `from __future__ import annotations`, `probe.watch()` made FastAPI before it followed `__wrapped__` treat `request: Request` as a required query parameter: every call returned `422` (a WebSocket closed with `1008`).
- **Dashboard fixes:** a NaN or infinite value from a custom probe no longer drops all embedded data or freezes live updates; a non-numeric `error_rate` or mixed-type detail keys no longer return `500`; a probe name containing `"` no longer stops live updates; points are no longer cut off when the browser clock runs behind the server.
- **Probe fixes:** async callable objects (`alru_cache` wrappers, partials) are timed and their errors counted; a returned model with a `status_code` field no longer counts as an HTTP error; WebSocket handlers whose socket parameter has another name and a string annotation are instrumented; `ThresholdProbe` keeps the inner probe's circuit-breaker settings; `RabbitMQProbe` decodes percent-encoded management credentials; `ProbeGroup.include()` keeps nested group tags; circuit-breaker state of removed probes is cleaned up.
- **Webhook deadlines cover connecting.** A host resolving to several unreachable addresses gave each the full timeout (a 1 s budget took 3 s with three); they now share it.

#### `error_rate` now reflects recent traffic, not the process lifetime

`PassiveProbe` (and every probe built on it: `HttpProbe`, `RedisProbe`, `SqlAlchemyProbe`, `PostgreSQLProbe`, `MySQLProbe`, `MongoProbe`, `SMTPProbe`), `FastAPIRouteProbe`, `FastAPIWebSocketProbe`, and `RequestMetricsMiddleware` / `RequestMetricsProbe` computed `error_rate` as `error_count / total_count` over the whole life of the process. A service with a long healthy history could fail every request for hours without crossing `max_error_rate` (1,000 successes followed by 100 straight failures still reported 9% and `healthy`), and an early burst of errors kept a probe unhealthy long after it recovered.

**Fix:** `error_rate` — and therefore the `max_error_rate` health check — is now computed over the last `window_size` outcomes (default 100; 200 for `RequestMetricsMiddleware`). For `FastAPIWebSocketProbe` the window covers closed connections only, since open ones have not had a chance to error. `error_count`, `request_count`, `call_count`, and `total_connections` still report lifetime totals.

**Behavior change:** probes now go unhealthy promptly during an outage and recover once the window clears. If you tuned `max_error_rate` around the old lifetime figure, revisit it.

#### `RequestMetricsMiddleware` now counts unhandled exceptions

When a route raised an exception that no exception handler caught, `call_next` re-raised it and the request was never recorded. Starlette turns those exceptions into a 500 in `ServerErrorMiddleware`, which sits outside every user middleware, so the most serious failures — real crashes — were invisible to `request_count`, `error_count`, `error_rate`, and the per-route breakdown. Only 500s produced by an exception handler were counted.

**Fix:** the middleware records an unhandled exception as a 500 (in the aggregate and under the route template) and re-raises it unchanged, so the response the client receives is the same as before.

#### A naive `until` no longer breaks readiness

`set_maintenance(until=...)` and `POST /health/maintenance {"until": ...}` accepted a datetime without a UTC offset, which `/health/ready` then compared against an aware "now" — raising `TypeError` on every readiness check (500) and on every poll until maintenance was cleared.

**Fix:** a naive `until` is interpreted in the registry's `timezone`. Over HTTP, an `until` that has already passed is rejected with `422` before any state changes, instead of reporting `active: true` and silently expiring (or ending a maintenance window that was already running).

#### A hung probe no longer hangs every health endpoint

`BaseProbe.timeout` defaults to `None`, and several built-in probes (`RabbitMQProbe`, `MemcachedProbe`, and every custom probe that didn't set one) had no bound of their own. Every health endpoint waits on the probes it runs, so a single blackholed dependency left `/health/ready`, `/health/status`, `/health/metrics`, and the dashboard pending forever — and Kubernetes readiness checks with them.

**Fix:** new `HealthRegistry(probe_timeout=30.0)` bounds every probe that does not set its own `timeout`; a per-probe `timeout` still takes precedence. `probe_timeout=None` restores unbounded checks. Timeouts now report `"probe timed out after N s"` and log a warning, instead of the generic `"probe check failed"` with a full traceback.

#### A raising `on_state_change` callback no longer breaks health checks; the poll loop no longer dies silently

Callbacks registered with `on_state_change()` ran unguarded inside the probe run, so an exception in one turned the `/health/ready` (or `/status`, `/metrics`) request that observed the state change into a `500`, skipped the remaining callbacks, and skipped alerting. The background poll loop had no error handling at all: one failing iteration (a callback, a storage backend error) ended background polling for the life of the process, and it was never restarted because `_on_connect` only checked whether a task object existed.

**Fix:** each callback is isolated — failures are logged with a traceback and the remaining callbacks and alerters still run. A failing poll iteration is logged and the loop continues; a poll task that has exited is restarted when the next streaming client connects. Errors are logged through `logger=` when given, otherwise the `fastapi_watch` logger.

#### `CeleryProbe` no longer reports healthy workers as unhealthy

`CeleryProbe(timeout=...)` was used both as the per-broadcast reply wait and — through `BaseProbe.timeout` — as the registry's deadline for the whole check. A Celery broadcast without a reply limit collects replies for the full timeout, so the deadline expired first and a healthy, responsive worker came back `UNHEALTHY` (always, with `detailed=True`, which issues seven broadcasts).

**Fix:** the constructor's `timeout` keeps its meaning (per-broadcast wait, now also exposed as `reply_timeout`), and the probe's overall deadline covers every broadcast it issues plus a 2 s margin: `timeout + 2` for ping-only, `7 × timeout + 2` with `detailed=True`.

#### Route probes with the same handler name no longer collide

Auto-discovered probes are named after the handler function, and two routers each defining `list_items` produced two probes named `list_items`. Results, history, alerts, and circuit-breaker state are keyed by name, so only one survived — the other route's traffic was silently invisible. A probe name failing validation was also only detected after the route had already been patched.

**Fix:** when an auto-generated name is already taken, the later route's probe is qualified with its methods and path (`list_items`, `list_items:GET_orders`); unique names are unchanged. Names are validated before a route is patched. `registry.add()` now warns when a *different* probe reuses an existing name (a warning, not an error, so existing apps keep starting). The name check also no longer accepts a trailing newline.

#### Path filters and descriptions use the full path on FastAPI 0.137+

FastAPI 0.137 stores included routers as `_IncludedRouter` wrappers whose routes keep their un-prefixed path. `discover_routes()` / `watch_router()` matched `include_paths` / `exclude_paths` and built probe descriptions from that short path, so `exclude_paths=["/internal/*"]` did not exclude a router included at `/internal`.

**Fix:** paths are matched and described using the full path a client requests on every FastAPI version. Health routes are recognised as the registry's own handlers rather than by path prefix, so an application route such as `/healthcheck` is no longer skipped by the default `/health` prefix, and `prefix=""` no longer excludes every route. `discover_routes()` and `watch_router()` now share one implementation.

#### `RequestMetricsProbe` can find the middleware itself

`app.add_middleware(RequestMetricsMiddleware)` makes Starlette build the instance lazily, so there was no supported way to hand that live instance to `RequestMetricsProbe`; the documented example built a second instance that never saw traffic, leaving the probe at "no requests observed yet" forever.

**Fix:** `RequestMetricsProbe(app)` accepts the app and resolves the live middleware from `app.middleware_stack` on its first check. If the stack is built without the middleware, the probe reports UNHEALTHY with a configuration error rather than silently showing no traffic. Passing a middleware instance still works. `RequestMetricsProbe` also defaults to `circuit_breaker_enabled=False` (see the passive-probe circuit breaker change below).

#### Dashboard live updates

Three problems with the built-in dashboard's live (SSE) updates:

- The per-route breakdown from `RequestMetricsProbe` rendered as "—" on the page and in every live update, although it was computed and streamed each time. It is now shown as a compact table (route, requests, errors, error rate, p95), escaped like every other value.
- Live updates refreshed each probe card but never the summary line ("N / M probes healthy"), and a probe that first appeared after page load never got a card. The summary now updates with every event, and when the set of probes changes the page reloads once to pick it up (guarded against reload loops).
- The stream URL was absolute (`/health/status/stream`), so a dashboard served under a mount or a proxy `root_path` connected to the wrong URL and never updated. It now includes the request's `root_path`.

#### Live streams survive proxies and no longer cause reconnect storms

The SSE endpoints (`/health/status/stream`, `/health/ready/stream`, and the dashboard that uses them) had three problems:

- **No keepalives.** Nothing was sent between events, and with the default 60-second interval that matched the common 60-second idle timeout of load balancers and proxies, which cut the connection. The stream now sends a `: keepalive` comment every 15 seconds.
- **Reconnect storm in single-fetch mode.** With no polling, the stream sent one event and closed; browsers reconnected about every 3 seconds per open tab, and each reconnect ran every probe. The single event now carries `retry: 30000`, so browsers wait 30 seconds.
- **Cadence ignored per-probe intervals.** Events went out only at the global `poll_interval_ms` (and the stream closed when it was `None`, even if probes had their own intervals). Events now follow the shortest effective probe interval.

The stream also no longer swallows `asyncio.CancelledError`; cancellation propagates after cleanup.

#### Dashboard live updates work with header-based `auth=`

With `auth={"type": "apikey", ...}` (or a callable checking a header), the dashboard page loaded but its live updates never did: a browser `EventSource` cannot send headers, so `/health/status/stream` answered `403` to every connection.

**Fix:** the authenticated dashboard page embeds a short-lived signed token in its stream URL (valid 5 minutes, checked when the stream connects). The two stream endpoints accept that token or normal credentials; nothing else accepts it, and the credentials never appear in the page. For `basic`/`apikey` auth the signing key is derived from the configured credentials, so tokens are honored across worker processes and replicas; a callable `auth` uses a per-process key. When a reconnect is refused after the token expires, the page reloads itself to obtain a fresh one.

#### Documented `auth=` examples now work

README.md and DOCS.md showed `auth={"username": ..., "password": ...}` (missing `"type": "basic"`) and `auth={"token": ...}` (not a supported form); both raised `ValueError` at startup. The examples are corrected, and a test now constructs a registry from every `auth=` example in the docs.

#### Documentation corrections

- The `grace_period_ms` docstring said failures are "ignored" during the grace window; it actually holds `/health/ready` at `503 {"status": "starting"}` (as `DOCS.md` already said). The docs now also state that the window starts when the registry is created.
- The `ProbeGroup` docstring referred to a `ProbeRouter` class and imported `PostgreSQLProbe` / `RedisProbe` from the package root (they live in `fastapi_watch.probes`), constructing them with a `url=` they do not accept. The example is corrected.
- The `RedisProbeStorage` sketch in the `ProbeStorage` docstring is gone, replaced by the real `RedisProbeStorage` (see New Features). The docstring now holds a short outline for writing your own backend, and a test checks it against the protocol.
- New tests check that every `from fastapi_watch… import …` in README.md, DOCS.md, and the docstrings resolves, alongside the existing check that every documented `auth=` example constructs.
- **Retention precedence:** `HealthRegistry`'s `history_size`, `result_ttl_seconds`, `alert_ttl_seconds`, and `max_alerts` apply to a custom backend only when at least one is passed explicitly; otherwise the backend keeps its own constructor values.
- **Known limit:** `KafkaProbe`, `RabbitMQProbe`, and `MemcachedProbe` connect by hostname through their client libraries, so their DNS lookups still use the event loop's default executor; pre-resolving would break TLS hostname checks. See "Blocking checks and the thread pool" in DOCS.md.
- **Custom-probe examples** report the exception type rather than `str(exc)`, and the "Testing a custom probe" example now goes through `registry.run_all()` (it called `check()` on a probe that raises, so it could never pass).
- The missing README "Custom storage backend" section that DOCS.md linked to now exists, and the strict-CSP claim for the dashboard is corrected (its inline script and styles need `'unsafe-inline'`).

#### Custom dashboard renderers with the documented signature

`dashboard=` documented a callable `(report) -> str`, but the registry called it with `(report, maintenance)`, so a renderer written to the documentation raised `TypeError` and `/health/dashboard` returned 500.

**Fix:** the renderer's signature is inspected once at startup: one-argument renderers are called with the report, and renderers taking a second positional argument (as 1.7.1 required) still receive the maintenance flag.

#### A probe failing at startup now alerts

State changes were only recognised from a probe's second result onward, so a dependency that was already down when the service started produced no callback and no alert — and stayed silent until it recovered and failed again. A probe's state before its first result now counts as `HEALTHY`: a first `UNHEALTHY` or `DEGRADED` result fires callbacks and alerters (`healthy → unhealthy`); a first `HEALTHY` result fires nothing, as before.

#### Probes with their own interval poll from startup, not from the first dashboard visit

A probe with its own `poll_interval_ms` is meant to poll in the background, and polling did continue after the last stream client left — but it only ever *started* when the first SSE client connected. Whether background polling happened at all depended on someone having opened the dashboard. It now starts at `set_started()`, or at the first health request when `set_started()` ran outside an event loop.

#### Passive probes no longer get stuck behind the circuit breaker

The circuit breaker was on by default for passive probes (`PassiveProbe` and its subclasses, `FastAPIRouteProbe`, `FastAPIWebSocketProbe`, `RequestMetricsProbe`). They only read in-memory stats, so tripping protected no dependency — it froze the probe on its failing result for the full cooldown (10 minutes by default) after the underlying traffic had already recovered.

**Behavior change:** passive probes now default to `circuit_breaker_enabled=False`; pass `True` to opt back in. Active probes (anything calling a real dependency in `check()`) keep the breaker on.

#### Cache hit/miss counts are no longer misattributed under concurrency

`watch()` decided whether a call on an `@lru_cache` / `@alru_cache` function was a hit by comparing the shared `cache_info().hits` before and after the call, so a concurrent call's hit was credited to every call that overlapped it. A call is now attributed only when its before/after window shows exactly one cache event; overlapping calls are skipped rather than miscounted, so `cache_hits` / `cache_misses` are a fair sample under load.

#### A non-integer `status_code` no longer drops the request record

`FastAPIRouteProbe` read `status_code` from any returned object; a route returning a model with a non-integer `status_code` field (a string, `None`) raised inside the recorder, and the request silently went uncounted. Non-integer values now fall back to 200 (or 500 for exceptions).

#### `RabbitMQProbe` no longer uses the deprecated `aiohttp.BasicAuth`

Current aiohttp deprecates `aiohttp.BasicAuth` (removed in aiohttp 4.0). The management-API request now sends a standard `Authorization: Basic` header built directly, which works across every supported aiohttp version.

#### Alerters: concurrent, isolated, and no longer leaking sockets

- Alerters were called one after another, so a slow integration (each built-in waits up to its 5-second `timeout`) delayed every other alerter and every later alert. They now run concurrently per state change, each bounded by a 30-second ceiling that also catches custom alerters that never return.
- Alerter failures were logged without a traceback (and not at all without `logger=`), and the alert worker silently swallowed its own errors and skipped `task_done()`. Failures are now logged with their traceback through `logger=` or the `fastapi_watch` logger, and the worker always marks the alert done.
- The built-in alerters never closed the HTTP response from `urlopen`, leaking a socket per alert until garbage collection; success and error responses are now closed.
- `OpsGenieAlerter`'s host-allowlist helper was defined but unused, with the same check duplicated inline twice; the requests now go through it.

#### `EventLoopProbe` catches stalls between checks

`EventLoopProbe` timed a single `asyncio.sleep(0)` at check time. That measurement only runs once the loop is free again, so a stall between checks — the thing the probe exists to catch — was invisible, and what it did measure was mostly the sibling probes' synchronous work in the same batch.

**Fix:** the first check starts a lightweight background sampler that sleeps for `sample_interval_ms` (new, default 100) and records how late it wakes. `lag_ms` is now the worst lag since the previous check; the new `current_lag_ms` keeps the immediate measurement. `warn_ms` / `fail_ms` semantics are unchanged.

#### Probe failures say what failed

`TCPProbe`, `MemcachedProbe`, `RabbitMQProbe`, `KafkaProbe`, `CeleryProbe`, and the registry's handler for exceptions escaping `check()` all reported the bare string `"probe check failed"` and discarded the exception, so a refused connection, a DNS failure, and a timeout looked identical on the dashboard. They now report the exception type — `"probe check failed: ConnectionRefusedError"` — still never the message text, which can carry credentials or internal hostnames; the full exception is logged at debug level on the `fastapi_watch` logger.

`ThresholdProbe` silently treated a raising `warn_if` / `fail_if` (a typo'd key, say) as "not met", disabling the threshold unnoticed. The error is now logged and reported in the result as `details["threshold_errors"]`. `ThresholdProbe` also now carries the inner probe's `description`, `tags`, and circuit-breaker setting instead of dropping them.

#### WebSocket handlers receive the real `WebSocket`

`FastAPIWebSocketProbe.watch()` replaced the handler's `WebSocket` argument with a proxy object, so `isinstance(websocket, WebSocket)` was false inside watched handlers (breaking code and libraries that type-check it), the proxy's connection state was separate from the socket any dependency held, and messages read through `iter_text()` / `iter_bytes()` / `iter_json()` were not counted.

**Fix:** the handler now receives the very socket FastAPI created, instrumented in place to count application messages at the `receive()` / `send()` level (handshake messages excluded) — so every helper, including the `iter_*` generators, is counted exactly once.

#### Passive probes accept `poll_interval_ms` and `timeout`

`HttpProbe`, `RedisProbe`, and the other `PassiveProbe` subclasses documented a `poll_interval_ms` argument but raised `TypeError` when given one. `PassiveProbe` now accepts `poll_interval_ms` and `timeout`, like the other probes.

#### Startup probes are guarded like every other probe

`/health/startup` ran `startup_probes` directly, with no timeout and no exception handling: a hung startup probe hung the Kubernetes `startupProbe` request, and one that raised turned it into a `500`. Startup probes now run through the same guarded path as regular probes (bounded by `probe_timeout` or their own `timeout`), so either case answers `503` with the probe's error.

#### Shutdown signal hook no longer cancels tasks from inside the signal handler

The early-shutdown hook cancelled SSE, poll, and alert tasks directly inside a `signal.signal()` handler, which can run in the middle of any event-loop callback. It now only sets the shutdown flag and schedules the cancellation on the loop with `call_soon_threadsafe()`; without a running loop the flag alone stops every stream within one 500 ms tick. A failure to install the hook (non-main thread) is logged at debug level instead of being silently ignored.

#### Health checks survive storage outages

Storage calls were awaited unguarded, so a storage backend that failed or hung failed or hung every health endpoint with it: with Redis down, `/health/ready` returned `500` and Kubernetes would take every pod out of rotation because the *monitoring store* was down, not the app.

**Fix:** new `HealthRegistry(storage_timeout=2.0)` bounds every storage call (`None` disables the bound). Failures and timeouts are logged at most once a minute per operation and never fail a request. `/health/ready`, `/status`, `/metrics`, and the streams answer from this process's own last results; `/history` and `/alerts` return `503 {"error": "storage unavailable"}`; the dashboard renders without history. Writes run concurrently, so a hung backend costs one timeout per check rather than one per write.

#### Readiness reflects this process, not whatever is in shared storage

With a shared `storage=`, `_get_results` skipped any probe that already had a stored result — possibly written by another replica — and returned every entry in storage, including other apps' probes. A replica that could not reach its database could report healthy (or the reverse), and every request read storage twice.

**Fix:** health endpoints report only this registry's probes, from results this process produced, and a probe that has not run in this process runs first. Requests no longer read storage at all; storage holds history, alerts, and the latest result for the circuit breaker's fallback.

#### Retention settings are no longer ignored with a custom `storage=`

`history_size`, `result_ttl_seconds`, `alert_ttl_seconds`, and `max_alerts` were only used to build the default in-memory backend, so `HealthRegistry(storage=X, history_size=5)` silently kept the backend's own limits.

**Fix:** when any of them is passed with `storage=`, the backend's optional `configure()` receives all four effective values; a backend without `configure()` logs a warning naming the ignored arguments. Omitted arguments leave the backend's own constructor values in place.

#### Failures during maintenance alert once maintenance ends

Probe states kept updating while alerters were suppressed, so a dependency that failed during maintenance and was still down afterwards never alerted.

**Fix:** the registry tracks the last state actually sent to alerters. After maintenance, the first check sends the difference (`healthy -> unhealthy` for an outage that is still ongoing, `unhealthy -> healthy` to resolve one that recovered inside the window); a failure that started and ended inside the window sends nothing. An alert dropped because the queue was full is retried on the next check. `on_state_change` callbacks and `/health/alerts` still record every real transition.

#### Circuit-breaker results keep their original timestamp

While a probe's circuit was open the registry served its cached result stamped with a fresh `checked_at`, so graphs and history showed stale data as current. The cached result now keeps its original `checked_at`.

#### Timed-out blocking checks no longer pile up threads in the app's executor

`CeleryProbe` inspections and `TCPProbe` DNS lookups ran in the event loop's default executor, which the app shares. A timeout cancels the coroutine but not the thread, so a hung broker or DNS server added a stuck thread on every poll (1 → 11 threads after 10 timeouts), starving the app's own `run_in_executor` work.

**Fix:** blocking work — Celery inspection, TCP DNS lookups, and alerter webhook posts — runs in fastapi-watch's own pool of at most 4 daemon threads. A probe whose previous blocking call is still running reports UNHEALTHY with `"previous check still running"` instead of starting another, so each hung dependency holds at most one thread. `TCPProbe` now resolves once and connects to the resolved addresses. Alerter `timeout` now bounds the whole webhook request rather than each socket read, so a slow endpoint fails with `TimeoutError` instead of holding a thread.

#### Invalid probe settings fail at startup

`window_size=0` or `cache_window_size=0` made every recording raise `IndexError`, which was swallowed, so the probe silently recorded nothing forever. Passive, route, and WebSocket probes and `RequestMetricsMiddleware` now raise `ValueError` for out-of-range `window_size`, `cache_window_size`, `ema_alpha`, `max_error_rate`, and `max_routes`. `PassiveProbe.check()` also reads its state under its lock, which is safe on free-threaded Python.

#### `EventLoopProbe` stops its sampler at shutdown

New `BaseProbe.aclose()` (a no-op by default) is awaited for every registered and startup probe at shutdown. `EventLoopProbe` uses it to cancel its background sampler; `ThresholdProbe` forwards it to the probe it wraps.

#### Data for removed probes is cleaned up — without touching anyone else's

Storage kept latest results and history for probes the registry no longer had. Backends that implement the new optional `forget(names)` are told, at most every 10 minutes, about probes this process wrote and has since stopped registering (for example after `discover_routes(refresh=True)` renames one). Names this process never wrote are never touched, so two apps on the same storage prefix, or old and new pods during a rolling deploy, keep each other's data. A failed `forget()` is retried on the next pass.

`set_poll_interval(0)` likewise clears only this process's cached results. It no longer calls `storage.clear_latest()`, which deleted the latest results of every app and replica sharing the backend. `clear_latest()` stays in the `ProbeStorage` protocol, so existing backends still satisfy it.

### New Features

#### Redesigned dashboard: a timeboard with graphs

`/health/dashboard` is rebuilt as a Datadog/Kibana-style timeboard. Every graph shares one time axis, so a status change, a latency spike, and an error burst line up vertically:

- **Status over time** — one lane per probe, colored by status, with a tick at each state change; gaps in the data stay blank.
- **Per-probe graphs** — latency (p50/p95/p99 where reported), event-loop lag with its warn/fail thresholds, error rate, and throughput, drawn only when the probe reports them.
- **Per-route graphs** — p95 latency for the eight busiest routes from `RequestMetricsProbe(per_route=True)`.
- **Time ranges** — 15 min / 1 hour / 2 hours / all retained history, from the registry's stored history embedded in the page and extended live over SSE.
- **Shared crosshair** — hover any graph to see the same moment on all of them, with values in a tooltip.
- **Readable for everyone** — percentile lines are told apart by color and by dash pattern, labeled at their ends, and listed in a legend; status colors are reserved for status. Light and dark themes follow the OS.
- **A plain-language header** — "orders-db unhealthy for 8 min" instead of a generic status banner.

The page remains a single self-contained HTML document: inline SVG and vanilla JavaScript shipped inside the package (`fastapi_watch/static/`), with no CDN or external assets. Custom renderers (`dashboard=callable` or an `.html` path) are unaffected. `render_dashboard()` gains an optional `history=` argument.

To support the graphs, every `ProbeResult` now carries `checked_at` — the time the registry recorded it — in all responses, history, and stored results. Custom storage backends that serialize the model pick it up automatically.

#### `RedisProbeStorage`: a bounded, first-party Redis backend

`ProbeStorage`'s documentation carried a Redis "sketch" that users copied. It never applied `max_alerts` (the alert list grew for as long as alerts kept arriving — 5,000 of 5,000 kept in testing), never aged individual entries (the TTL covered whole keys and was refreshed on every write), errored or deleted data with a TTL of `0` (documented as "no expiry"), could leave keys without a TTL after a partial write, stored full `details` per history entry (3.4 MB for one 150-route probe), and used `KEYS`, which blocks a Redis the app usually shares.

New `fastapi_watch.RedisProbeStorage(redis_client, prefix=..., max_results=..., result_ttl_seconds=..., alert_ttl_seconds=..., max_alerts=..., max_entry_bytes=8192)` (needs the `redis` extra) replaces it:

- History and alerts are sorted sets scored by time, trimmed by count **and** age on every write and filtered on read.
- Each write is one `MULTI/EXEC` transaction; a TTL of `0` means no expiry and never deletes data.
- History entries are capped at `max_entry_bytes`: the largest `details` values are dropped first and the entry is marked `_truncated`. Total usage is bounded and the bound is documented.
- No `KEYS` or `SCAN`: an index set plus pipelined reads keep each read to two round trips regardless of probe count.
- All keys live under a configurable `prefix`; wrap it in braces (`{myapp:health}`) on Redis Cluster.

Storage backends may also implement three optional methods: `configure()` (receives the registry's retention settings), `forget(names)` (deletes data for removed probes), and `claim_alert(key, ttl_seconds)` (cross-replica alert dedupe). Existing custom backends keep working unchanged. `InMemoryProbeStorage` implements all three.

#### Opt-in alert flap limiting and multi-replica dedupe

Every replica alerted on every transition, and a flapping probe alerted on each flip.

- `alert_min_interval_s` sends at most one alert per probe per interval. The first change after a quiet interval alerts immediately; later changes are coalesced and the latest status is sent when the interval ends. The final state is never lost, and a flap that ends where it started sends nothing.
- `alert_dedupe_window_s`, with a shared backend that implements `claim_alert()` such as `RedisProbeStorage`, has only one replica send each alert. If the claim fails, the alert is sent anyway.

Both are off by default. `on_state_change` callbacks and `/health/alerts` are unaffected.

#### Stream connection cap

New `max_stream_connections` (default `100`) bounds concurrent SSE streams; further requests get `503` with `Retry-After: 30`. `None` removes the cap.

#### `metrics_prefix` for Prometheus metric names

The `/metrics` endpoint names every metric `probe_*` (`probe_healthy`, `probe_latency_ms`, …), which collides with blackbox_exporter's `probe_*` metrics when both are scraped into one Prometheus. New `HealthRegistry(metrics_prefix="fastapi_watch_")` namespaces them. The default `""` keeps the existing names, so current dashboards and alerts are unaffected; a non-empty default may come in a future minor release.

#### Other additions

- `HealthRegistry(probe_timeout=...)` — registry-wide probe deadline (see Bug Fixes).
- `RequestMetricsProbe(app)` — resolves the live middleware from the app (see Bug Fixes).
- `EventLoopProbe(sample_interval_ms=...)` and the `current_lag_ms` detail.
- `PassiveProbe(poll_interval_ms=..., timeout=...)`.
- `circuit_breaker_enabled` parameter on `RequestMetricsProbe` and `FastAPIWebSocketProbe`.
- `CeleryProbe.reply_timeout` (the per-broadcast wait, previously only available as `timeout`).
- `HealthRegistry(storage_timeout=..., max_stream_connections=..., alert_min_interval_s=..., alert_dedupe_window_s=..., stream_token_secret=...)` — see above and Security Fixes.
- `PassiveProbe(expose_error_messages=...)` and `CeleryProbe(include_task_args=...)` — opt back in to the old details (see Security Fixes).
- `RequestMetricsMiddleware(max_routes=500)` — see Performance.
- `BaseProbe.aclose()`.

### Performance

#### Per-route request metrics no longer grow without bound

With `per_route=True`, a request that matched no route was bucketed under its concrete URL, so every distinct 404 — scanners, typos, cache-busting query paths — created a new stats entry holding two rolling windows. Memory grew without bound, and every entry was snapshotted, sorted, and streamed to the dashboard on each update.

**Fix:** unmatched requests share one `"<unmatched>"` entry, and a `Mount` without inner routes (static files, raw ASGI apps) shares one `"<mount>/{path:path}"` entry. Matched routes key on their full template, including `include_router` and `Mount` prefixes, on every FastAPI version.

#### Concurrent requests share one probe run

Each request that found a probe due ran it independently, so a burst of readiness checks, open dashboards, and the poll loop multiplied load on the very dependency being checked (20 concurrent `/health/ready` requests ran a 60-second-interval probe 20 times). Because the runs raced, a slower, older result could also land last and overwrite a newer one, producing spurious state flips and alerts.

**Fix:** a probe that is already being checked is joined rather than restarted; every caller receives the same result, and storage, history, and state-change handling happen exactly once per run. A caller that disconnects mid-run no longer cancels the run for the others.

#### Request metrics middleware is pure ASGI

`RequestMetricsMiddleware` was a Starlette `BaseHTTPMiddleware`, which added about 155 µs to every request of the host app, broke context-variable propagation, and timed streaming responses only until their headers were sent. It is now pure ASGI middleware with the same API: about 3.5 µs per request, context variables propagate normally, streaming responses are timed until their last chunk (background tasks are not timed), WebSocket and lifespan traffic pass straight through, and fastapi-watch's own health and dashboard routes are not counted.

#### Bounded per-route table

A `Mount` with a path parameter (`/t/{tenant}`) produced one per-route entry per concrete tenant; it is now keyed by the parameter name. New `RequestMetricsMiddleware(max_routes=500)` sends any further routes to one `"<other>"` entry as a backstop.

#### Monitoring storage stays off the request path and inside its limits

- **A slow or hung store no longer slows health checks.** Writes to a shared backend waited on the probe run, so a blackholed Redis added `storage_timeout` (2 s) to every `/health/ready` — failing Kubernetes' default 1 s readiness timeout because the *monitoring* store was down. They now run in a bounded background writer per probe.
- **The monitoring store cannot exhaust the application's Redis pool.** Each check issued two or three concurrent pipelines; with 60 probes on the application's client every host `GET` failed with `MaxConnectionsError` during a check. `RedisProbeStorage` now keeps at most `max_connections` (4) commands in flight.
- **The Redis name index is bounded.** Names of renamed or removed probes were only reaped on reads and every write refreshed the index TTL, so they accumulated for the life of the app, and the reap could recreate the index without a TTL. The index is now trimmed by age inside every write's transaction, and reads never write.
- **Entries are bounded in bytes everywhere.** Latest results in Redis and in-memory history entries had no size cap (one 200 KB error made a 410 KB key; a 150-route probe held 2.7 MB in memory).
- **`TTL=0` keeps data.** A key that once had an expiry kept it after switching to `result_ttl_seconds=0` and was wiped when it lapsed; writes now `PERSIST`. Non-finite TTLs are rejected instead of failing every write.
- **Circuit-open results are not re-appended.** Each request while a circuit was open appended another copy of the cached result, pushing real history out of the window.

#### Less work per request

- `RequestMetricsMiddleware` caches whether an endpoint is a health route, takes one clock reading per request and skips Mount handling when there is none (≈1.8 → 1.4 µs of middleware work per request). Passive probes do their formatting at check time (≈1.5 → 1.1 µs per watched call) and time throughput on a monotonic clock, so an NTP step no longer skews `requests_per_minute`.
- The dashboard is rendered in fastapi-watch's thread pool, one render at a time (68 ms of event-loop time for 20 probes × 2,000 history entries), embeds only the charted p95 for older route entries, and `/health/history` serializes in one pass (about 4× faster).
- Open streams share a single-fetch probe's result checked within their interval instead of each running the probe (20 streams ran a database check 100 times in 5 s), and registry-interval probes are polled only while a stream is open.
- Alerters post from their own thread pool, so a slow webhook can no longer occupy the threads blocking probe checks use and time them out; their connect attempts share the request deadline across every resolved address.
- Dashboard tabs whose stream is refused retry the stream alone with jittered backoff (up to 5 minutes) instead of re-rendering the page every 30 s, and renew expired tokens without a reload.

#### A smaller dashboard that stays small

The dashboard embedded every stored result with full `details` — about 4 MB for one 150-route `RequestMetricsProbe` — on every load and every auto-reload, and an open tab kept every live update forever. The page now embeds only the fields its graphs draw and the eight busiest routes (4 MB → 212 KB; render time ~56 ms → ~2 ms), and the browser keeps at most 2,000 points per probe, none older than the longer of 2 hours and the stored history.

#### A lighter stream for the dashboard

`/health/status/stream` sent every probe's full `details` on every event, including every route from `RequestMetricsProbe` with all its fields, although the dashboard draws only the busiest eight. Both streams now accept `?view=dashboard`, which trims `details.routes` to the eight busiest routes and their charted fields (about 42 KB → 1.4 KB per event with 150 routes). The built-in dashboard uses it; clients that don't pass `view` get exactly the previous payload.

### Security Fixes

#### Monitoring a route no longer strips its decorators (also affects v1.7.1)

`discover_routes()` and `watch_router()` wrapped `getattr(route.endpoint, "__wrapped__", route.endpoint)`. That follows any `functools.wraps` decorator, so an endpoint behind an authentication, authorization or rate-limit decorator was replaced by its bare handler: a route that answered `403` served `200` and its data as soon as monitoring was switched on. Re-discovery (`refresh=True`) and a second `discover_routes()` did the same.

**Fix:** only the wrapper fastapi-watch itself installed is ever unwrapped, recognised by identity rather than by an attribute a user decorator could copy. A regression matrix covers async, sync and request-reading decorators across every discovery path on FastAPI 0.100 and the latest release.

**Action:** if you run v1.7.1 with `discover_routes()` or `watch_router()` over decorated endpoints, upgrade.

#### Requires redis-py 4.5.4+

`RedisProbeStorage` cancels commands that exceed `storage_timeout`. Before redis-py 4.5.4 (CVE-2023-28858/28859) a cancelled command could leave a shared connection returning the wrong replies to the application's next command. The `redis` extra now requires `redis>=4.5.4`.

#### Tenant IDs no longer appear in request metrics

A Mount with a path parameter keyed per-route metrics by the concrete value (`/t/acme/...`), publishing tenant IDs on `/health/status`, the dashboard and shared storage. They are keyed by the parameter name now.

#### Smaller fixes

- A non-ASCII `?token=` on a stream endpoint raised inside `compare_digest` and returned `500` with a traceback; it is a `403` now.
- The dashboard page, which embeds a live stream token, is no longer cacheable or frameable.

#### Maintenance write endpoints require `auth=`

With the default `auth=None`, anyone who could reach the service could `POST /health/maintenance` — pinning `/health/ready` at `200` and suppressing every alerter indefinitely — or `DELETE` it to cut a planned window short.

**Fix:** `POST` and `DELETE /health/maintenance` are mounted only when `auth=` is configured. `GET /health/maintenance` and the `set_maintenance()` / `clear_maintenance()` methods are unaffected. The "health endpoints are publicly accessible" startup warning now always logs (through the `fastapi_watch` logger when no `logger=` is passed — previously it only appeared when one was) and names the routes that were left out.

**Upgrading:** if you toggle maintenance over HTTP without `auth=`, those calls now return `405`. Pass `auth=` to `HealthRegistry`, or call `registry.set_maintenance()` / `registry.clear_maintenance()` from your own authenticated endpoint.

#### Webhook URL validation closes several bypasses; redirects are no longer followed

`_validate_webhook_url` only rejected a short list of private ranges in their canonical spelling, and alerter requests followed redirects. `0.0.0.0`, IPv4-mapped IPv6 (`::ffff:127.0.0.1`), integer/octal/hex hosts (`http://2130706433/`), multicast/reserved addresses, and `localhost.` all passed, and any validated public URL could redirect the POST — with its auth headers — to an internal address.

**Fix:** every IP literal that is not publicly routable unicast is rejected, in any spelling a resolver accepts, along with `localhost` / `*.localhost`. Alerter requests use an opener that never follows redirects.

#### Misconfigured alerters no longer write secrets to the log

A webhook URL with credentials (`https://user:pw@host/`), or a header value or OpsGenie `api_key` ending in a newline, made every post fail before connecting — with an error that quoted the password or token (`nonnumeric port: 'pw@host'`, `Invalid header value b'Bearer <token>\n'`), logged with every alert. Every alert was lost, and the secret landed in the application's logs.

**Fix:** all three raise `ValueError` when the alerter is created, without echoing the value. A full alert queue is now also logged through the `fastapi_watch` logger when no `logger=` is passed (it was silent).

#### Basic auth no longer short-circuits on the username

The `basic` auth check compared the password only when the username matched, so response timing could reveal whether a guessed username was correct. Both are now compared before the result is combined.

#### Exception messages no longer appear in health responses

Passive and route probes stored `"<type>: <message>"` as `details["last_error"]`, served on `/health/status`, the streams, and the dashboard — public unless `auth=` is set. Driver messages embed connection strings, credentials, and bound query parameters; in testing an unauthenticated `/health/status` exposed an email address and SSN from a failed `INSERT`. `HTTPException.detail` was exposed the same way.

**Fix:** `last_error` is the exception type only (`"IntegrityError"`, `"HTTPException"`, or `"HTTP 503"` for an error status returned without raising). The full exception is logged at debug level on the `fastapi_watch` logger. `expose_error_messages=True` restores the message.

#### `CeleryProbe(detailed=True)` no longer publishes task arguments

Task `args` and `kwargs` — often user data or secrets — were included for every active, reserved, and scheduled task. They are dropped unless `include_task_args=True`, and each list is capped at 50 entries per worker with a `truncated` count.

#### Maintenance endpoints resist cross-site requests

A cross-site `<form method="post">` to `/health/maintenance` sent with the browser's cached Basic credentials switched on indefinite maintenance — `/ready` pinned at `200`, alerts muted. `POST` and `DELETE /health/maintenance` now reject `Sec-Fetch-Site: cross-site` and mismatched `Origin` headers with `403`, and non-empty non-JSON bodies with `415`. curl and other clients that send no `Origin` are unaffected.

#### Blank credentials are rejected

`auth={"type": "apikey", "key": ""}` admitted every request that sent no header. An empty or whitespace `key`, `username`, or `password` — or an unknown auth type — now raises `ValueError` at startup, without echoing the value.

#### Dashboard stream tokens can no longer be used to guess credentials

The stream token's signing key was a single SHA-256 of the password or API key, and tokens travel in URLs (and so in proxy logs and browser history), allowing fast offline guessing of weak credentials. The key is now derived with scrypt once at startup (PBKDF2 where scrypt is unavailable). New `stream_token_secret=` signs tokens with a key you provide — use it to share tokens across workers with a callable `auth`. A stream authorized only by a token now closes when the token expires; header-authenticated streams are unaffected.

### Infrastructure

- **CI on every push and tag**, not only pull requests, with a `redis:7` service so `RedisProbeStorage` is tested against a real server. The concurrency group now keys on the branch for pushes so unrelated runs do not cancel each other.
- **CI on pull requests.** A new `ci.yml` workflow runs the suite across Python 3.10–3.15 against FastAPI 0.100 (the oldest supported release) and the latest FastAPI. Previously tests only ran on Python 3.11 at publish time. Optional extras install individually so a driver without wheels for a new Python (currently `aiokafka` on 3.15) skips its own tests rather than blocking the run.
- **Python 3.15** added to the supported classifiers.
- **Python 3.16 readiness.** CI also runs against the 3.16 pre-release as a non-blocking job; the classifier will be added once 3.16 is final. `asyncio.iscoroutinefunction`, which 3.16 removes (and which warned on every `watch()` from 3.14), is replaced by `inspect.iscoroutinefunction`.
- **Lint in CI.** A Ruff job runs on every pull request; `src/` and `tests/` are clean (the test suite had 21 findings, plus an import-order violation in `registry.py`).
- **Deprecations fail the suite.** A `DeprecationWarning` raised from `fastapi_watch` code is now a test error, so APIs slated for removal are caught in CI before a Python or dependency release removes them. It immediately caught the `aiohttp.BasicAuth` use above.
- **One passive-probe implementation.** `FastAPIRouteProbe` now subclasses `PassiveProbe` instead of duplicating ~200 lines of recording and reporting logic; public constructors and details keys are unchanged (passive probes additionally report `requests_per_minute`).
- `fastapi_watch.__version__` reported `1.7.0` in the 1.7.1 release; it now matches the package version.

---

## v1.7.1

**FastAPI 0.137.0 compatibility fix.**

### Bug Fixes

#### `discover_routes` and `watch_router` not recording traffic for routes behind `include_router` (FastAPI 0.137+)

FastAPI 0.137.0 changed how included routers are stored: instead of copying routes with their full paths into `app.routes`, it now stores `_IncludedRouter` wrapper objects whose routes retain short (prefix-less) paths. Request handling is done through a lazily-built `_EffectiveRouteContext` whose `dependant.call` is derived from `route.endpoint` at first-request time — not from `route.dependant.call`.

The old patching strategy only updated `route.dependant.call`, which was invisible to 0.137.0's request pipeline. This meant `discover_routes` would find and register probes but never record any traffic when routes were included with a prefix via `app.include_router(router, prefix="...")`.

**Fix:** `discover_routes` now patches both `route.dependant.call` and `route.endpoint`. After patching, `_IncludedRouter` lazy caches are invalidated (via `APIRouter._mark_routes_changed()` when available, falling back to resetting `_effective_candidates_version`) so the next request rebuilds the effective route context with the wrapped endpoint. `watch_router` already patched `route.endpoint` but now also gets the cache invalidation step.

#### `discover_routes` → `watch_router` override not removing the discover probe (older FastAPI, same session)

When `discover_routes()` was called first and then `watch_router()` was called on the same session in FastAPI <0.137, the discover probe was not removed and a duplicate probe appeared in the registry. This happened because in older FastAPI routes are *copied* into `app.routes` when included — so after `discover_routes` patched the copy's `endpoint`, the `watch_router` primary pass looked for the original endpoint in `router_endpoints` and missed it, falling back to the unpatched router route which had no `_fastapi_watch` marker, creating a second probe without removing the first.

**Fix:** The primary pass now also checks `ep.__wrapped__` (set by `functools.wraps`) when looking up endpoints in `router_endpoints`, so it correctly finds app-route copies that were already patched by `discover_routes`. `_process_route` also unwraps before watching to prevent double-wrapping.

### Compatibility

Tested and passing across FastAPI 0.116.1 – 0.137.0.

---

## v1.7.0

**Probe introspection, failing field, OpsGenie alerter, and security hardening.**

### New Features

#### `GET /health/probes` — probe introspection
- New endpoint lists all registered probes and their configuration without running any checks: `name`, `critical`, `tags`, `description`, `poll_interval_ms`, `circuit_breaker_enabled`. Useful for verifying what the registry is monitoring and its per-probe settings. Subject to the same `auth` as all other health endpoints.

#### `HealthRegistry.reset_circuit(probe_name)` — manual circuit breaker reset
- New public method clears a probe's open circuit state and consecutive failure count immediately, without waiting for the cooldown to expire. Does not reset the lifetime trips counter (observability). Returns `self` for chaining.

#### `?probe=` name filter on health endpoints
- `/health/ready`, `/health/status`, `/health/ready/stream`, and `/health/status/stream` now accept `?probe=name1,name2` to filter results by exact probe name. Works alongside `?tag=` with AND logic — a result must match both filters when both are supplied.

#### `failing` field in `HealthReport`
- Every health report now includes `failing: list[str]` — the names of critical probes whose status is `UNHEALTHY`. Always present (empty list when nothing is failing). Propagates to all endpoints and SSE streams automatically. Makes the failing probe names immediately readable at the top level of a 503 response without parsing the full `probes` array.

#### `OpsGenieAlerter`
- New alerter for OpsGenie Alerts API v2. Triggers P1 on UNHEALTHY, P3 on DEGRADED, closes on HEALTHY. Uses `fastapi-watch:<probe-name>` as a stable dedup alias. Supports `region="us"` (default) and `region="eu"`. Outbound URLs are validated against an explicit hostname allowlist before each request.

### Security Fixes

- **`WebhookAlerter.__repr__`** now returns `url='<redacted>'`, consistent with `SlackAlerter` and `TeamsAlerter`. Previously the full webhook URL (which may contain tokens) was exposed.
- **`RabbitMQProbe.__repr__`** now redacts the password from the AMQP URL. Management API exceptions now use `type(exc).__name__` only — raw exception text (which may contain credentials) no longer appears in `/health/status` response bodies.
- **Registry probe exception logger** no longer embeds `str(exc)` in the log format string. Full exception details are still available via `logger.exception()` traceback, but the format string itself no longer risks surfacing credentials from database driver error messages.

---

## v1.6.1

**Bug fix: alert worker hang under Python 3.11.**

### Bug Fixes

#### `_alert_loop` deadlock on Python 3.11
- `_alert_loop` previously used `asyncio.wait_for(queue.get(), timeout=1.0)` to periodically check the `_shutting_down` flag. On Python 3.11, a known race condition in `asyncio.wait_for` (bpo-46607, fixed in 3.12) can cause the method to deadlock when task cancellation arrives at the same moment the 1-second timeout fires. This manifested as tests hanging indefinitely in CI when pytest-asyncio's event loop cleanup tried to cancel uncollected `_alert_worker` tasks.
- Fixed by replacing `wait_for` with a plain `await queue.get()`. The `_shutting_down` check was redundant — `_shutdown()` and the signal handler both cancel the worker task directly.

#### CI hardening
- Added `timeout-minutes: 10` to the GitHub Actions test job so a hanging test can no longer block the pipeline for hours.
- Added `pytest-timeout` to CI dependencies and a 30-second per-test timeout in `pyproject.toml`.
- Added `asyncio_default_fixture_loop_scope = "function"` to pytest config to silence the pytest-asyncio deprecation warning and ensure per-test event loop isolation.

---

## v1.6.0

**Custom health endpoints and bounded alert dispatch.**

### New Features

#### `serve_routes=False` and `get_report()` for custom health endpoints
- `HealthRegistry` now accepts `serve_routes=False` (default `True`). When `False`, no health routes are registered on the FastAPI app — useful when you want to expose probe status on your own endpoint with custom auth, path, or response shaping.
- New `async def get_report() -> HealthReport` public method returns the current health report without going through HTTP. Intended for use with `serve_routes=False`:
  ```python
  registry = HealthRegistry(app, serve_routes=False)

  @app.get("/internal/health", dependencies=[Depends(my_auth)])
  async def my_health():
      report = await registry.get_report()
      return report.model_dump()
  ```

#### Bounded alert dispatch queue
- Alert dispatch previously called `asyncio.create_task()` on every probe state change, creating an unbounded number of concurrent tasks under alert storms.
- A single background worker (`_alert_loop`) now drains an `asyncio.Queue(maxsize=256)` serially. The worker starts lazily on the first alert and is cancelled cleanly on shutdown. When the queue is full, new alerts are dropped with a warning log.

---

## v1.5.11

**Security hardening.**

### Compatibility

fastapi-watch 1.5.11 has identical dependency constraints to 1.5.10 (`fastapi>=0.100.0`, `pydantic>=2.0`). Upgrading from 1.5.10 will not downgrade or change any other packages in your environment.

### Security Fixes

#### SSRF protection on webhook alerters
- `WebhookAlerter`, `SlackAlerter`, and `TeamsAlerter` now validate the webhook URL at construction time via `_validate_webhook_url()`. URLs targeting private/loopback/link-local IP ranges (`10.x`, `172.16–31.x`, `192.168.x`, `127.x`, `169.254.x`, `100.64.x`, `::1`, `fc00::/7`, `fe80::/10`) and `localhost` are rejected with a `ValueError`. Misconfiguration now fails at startup rather than silently at send time.

#### Generic probe error responses
- Exceptions that escape a probe's `check()` method now return `error: "probe check failed"` to HTTP clients. Full exception details (including connection strings and passwords that may appear in third-party library error messages) are forwarded only to the configured logger. Affects all built-in probes: Kafka, Celery, Memcached, RabbitMQ, and TCP previously exposed `str(exc)` directly.

#### Strict auth callable check
- The `auth` callable return value is now checked with `result is not True` instead of truthiness. Previously, returning `None`, `0`, or `""` would incorrectly grant access. These values now correctly deny with `403`.

#### Secret redaction in alerter repr
- `__repr__` on all alerters (`WebhookAlerter`, `SlackAlerter`, `TeamsAlerter`, `PagerDutyAlerter`) no longer exposes webhook URLs, auth headers, or routing keys. All sensitive fields are replaced with `<redacted>`.

#### Dashboard XSS prevention
- `ProbeResult.details` keys are now HTML-escaped before insertion into the dashboard (both server-side rendering and the client-side JavaScript live update path). Previously, a probe returning a key like `<script>alert(1)</script>` in its details dict would execute JavaScript in any browser viewing the dashboard.
- Client-side `fmtDetailVal` now escapes all string values and nested dict keys via `escAttr()` before inserting via `innerHTML`.

#### Probe name validation
- `registry.add()` now rejects probe names containing characters outside `[A-Za-z0-9_\-.:]+` with a `ValueError`. This prevents newlines and quotes from reaching Prometheus metric label output and other rendering surfaces.

#### Prometheus label injection
- Probe names are now escaped in Prometheus metric output via `_escape_label()`, which backslash-escapes `\`, `"`, and `\n` per the Prometheus text format spec.

#### Custom dashboard `.py` file execution removed
- `HealthRegistry(dashboard=<path>)` no longer accepts `.py` files. Previously, `.py` files were loaded via `importlib` and their `render_dashboard` function called — arbitrary code execution from a file path. The `dashboard` parameter now accepts `True` (built-in), `False` (disabled), a `Callable`, or a `.html`/`.htm` file path.

#### Path traversal guard on dashboard file
- The dashboard file path is now resolved with `Path.resolve()` before use.

#### Maintenance endpoint DoS fix
- `POST /health/maintenance` with `{"minutes": 1e308}` or `{"minutes": -1}` previously caused an unhandled `OverflowError` or validation failure resulting in a 500. The `minutes` field is now validated as `0 < minutes ≤ 525600` (one year) at the Pydantic model level, returning `422` for out-of-range values.

#### Tag filter memory bound
- `_parse_tag_filter` now caps the tag list at 50 entries. Previously, a `?tag=a,b,c,...` query string with thousands of values would create an arbitrarily large `frozenset`.

#### auth=None warning
- `HealthRegistry` now logs a warning at startup when `auth=None` (the default), making it explicit that health endpoints are publicly accessible.

---

## v1.5.10

**Route auto-discovery, probe descriptions, tag-based filtering, and dashboard improvements.**

### New Features

#### Route auto-discovery
- **`registry.discover_routes()`** — monitors every registered FastAPI route with one call. No decorators required. Accepts `exclude_paths`, `include_paths`, `include_methods`, `tags`, `ws_probe_kwargs`, `name_fn`, `refresh`, and any `FastAPIRouteProbe` keyword argument
- **`registry.watch_router(router, ...)`** — scopes auto-discovery to a single `APIRouter` with its own tags, thresholds, and criticality. Call after `app.include_router`. Accepts all the same options as `discover_routes`
- **Monitoring priority system** — `@probe.watch` (highest) > `watch_router` > `discover_routes` (lowest). All three are safe to mix; each checks the current state of a route and skips or replaces accordingly. No double-counting, no conflicts
- **WebSocket auto-discovery** — both methods automatically detect `APIWebSocketRoute` and create a `FastAPIWebSocketProbe`. Pass `ws_probe_kwargs={...}` to forward WebSocket-specific options
- **`name_fn`** — optional callable `(route) -> str` to customise probe names. Defaults to `route.name` (the handler function name)
- **`include_paths`** — whitelist complement to `exclude_paths`; supports fnmatch glob patterns (e.g. `["/api/*"]`). `exclude_paths` takes precedence when both are set
- **Glob patterns in `exclude_paths`** — supports fnmatch patterns such as `["/internal/*", "/admin/*"]` in both methods
- **`discover_routes(refresh=True)`** — re-monitors previously auto-discovered routes with updated options, without touching `@probe.watch` or `watch_router` routes
- **`watch_router(group=True)`** — collects all probes for the router into a `ProbeGroup` before registering, so group-level tags propagate to every member probe automatically

#### Tags and filtering
- **Probe tags** — all probe types accept `tags=[...]`. Tags appear in results and enable filtered endpoints
- **FastAPI route tags auto-merged** — `discover_routes` and `watch_router` pull each route's OpenAPI tags into probe tags automatically; user-supplied tags are appended on top
- **`ProbeGroup` tags** — `ProbeGroup(tags=["db"])` propagates tags to all member probes when included in the registry
- **Comma-separated OR filtering** — `?tag=payments,orders` returns probes matching either tag. Both `/health/ready` and `/health/status` support this
- **SSE stream tag filtering** — `/health/ready/stream?tag=payments` and `/health/status/stream?tag=payments` filter the live stream
- **Empty `?tag=` treated as no filter** — previously an empty tag param would return zero probes; now correctly ignored

#### Probe descriptions
- **Probe descriptions** — all probes accept `description`. Auto-discovered probes use `GET /items/{id}` / `WS /ws/chat` style descriptions automatically

#### Dashboard
- **Tag filter bar** — clickable tag buttons above the probe grid for instant client-side filtering; a Clear button resets all active filters
- **Tag badges** — probe cards show tag chips below the probe name
- **Probe search** — text input filters probe cards by name in real time; works alongside the tag filter (both must match)

### Bug Fixes
- Fixed `discover_routes`/`watch_router` crashing on non-`APIRoute` objects (e.g. `Mount`) in `app.routes`
- Fixed `FastAPIWebSocketProbe.watch` not counting messages when WebSocket is passed as a positional argument

---

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

- `PassiveProbe` base class — monitors your own functions via `@probe.watch` instead of making synthetic requests
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
- `RouteProbe` (later renamed `FastAPIRouteProbe`) — monitors FastAPI route handlers
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
