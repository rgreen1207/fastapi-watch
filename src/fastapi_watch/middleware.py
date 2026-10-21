"""Request metrics middleware and associated probe.

Add :class:`RequestMetricsMiddleware` to your FastAPI app and pass the app to
:class:`RequestMetricsProbe` to get automatic, app-wide request telemetry
without decorating individual routes.
"""
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from .models import ProbeResult, ProbeStatus
from .probes.base import BaseProbe, _calc_percentiles, _update_ema

if TYPE_CHECKING:
    from starlette.applications import Starlette

# Bound on the middleware-stack walk: real stacks are a handful of layers deep.
_MAX_STACK_DEPTH = 64


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

    def record(self, status_code: int, rtt_ms: float) -> None:
        with self._lock:
            self._request_count += 1
            self._request_timestamps.append(time.monotonic())
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


def _route_key(scope: dict, entry_root_path: str) -> str:
    """Return the per-route bucket for a finished request.

    Keys are route *templates* (``/users/{id}``), never concrete URLs: a key per
    URL would let scanners and 404 floods grow the stats table without bound.

    * A matched route keys on its full template, including any router prefix
      and Mount prefix.  FastAPI 0.137+ keeps included routes' short paths on
      ``route.path`` and the full one on the effective route context.
    * A Mount that matched no inner route (static files, raw ASGI apps, or a
      404 inside a mounted app) shares one ``<mount>/{path:path}`` bucket.
    * Anything else shares the single ``<unmatched>`` bucket.
    """
    root_path = scope.get("root_path", "")
    mount_prefix = root_path[len(entry_root_path):] if root_path.startswith(entry_root_path) else ""
    effective = (scope.get("fastapi") or {}).get("effective_route_context")
    template = getattr(effective, "path", None) or getattr(scope.get("route"), "path", None)
    if template:
        return mount_prefix + template
    if mount_prefix:
        return mount_prefix + "/{path:path}"
    return UNMATCHED_ROUTE


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

class RequestMetricsMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that records request metrics across all routes.

    Attach it with ``app.add_middleware(...)``, then pass the *app* to
    :class:`RequestMetricsProbe` to expose the collected data as a health probe.
    Starlette builds middleware instances lazily (on the first request or
    lifespan event), so the probe finds the live instance in the app's
    middleware stack when it first checks.

    Args:
        app: The ASGI application (injected automatically by FastAPI/Starlette).
        per_route: When ``True`` (default), metrics are also broken down by
            route path template (e.g. ``/users/{id}``), available in the probe's
            ``details["routes"]``.  Requests that match no route share a single
            ``"<unmatched>"`` entry, and a Mount without inner routes (e.g.
            static files) shares one ``"/static/{path:path}"``-style entry, so
            the table stays bounded however many distinct URLs arrive.  When
            ``False``, only aggregate stats are tracked.
        window_size: Number of recent requests used for the error rate,
            percentiles, and requests-per-minute (default ``200``).
        ema_alpha: Smoothing factor for the exponential moving average RTT
            (default ``0.1``; higher = reacts faster to changes).
        min_error_status: HTTP status codes at or above this value are counted
            as errors (default ``500``).  4xx client errors such as 404 are not
            counted as errors by default; set to ``400`` to include them.

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
        app,
        per_route: bool = True,
        window_size: int = 200,
        ema_alpha: float = 0.1,
        min_error_status: int = 500,
    ) -> None:
        super().__init__(app)
        self.per_route = per_route
        self._window_size = window_size
        self._ema_alpha = ema_alpha
        self._min_error_status = min_error_status
        self._aggregate = _RouteStats(window_size, ema_alpha, min_error_status)
        self._routes: dict[str, _RouteStats] = {}
        self._routes_lock = threading.Lock()

    def _get_or_create(self, path: str) -> _RouteStats:
        with self._routes_lock:
            if path not in self._routes:
                self._routes[path] = _RouteStats(self._window_size, self._ema_alpha, self._min_error_status)
            return self._routes[path]

    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.perf_counter()
        # Routing grows root_path by each Mount it enters; remember the starting
        # value so the mount prefix can be recovered afterwards.
        entry_root_path = request.scope.get("root_path", "")
        try:
            response = await call_next(request)
        except Exception:
            # Unhandled exceptions become a 500 in Starlette's ServerErrorMiddleware,
            # which sits outside every user middleware — so this is the only place
            # the request can be counted.  Re-raise so that response is unchanged.
            self._record(request, 500, start, entry_root_path)
            raise
        self._record(request, response.status_code, start, entry_root_path)
        return response

    def _record(self, request: Request, status_code: int, start: float, entry_root_path: str) -> None:
        """Record one finished request in the aggregate and its per-route bucket."""
        rtt_ms = round((time.perf_counter() - start) * 1000, 2)
        self._aggregate.record(status_code, rtt_ms)

        if self.per_route:
            self._get_or_create(_route_key(request.scope, entry_root_path)).record(status_code, rtt_ms)


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
                status=ProbeStatus.HEALTHY,
                latency_ms=0.0,
                details={"message": "no requests observed yet"},
            )

        snap = middleware._aggregate.snapshot()

        if snap["request_count"] == 0:
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.HEALTHY,
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
            details["routes"] = {
                path: stats.snapshot()
                for path, stats in sorted(routes_copy)
            }

        return ProbeResult(
            name=self.name,
            status=status,
            latency_ms=snap["avg_rtt_ms"],
            error="; ".join(reasons) if reasons else None,
            details=details,
        )
