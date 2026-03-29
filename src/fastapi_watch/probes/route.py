import asyncio
import functools
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe


class RouteProbe(BaseProbe):
    """Health probe that instruments a FastAPI route handler via the :meth:`watch` decorator.

    Collects per-route traffic stats from real requests and reports them as a
    :class:`~fastapi_watch.models.ProbeResult`.  The probe is passive — it observes
    actual traffic rather than making synthetic requests.

    Stats collected:

    * ``last_rtt_ms`` — handler execution time for the most recent request
    * ``avg_rtt_ms`` — exponential moving average RTT (smoothed by ``ema_alpha``)
    * ``p95_rtt_ms`` — 95th-percentile RTT over the last ``window_size`` requests
    * ``min_rtt_ms`` / ``max_rtt_ms`` — all-time bounds
    * ``last_status_code`` — HTTP status of the most recent request
    * ``request_count`` — total requests observed
    * ``error_count`` — requests that raised an ``HTTPException`` or unhandled exception
    * ``error_rate`` — ``error_count / request_count``
    * ``consecutive_errors`` — unbroken run of failures (resets on any success)
    * ``requests_per_minute`` — throughput computed from the request timestamp window

    Health thresholds (probe reports ``UNHEALTHY`` when exceeded):

    * ``max_error_rate`` — default ``0.1`` (10 %)
    * ``max_avg_rtt_ms`` — default ``None`` (disabled)

    Usage::

        route_probe = RouteProbe(name="users", max_error_rate=0.05, max_avg_rtt_ms=300)

        @app.get("/users")
        @route_probe.watch
        async def list_users():
            ...

        registry.add(route_probe)

    Args:
        name: Probe name shown in health reports.
        max_error_rate: Error-rate threshold above which the probe is UNHEALTHY (0–1).
        max_avg_rtt_ms: Average-RTT threshold in milliseconds. ``None`` disables it.
        window_size: Number of recent requests used for percentile and throughput calculations.
        ema_alpha: Smoothing factor for the exponential moving average (0–1).
            Higher values make the average react faster to changes.
        timeout: Passed to the registry; not used internally.
    """

    def __init__(
        self,
        name: str = "route",
        *,
        max_error_rate: float = 0.1,
        max_avg_rtt_ms: float | None = None,
        window_size: int = 100,
        ema_alpha: float = 0.1,
        timeout: float | None = None,
    ) -> None:
        self.name = name
        self.timeout = timeout
        self.max_error_rate = max_error_rate
        self.max_avg_rtt_ms = max_avg_rtt_ms
        self.ema_alpha = ema_alpha

        self._lock = threading.Lock()
        self._request_count: int = 0
        self._error_count: int = 0
        self._consecutive_errors: int = 0
        self._last_status_code: int | None = None
        self._last_rtt_ms: float | None = None
        self._avg_rtt_ms: float | None = None
        self._min_rtt_ms: float | None = None
        self._max_rtt_ms: float | None = None
        self._rtt_window: deque[float] = deque(maxlen=window_size)
        self._request_timestamps: deque[float] = deque(maxlen=window_size)

    # ------------------------------------------------------------------
    # Internal recording (called by the wrapper on every request)
    # ------------------------------------------------------------------

    def _record(self, status_code: int, rtt_ms: float) -> None:
        with self._lock:
            self._request_count += 1
            self._last_status_code = status_code
            self._last_rtt_ms = rtt_ms
            self._request_timestamps.append(datetime.now(timezone.utc).timestamp())

            is_error = status_code >= 400
            if is_error:
                self._error_count += 1
                self._consecutive_errors += 1
            else:
                self._consecutive_errors = 0

            # Exponential moving average
            if self._avg_rtt_ms is None:
                self._avg_rtt_ms = rtt_ms
            else:
                self._avg_rtt_ms = self.ema_alpha * rtt_ms + (1 - self.ema_alpha) * self._avg_rtt_ms

            if self._min_rtt_ms is None or rtt_ms < self._min_rtt_ms:
                self._min_rtt_ms = rtt_ms
            if self._max_rtt_ms is None or rtt_ms > self._max_rtt_ms:
                self._max_rtt_ms = rtt_ms

            self._rtt_window.append(rtt_ms)

    # ------------------------------------------------------------------
    # Computed properties
    # ------------------------------------------------------------------

    @property
    def _error_rate(self) -> float:
        if self._request_count == 0:
            return 0.0
        return self._error_count / self._request_count

    @property
    def _p95_rtt_ms(self) -> float | None:
        if not self._rtt_window:
            return None
        sorted_rtts = sorted(self._rtt_window)
        idx = max(0, int(len(sorted_rtts) * 0.95) - 1)
        return round(sorted_rtts[idx], 2)

    @property
    def _requests_per_minute(self) -> float | None:
        """Throughput derived from the timestamp window; ``None`` until ≥2 requests."""
        if len(self._request_timestamps) < 2:
            return None
        span = self._request_timestamps[-1] - self._request_timestamps[0]
        if span <= 0:
            return None
        return round((len(self._request_timestamps) - 1) / span * 60, 2)

    # ------------------------------------------------------------------
    # Decorator
    # ------------------------------------------------------------------

    def watch(self, func: Callable) -> Callable:
        """Decorator that instruments a route handler.

        Works with both ``async def`` and ``def`` handlers.  Preserves the
        function signature so FastAPI dependency injection continues to work.

        ``HTTPException`` is caught to record its status code and then
        re-raised so FastAPI's normal exception handling is unaffected.
        Any other exception is recorded as a 500 and re-raised.
        """
        if asyncio.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                start = time.perf_counter()
                try:
                    result = await func(*args, **kwargs)
                    rtt_ms = round((time.perf_counter() - start) * 1000, 2)
                    # Honour explicit status codes set on a Response return value.
                    status_code = getattr(result, "status_code", 200)
                    self._record(status_code, rtt_ms)
                    return result
                except Exception as exc:
                    rtt_ms = round((time.perf_counter() - start) * 1000, 2)
                    status_code = getattr(exc, "status_code", 500)
                    self._record(status_code, rtt_ms)
                    raise

            return async_wrapper
        else:
            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                start = time.perf_counter()
                try:
                    result = func(*args, **kwargs)
                    rtt_ms = round((time.perf_counter() - start) * 1000, 2)
                    status_code = getattr(result, "status_code", 200)
                    self._record(status_code, rtt_ms)
                    return result
                except Exception as exc:
                    rtt_ms = round((time.perf_counter() - start) * 1000, 2)
                    status_code = getattr(exc, "status_code", 500)
                    self._record(status_code, rtt_ms)
                    raise

            return sync_wrapper

    # ------------------------------------------------------------------
    # BaseProbe interface
    # ------------------------------------------------------------------

    async def check(self) -> ProbeResult:
        if self._request_count == 0:
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.HEALTHY,
                latency_ms=0.0,
                details={"message": "no requests observed yet"},
            )

        error_rate = self._error_rate
        avg_rtt = self._avg_rtt_ms or 0.0

        reasons: list[str] = []
        if error_rate > self.max_error_rate:
            reasons.append(
                f"error rate {error_rate:.1%} exceeds threshold {self.max_error_rate:.1%}"
            )
        if self.max_avg_rtt_ms is not None and avg_rtt > self.max_avg_rtt_ms:
            reasons.append(
                f"avg RTT {avg_rtt:.1f} ms exceeds threshold {self.max_avg_rtt_ms:.1f} ms"
            )

        status = ProbeStatus.UNHEALTHY if reasons else ProbeStatus.HEALTHY

        details: dict[str, Any] = {
            "request_count": self._request_count,
            "error_count": self._error_count,
            "error_rate": round(error_rate, 4),
            "consecutive_errors": self._consecutive_errors,
            "last_status_code": self._last_status_code,
            "last_rtt_ms": self._last_rtt_ms,
            "avg_rtt_ms": round(avg_rtt, 2),
            "p95_rtt_ms": self._p95_rtt_ms,
            "min_rtt_ms": self._min_rtt_ms,
            "max_rtt_ms": self._max_rtt_ms,
        }

        rpm = self._requests_per_minute
        if rpm is not None:
            details["requests_per_minute"] = rpm

        return ProbeResult(
            name=self.name,
            status=status,
            latency_ms=round(avg_rtt, 2),
            error="; ".join(reasons) or None,
            details=details,
        )
