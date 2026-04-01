"""Request metrics middleware and associated probe.

Add :class:`RequestMetricsMiddleware` to your FastAPI app and pass it to
:class:`RequestMetricsProbe` to get automatic, app-wide request telemetry
without decorating individual routes.
"""
import threading
import time
from collections import deque
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from .models import ProbeResult, ProbeStatus
from .probes.base import BaseProbe, _calc_percentiles, _update_ema


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
        self._ema_alpha = ema_alpha
        self._min_error_status = min_error_status

    def record(self, status_code: int, rtt_ms: float) -> None:
        with self._lock:
            self._request_count += 1
            self._request_timestamps.append(time.monotonic())
            is_error = status_code >= self._min_error_status
            if is_error:
                self._error_count += 1
                self._consecutive_errors += 1
            else:
                self._consecutive_errors = 0
            self._avg_rtt_ms = _update_ema(self._avg_rtt_ms, rtt_ms, self._ema_alpha)
            self._rtt_window.append(rtt_ms)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            rc = self._request_count
            ec = self._error_count
            error_rate = ec / rc if rc > 0 else 0.0
            avg = round(self._avg_rtt_ms or 0.0, 2)
            ce = self._consecutive_errors
            rtt_copy = list(self._rtt_window)
            rpm: float | None = None
            ts = self._request_timestamps
            if len(ts) >= 2:
                span = ts[-1] - ts[0]
                if span > 0:
                    rpm = round((len(ts) - 1) / span * 60, 2)
        # sort outside the lock — reduces lock hold time under concurrent recording
        p50, p95, p99 = _calc_percentiles(rtt_copy, 0.5, 0.95, 0.99)
        return {
            "request_count": rc,
            "error_count": ec,
            "error_rate": round(error_rate, 4),
            "consecutive_errors": ce,
            "avg_rtt_ms": avg,
            "p50_rtt_ms": p50,
            "p95_rtt_ms": p95,
            "p99_rtt_ms": p99,
            "requests_per_minute": rpm,
        }


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

class RequestMetricsMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that records request metrics across all routes.

    Attach to your FastAPI app, then pass the same instance to
    :class:`RequestMetricsProbe` to expose the collected data as a health probe.

    Args:
        app: The ASGI application (injected automatically by FastAPI/Starlette).
        per_route: When ``True`` (default), metrics are also broken down by
            route path template (e.g. ``/users/{id}``), available in the probe's
            ``details["routes"]``.  When ``False``, only aggregate stats are
            tracked.
        window_size: Number of recent requests used for p95 and
            requests-per-minute calculations (default ``200``).
        ema_alpha: Smoothing factor for the exponential moving average RTT
            (default ``0.1``; higher = reacts faster to changes).
        min_error_status: HTTP status codes at or above this value are counted
            as errors (default ``500``).  4xx client errors such as 404 are not
            counted as errors by default; set to ``400`` to include them.

    Example::

        from fastapi import FastAPI
        from fastapi_watch import HealthRegistry, RequestMetricsMiddleware, RequestMetricsProbe

        app = FastAPI()

        metrics = RequestMetricsMiddleware(app, per_route=True)
        app.add_middleware(RequestMetricsMiddleware, per_route=True)

        # Attach the middleware *before* building the probe
        registry = HealthRegistry(app)
        registry.add(RequestMetricsProbe(metrics, max_error_rate=0.05))
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
        response = await call_next(request)
        rtt_ms = round((time.perf_counter() - start) * 1000, 2)
        status_code = response.status_code

        self._aggregate.record(status_code, rtt_ms)

        if self.per_route:
            # Prefer the route template (e.g. /users/{id}) over the concrete
            # path (e.g. /users/42) so stats group by pattern, not by value.
            route = request.scope.get("route")
            path = getattr(route, "path", None) or request.url.path or "<unmatched>"
            self._get_or_create(path).record(status_code, rtt_ms)

        return response


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
        middleware: The :class:`RequestMetricsMiddleware` instance attached to
            the app.
        name: Probe name (default ``"request_metrics"``).
        max_error_rate: Error rate (0–1) above which the probe is UNHEALTHY
            (default ``0.1`` = 10 %).
        max_avg_rtt_ms: Average RTT threshold in milliseconds.  ``None``
            disables this check.
        poll_interval_ms: Per-probe poll interval override.

    Example::

        registry.add(RequestMetricsProbe(metrics, max_error_rate=0.02, max_avg_rtt_ms=500))
    """

    def __init__(
        self,
        middleware: RequestMetricsMiddleware,
        name: str = "request_metrics",
        max_error_rate: float = 0.1,
        max_avg_rtt_ms: float | None = None,
        poll_interval_ms: int | None = None,
    ) -> None:
        self.name = name
        self._middleware = middleware
        self.max_error_rate = max_error_rate
        self.max_avg_rtt_ms = max_avg_rtt_ms
        self.poll_interval_ms = poll_interval_ms

    async def check(self) -> ProbeResult:
        snap = self._middleware._aggregate.snapshot()

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

        if self._middleware.per_route:
            with self._middleware._routes_lock:
                routes_copy = list(self._middleware._routes.items())
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
