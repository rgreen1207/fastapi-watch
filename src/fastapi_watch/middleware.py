"""Request metrics middleware and associated probe.

Add :class:`RequestMetricsMiddleware` to your FastAPI app and pass the app to
:class:`RequestMetricsProbe` to get automatic, app-wide request telemetry
without decorating individual routes.
"""
import asyncio
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any, Callable

from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .models import ProbeResult, ProbeStatus
from .probes.base import BaseProbe, _calc_percentiles, _update_ema
from .probes.route import CLIENT_CLOSED_REQUEST

if TYPE_CHECKING:
    from starlette.applications import Starlette

# Bound on the middleware-stack walk: real stacks are a handful of layers deep.
_MAX_STACK_DEPTH = 64
# Per-route snapshots taken between yields to the event loop (well under 1 ms).
_ROUTES_PER_SLICE = 50


# ---------------------------------------------------------------------------
# Internal stats bucket (shared between middleware and probe)
# ---------------------------------------------------------------------------

class _RouteStats:
    """Thread-safe stats accumulator for one route or the aggregate."""

    __slots__ = (
        "_lock",
        "_request_count",
        "_error_count",
        "_consecutive_errors",
        "_avg_rtt_ms",
        "_rtt_window",
        "_request_timestamps",
        "_error_window",
        "_window_error_count",
        "_ema_alpha",
        "_min_error_status",
    )

    def __init__(self, window_size: int, ema_alpha: float, min_error_status: int = 500) -> None:
        self._lock = threading.Lock()
        self._request_count: int = 0
        self._error_count: int = 0
        self._consecutive_errors: int = 0
        self._avg_rtt_ms: float | None = None
        self._rtt_window: deque[float] = deque(maxlen=window_size)
        self._request_timestamps: deque[float] = deque(maxlen=window_size)
        # True = error; drives error_rate so health reflects recent traffic only.
        self._error_window: deque[bool] = deque(maxlen=window_size)
        self._window_error_count: int = 0
        self._ema_alpha = ema_alpha
        self._min_error_status = min_error_status

    def record(self, status_code: int, rtt_ms: float, now: float | None = None) -> None:
        """Record one request; *now* is any monotonic timestamp in seconds."""
        if now is None:
            now = time.perf_counter()
        with self._lock:
            self._request_count += 1
            self._request_timestamps.append(now)
            is_error = status_code >= self._min_error_status
            if len(self._error_window) == self._error_window.maxlen and self._error_window[0]:
                self._window_error_count -= 1
            self._error_window.append(is_error)
            self._window_error_count += is_error
            if is_error:
                self._error_count += 1
                self._consecutive_errors += 1
            else:
                self._consecutive_errors = 0
            self._avg_rtt_ms = _update_ema(self._avg_rtt_ms, rtt_ms, self._ema_alpha)
            self._rtt_window.append(rtt_ms)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            request_count = self._request_count
            error_count = self._error_count
            window_len = len(self._error_window)
            error_rate = self._window_error_count / window_len if window_len else 0.0
            avg_rtt_ms = round(self._avg_rtt_ms or 0.0, 2)
            consecutive_errors = self._consecutive_errors
            rtt_copy = list(self._rtt_window)
            requests_per_minute: float | None = None
            timestamps = self._request_timestamps
            if len(timestamps) >= 2:
                span = timestamps[-1] - timestamps[0]
                if span > 0:
                    requests_per_minute = round((len(timestamps) - 1) / span * 60, 2)
        # sort outside the lock — reduces lock hold time under concurrent recording
        p50, p95, p99 = _calc_percentiles(rtt_copy, 0.5, 0.95, 0.99)
        return {
            "request_count": request_count,
            "error_count": error_count,
            "error_rate": round(error_rate, 4),
            "consecutive_errors": consecutive_errors,
            "avg_rtt_ms": avg_rtt_ms,
            "p50_rtt_ms": p50,
            "p95_rtt_ms": p95,
            "p99_rtt_ms": p99,
            "requests_per_minute": requests_per_minute,
        }


UNMATCHED_ROUTE = "<unmatched>"


def _mount_template(mount_prefix: str, path_params: dict, template: str | None) -> str:
    """Put a Mount's parameter names back in place of their values.

    Routing records only the concrete prefix (``/t/acme``), but the Mount's
    parameters are in ``path_params``: each prefix segment equal to one of
    them becomes ``{name}`` (``/t/{tenant}``), so a tenant ID neither makes a
    key of its own nor shows up in the metrics.  The inner route's own
    parameters (those named in *template*) are not the Mount's and are skipped.
    """
    values: dict[str, str] = {}
    for name, value in path_params.items():
        if template is not None and ("{" + name + "}" in template or "{" + name + ":" in template):
            continue
        values.setdefault(str(value), name)
    if not values:
        return mount_prefix
    return "/".join("{" + values[segment] + "}" if segment in values else segment for segment in mount_prefix.split("/"))


def _route_key(scope: dict, entry_root_path: str) -> str:
    """Return the per-route bucket for a finished request.

    Keys are route *templates* (``/users/{id}``), never concrete URLs: a key per
    URL would let scanners and 404 floods grow the stats table without bound.

    * A matched route keys on its full template, including any router prefix
      and Mount prefix.  FastAPI 0.137+ keeps included routes' short paths on
      ``route.path`` and the full one on the effective route context.  A
      Mount's path parameters are restored to ``{name}`` form.
    * A Mount that matched no inner route (static files, raw ASGI apps, or a
      404 inside a mounted app) shares one ``<mount>/{path:path}`` bucket.
    * Anything else shares the single ``<unmatched>`` bucket.
    """
    root_path = scope.get("root_path", "")
    if root_path == entry_root_path:
        mount_prefix = ""
    else:
        mount_prefix = root_path[len(entry_root_path):] if root_path.startswith(entry_root_path) else ""
    effective = (scope.get("fastapi") or {}).get("effective_route_context")
    template = getattr(effective, "path", None) or getattr(scope.get("route"), "path", None)
    if mount_prefix:
        path_params = scope.get("path_params")
        if path_params:
            mount_prefix = _mount_template(mount_prefix, path_params, template)
    if template:
        return mount_prefix + template
    if mount_prefix:
        return mount_prefix + "/{path:path}"
    return UNMATCHED_ROUTE


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

OTHER_ROUTE = "<other>"

# Bound to fastapi_watch.registry._is_health_handler on first use, so this module
# has no load-time dependency on registry (which may come to import it).
_is_health_handler: "Callable[[Any], bool] | None" = None


# Most endpoints whose health-handler verdict a middleware caches.  Endpoints
# are the app's route handlers, a fixed set; the cap only guards odd apps.
_MAX_CACHED_ENDPOINTS = 1024


def _is_health_route(scope: Scope, cache: "dict[Any, bool] | None" = None) -> bool:
    """True when routing sent the request to a HealthRegistry's own handler.

    *cache* maps endpoints to the verdict, so a request costs a dict lookup
    rather than an unwrap-and-compare of the handler's qualified name.
    """
    global _is_health_handler
    effective = (scope.get("fastapi") or {}).get("effective_route_context")
    endpoint = getattr(effective, "endpoint", None) or getattr(scope.get("route"), "endpoint", None)
    if endpoint is None:
        return False
    if cache is not None:
        try:
            cached = cache.get(endpoint)
        except TypeError:  # an unhashable callable endpoint
            cache = cached = None
        if cached is not None:
            return cached
    if _is_health_handler is None:
        from .registry import _is_health_handler as is_health_handler

        _is_health_handler = is_health_handler
    is_health = _is_health_handler(endpoint)
    if cache is not None and len(cache) < _MAX_CACHED_ENDPOINTS:
        cache[endpoint] = is_health
    return is_health


class RequestMetricsMiddleware:
    """ASGI middleware that records request metrics across all routes.

    Attach it with ``app.add_middleware(...)``, then pass the *app* to
    :class:`RequestMetricsProbe` to expose the collected data as a health probe.
    Starlette builds middleware instances lazily (on the first request or
    lifespan event), so the probe finds the live instance in the app's
    middleware stack when it first checks.

    It is plain ASGI middleware, not a ``BaseHTTPMiddleware``: requests pass
    through with one wrapped ``send`` and no extra task, context variables set
    by the app propagate normally, and a streaming response is timed until its
    last body chunk is sent rather than until its headers are.  WebSocket and
    lifespan traffic pass straight through, and requests served by a
    :class:`~fastapi_watch.HealthRegistry`'s own routes are not counted, so
    health checks and dashboard streams never skew the app's numbers.

    Args:
        app: The ASGI application (injected automatically by FastAPI/Starlette).
        per_route: When ``True`` (default), metrics are also broken down by
            route path template (e.g. ``/users/{id}``), available in the probe's
            ``details["routes"]``.  Requests that match no route share a single
            ``"<unmatched>"`` entry, and a Mount without inner routes (e.g.
            static files) shares one ``"/static/{path:path}"``-style entry, so
            the table stays bounded however many distinct URLs arrive.  A
            Mount's path parameters are keyed by name too
            (``app.mount("/t/{tenant}", sub)`` gives ``/t/{tenant}/...``).
            When ``False``, only aggregate stats are tracked.
        window_size: Number of recent requests used for the error rate,
            percentiles, and requests-per-minute (default ``200``).
        ema_alpha: Smoothing factor for the exponential moving average RTT
            (default ``0.1``; higher = reacts faster to changes).  Must be in
            ``(0, 1]``.
        min_error_status: HTTP status codes at or above this value are counted
            as errors (default ``500``).  4xx client errors such as 404 are not
            counted as errors by default; set to ``400`` to include them.  A
            request whose client hung up before the response started (an
            aborted upload) is recorded as ``499``, not as a server error.
        max_routes: Most per-route entries kept (default ``500``).  Requests
            for further routes share one ``"<other>"`` entry — a backstop for
            apps whose route keys are not a fixed set.

    Raises:
        ValueError: If ``window_size`` or ``max_routes`` is not positive, or
            ``ema_alpha`` is outside ``(0, 1]``.

    Example::

        from fastapi import FastAPI
        from fastapi_watch import HealthRegistry, RequestMetricsMiddleware, RequestMetricsProbe

        app = FastAPI()
        app.add_middleware(RequestMetricsMiddleware, per_route=True)

        registry = HealthRegistry(app)
        registry.add(RequestMetricsProbe(app, max_error_rate=0.05))
    """

    def __init__(
        self,
        app: ASGIApp,
        per_route: bool = True,
        window_size: int = 200,
        ema_alpha: float = 0.1,
        min_error_status: int = 500,
        max_routes: int = 500,
    ) -> None:
        if window_size <= 0:
            raise ValueError(f"window_size must be positive, got {window_size!r}")
        if not 0 < ema_alpha <= 1:
            raise ValueError(f"ema_alpha must be in (0, 1], got {ema_alpha!r}")
        if max_routes <= 0:
            raise ValueError(f"max_routes must be positive, got {max_routes!r}")
        self.app = app
        self.per_route = per_route
        self._window_size = window_size
        self._ema_alpha = ema_alpha
        self._min_error_status = min_error_status
        self._max_routes = max_routes
        self._aggregate = _RouteStats(window_size, ema_alpha, min_error_status)
        self._routes: dict[str, _RouteStats] = {}
        self._routes_lock = threading.Lock()
        self._health_endpoints: dict[Any, bool] = {}

    def _get_or_create(self, path: str) -> _RouteStats:
        # Lock-free hit for known routes — the common case; dict reads are atomic.
        stats = self._routes.get(path)
        if stats is not None:
            return stats
        with self._routes_lock:
            if path not in self._routes and len(self._routes) >= self._max_routes:
                path = OTHER_ROUTE
            stats = self._routes.get(path)
            if stats is None:
                stats = self._routes[path] = _RouteStats(self._window_size, self._ema_alpha, self._min_error_status)
            return stats

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        # Routing grows root_path by each Mount it enters; remember the starting
        # value so the mount prefix can be recovered afterwards.
        entry_root_path = scope.get("root_path", "")
        status_code = 500  # a response that never starts counts as a server error
        started = done = client_gone = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, started, done, client_gone
            message_type = message["type"]
            if message_type == "http.response.start":
                status_code = message["status"]
                started = True
            elif message_type == "http.response.body" and not message.get("more_body", False) and not done:
                # The last body chunk completes the response: record it here,
                # so background tasks that run afterwards are not timed.
                done = True
                try:
                    await send(message)
                finally:
                    self._record(scope, status_code, start, entry_root_path)
                return
            try:
                await send(message)
            except OSError:
                # ASGI servers raise an OSError subclass when the client has gone.
                # Anything else (h11 refusing an invalid header) is a server error.
                client_gone = True
                raise

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            # Unhandled exceptions become a 500 in Starlette's ServerErrorMiddleware,
            # which sits outside every user middleware — so this is the only place
            # the request can be counted.  Re-raise so that response is unchanged.
            if not done:
                done = True
                if client_gone or isinstance(exc, ClientDisconnect):
                    # The client hung up (an aborted upload or download): not a
                    # server error.  Keep the status already sent, else 499.
                    status_code = status_code if started else CLIENT_CLOSED_REQUEST
                else:
                    status_code = 500
                self._record(scope, status_code, start, entry_root_path)
            raise
        if not done:
            # No final body chunk (e.g. a ``pathsend`` file response): the
            # request ends when the app returns.
            self._record(scope, status_code, start, entry_root_path)

    def _record(self, scope: Scope, status_code: int, start: float, entry_root_path: str) -> None:
        """Record one finished request in the aggregate and its per-route bucket."""
        end = time.perf_counter()
        if _is_health_route(scope, self._health_endpoints):
            return
        # Unrounded: snapshots round what they report, so recording need not.
        rtt_ms = (end - start) * 1000
        self._aggregate.record(status_code, rtt_ms, end)

        if self.per_route:
            self._get_or_create(_route_key(scope, entry_root_path)).record(status_code, rtt_ms, end)


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------

class RequestMetricsProbe(BaseProbe):
    """Health probe that reads metrics from a :class:`RequestMetricsMiddleware`.

    Reports UNHEALTHY when the aggregate error rate or average RTT exceeds the
    configured thresholds.  When ``per_route=True`` is set on the middleware,
    ``details["routes"]`` contains a per-path breakdown.

    Before any requests arrive, the probe returns HEALTHY with
    ``"no requests observed yet"`` in details.

    Args:
        middleware: The app the middleware was added to (recommended), or a
            :class:`RequestMetricsMiddleware` instance that requests actually
            pass through.  Given an app, the live instance is looked up in
            ``app.middleware_stack`` on the first check after Starlette builds
            it; if the stack is built but holds no ``RequestMetricsMiddleware``
            the probe reports UNHEALTHY with a configuration error.
        name: Probe name (default ``"request_metrics"``).
        max_error_rate: Error rate (0–1) above which the probe is UNHEALTHY
            (default ``0.1`` = 10 %).
        max_avg_rtt_ms: Average RTT threshold in milliseconds.  ``None``
            disables this check.
        poll_interval_ms: Per-probe poll interval override.
        circuit_breaker_enabled: Whether the registry's circuit breaker may
            suspend this probe (default ``False``).  The probe only reads
            in-memory counters, so suspending it protects nothing and would
            serve a stale result while traffic recovers.

    Example::

        registry.add(RequestMetricsProbe(app, max_error_rate=0.02, max_avg_rtt_ms=500))
    """

    def __init__(
        self,
        middleware: "RequestMetricsMiddleware | Starlette",
        name: str = "request_metrics",
        max_error_rate: float = 0.1,
        max_avg_rtt_ms: float | None = None,
        poll_interval_ms: int | None = None,
        circuit_breaker_enabled: bool = False,
    ) -> None:
        if isinstance(middleware, RequestMetricsMiddleware):
            self._middleware: RequestMetricsMiddleware | None = middleware
            self._app = None
        elif hasattr(middleware, "middleware_stack"):
            self._middleware = None
            self._app = middleware
        else:
            raise TypeError(
                "RequestMetricsProbe needs the app RequestMetricsMiddleware was added to, "
                f"or a RequestMetricsMiddleware instance; got {type(middleware).__name__}"
            )
        self.name = name
        self.max_error_rate = max_error_rate
        self.max_avg_rtt_ms = max_avg_rtt_ms
        self.poll_interval_ms = poll_interval_ms
        self.circuit_breaker_enabled = circuit_breaker_enabled

    def _resolve_middleware(self) -> "RequestMetricsMiddleware | None | bool":
        """Return the live middleware, ``None`` if the stack is not built yet,
        or ``False`` if it is built without a ``RequestMetricsMiddleware``."""
        if self._middleware is not None:
            return self._middleware
        node = self._app.middleware_stack
        if node is None:
            return None
        for _ in range(_MAX_STACK_DEPTH):
            if node is None:
                break
            if isinstance(node, RequestMetricsMiddleware):
                self._middleware = node
                return node
            node = getattr(node, "app", None)
        return False

    async def check(self) -> ProbeResult:
        middleware = self._resolve_middleware()
        if middleware is False:
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.UNHEALTHY,
                latency_ms=0.0,
                error="RequestMetricsMiddleware is not installed on the app",
            )
        if middleware is None:
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.NO_DATA,
                latency_ms=0.0,
                details={"message": "no requests observed yet"},
            )

        snap = middleware._aggregate.snapshot()

        if snap["request_count"] == 0:
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.NO_DATA,
                latency_ms=0.0,
                details={"message": "no requests observed yet"},
            )

        reasons: list[str] = []
        if snap["error_rate"] > self.max_error_rate:
            reasons.append(
                f"error rate {snap['error_rate']:.1%} exceeds threshold {self.max_error_rate:.1%}"
            )
        if self.max_avg_rtt_ms is not None and snap["avg_rtt_ms"] > self.max_avg_rtt_ms:
            reasons.append(
                f"avg RTT {snap['avg_rtt_ms']:.1f} ms exceeds threshold {self.max_avg_rtt_ms:.1f} ms"
            )

        status = ProbeStatus.UNHEALTHY if reasons else ProbeStatus.HEALTHY
        details: dict[str, Any] = dict(snap)

        if middleware.per_route:
            with middleware._routes_lock:
                routes_copy = list(middleware._routes.items())
            # Each snapshot sorts its route's latency window: with hundreds of
            # routes that is milliseconds of CPU, so let requests run in between.
            routes: dict[str, Any] = {}
            for index, (path, stats) in enumerate(sorted(routes_copy)):
                if index and not index % _ROUTES_PER_SLICE:
                    await asyncio.sleep(0)
                routes[path] = stats.snapshot()
            details["routes"] = routes

        return ProbeResult(
            name=self.name,
            status=status,
            latency_ms=snap["avg_rtt_ms"],
            error="; ".join(reasons) if reasons else None,
            details=details,
        )
