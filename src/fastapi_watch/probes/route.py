import asyncio
import functools
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe, _calc_percentiles, _update_ema


class FastAPIRouteProbe(BaseProbe):
    """Health probe that instruments a FastAPI route handler via the :meth:`watch` decorator.

    Collects per-route traffic stats from real requests and reports them as a
    :class:`~fastapi_watch.models.ProbeResult`.  The probe is passive — it observes
    actual traffic rather than making synthetic requests.

    Stats collected:

    * ``last_rtt_ms`` — handler execution time for the most recent request
    * ``avg_rtt_ms`` — exponential moving average RTT (smoothed by ``ema_alpha``)
    * ``p50_rtt_ms`` / ``p95_rtt_ms`` / ``p99_rtt_ms`` — percentile RTTs over the last ``window_size`` requests
    * ``min_rtt_ms`` / ``max_rtt_ms`` — all-time bounds
    * ``last_status_code`` — HTTP status of the most recent request
    * ``request_count`` — total requests observed
    * ``error_count`` — requests at or above ``min_error_status``
    * ``error_rate`` — ``error_count / request_count``
    * ``consecutive_errors`` — unbroken run of failures (resets on any success)
    * ``requests_per_minute`` — throughput computed from the request timestamp window
    * ``slow_calls`` — requests exceeding ``slow_call_threshold_ms`` in the last ``window_size`` requests (when threshold is set)
    * ``status_distribution`` — count per status-code family (2xx/3xx/4xx/5xx) over the last ``cache_window_size`` requests
    * ``error_types`` — count per exception class over the last ``cache_window_size`` errors (exceptions only, not status-code-only errors)

    Health thresholds (probe reports ``UNHEALTHY`` when exceeded):

    * ``max_error_rate`` — default ``0.1`` (10 %)
    * ``max_avg_rtt_ms`` — default ``None`` (disabled)

    Usage::

        route_probe = FastAPIRouteProbe(name="users", max_error_rate=0.05, max_avg_rtt_ms=300)

        @app.get("/users")
        @route_probe.watch
        async def list_users():
            ...

        registry.add(route_probe)

    Args:
        name: Probe name shown in health reports.
        max_error_rate: Error-rate threshold above which the probe is UNHEALTHY (0–1).
        max_avg_rtt_ms: Average-RTT threshold in milliseconds. ``None`` disables it.
        window_size: Rolling window for RTT percentiles, throughput, and slow-call counts.
        ema_alpha: Smoothing factor for the exponential moving average (0–1).
            Higher values make the average react faster to changes.
        timeout: Passed to the registry; not used internally.
        min_error_status: HTTP status codes at or above this value are counted
            as errors (default ``500``).  4xx client errors such as 404 are not
            counted as errors by default; set to ``400`` to include them.
        cache_window_size: Rolling window for cache hit/miss counts, status
            distribution, and error-type breakdown. Defaults to ``window_size``.
        slow_call_threshold_ms: Requests taking longer than this (ms) are counted
            as slow calls. ``None`` disables the counter.
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
        poll_interval_ms: int | None = None,
        min_error_status: int = 500,
        circuit_breaker_enabled: bool = True,
        cache_window_size: int | None = None,
        slow_call_threshold_ms: float | None = None,
    ) -> None:
        self.name = name
        self.timeout = timeout
        self.poll_interval_ms = poll_interval_ms
        self.max_error_rate = max_error_rate
        self.max_avg_rtt_ms = max_avg_rtt_ms
        self.ema_alpha = ema_alpha
        self.min_error_status = min_error_status
        self.circuit_breaker_enabled = circuit_breaker_enabled
        self.slow_call_threshold_ms = slow_call_threshold_ms

        _stats_window = cache_window_size if cache_window_size is not None else window_size

        self._label: str | None = None
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
        self._outcome_window: deque[bool] = deque(maxlen=window_size)  # True=success, False=failure
        self._outcome_failure_count: int = 0
        self._cache_window: deque[bool] = deque(maxlen=_stats_window)
        self._cache_hit_count: int = 0
        self._status_window: deque[int] = deque(maxlen=_stats_window)
        self._status_counts: dict[str, int] = {}
        self._error_type_window: deque[str] = deque(maxlen=_stats_window)
        self._error_type_counts: dict[str, int] = {}
        self._slow_call_count: int = 0
        self._last_error: str | None = None
        self._last_error_at: datetime | None = None
        self._last_success_at: datetime | None = None

    # ------------------------------------------------------------------
    # Internal recording (called by the wrapper on every request)
    # ------------------------------------------------------------------

    def record_cache_hit(self) -> None:
        """Record a cache hit.

        Counts are tracked over the last ``cache_window_size`` (or ``window_size``)
        cache lookups; older entries are dropped automatically as new ones arrive.
        """
        with self._lock:
            if len(self._cache_window) == self._cache_window.maxlen and self._cache_window[0]:
                self._cache_hit_count -= 1
            self._cache_window.append(True)
            self._cache_hit_count += 1

    def record_cache_miss(self) -> None:
        """Record a cache miss.

        Counts are tracked over the last ``cache_window_size`` (or ``window_size``)
        cache lookups; older entries are dropped automatically as new ones arrive.
        """
        with self._lock:
            if len(self._cache_window) == self._cache_window.maxlen and self._cache_window[0]:
                self._cache_hit_count -= 1
            self._cache_window.append(False)

    def _record(
        self,
        status_code: int,
        rtt_ms: float,
        error_msg: str | None = None,
        error_type: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            self._request_count += 1
            self._last_status_code = status_code
            self._last_rtt_ms = rtt_ms
            self._request_timestamps.append(now.timestamp())

            # status window — track distribution incrementally
            fam = f"{status_code // 100}xx"
            if len(self._status_window) == self._status_window.maxlen:
                evicted_fam = f"{self._status_window[0] // 100}xx"
                self._status_counts[evicted_fam] -= 1
                if not self._status_counts[evicted_fam]:
                    del self._status_counts[evicted_fam]
            self._status_window.append(status_code)
            self._status_counts[fam] = self._status_counts.get(fam, 0) + 1

            is_error = status_code >= self.min_error_status
            # outcome window — track failures incrementally
            if len(self._outcome_window) == self._outcome_window.maxlen:
                if not self._outcome_window[0]:
                    self._outcome_failure_count -= 1
            self._outcome_window.append(not is_error)
            if is_error:
                self._error_count += 1
                self._consecutive_errors += 1
                self._outcome_failure_count += 1
                self._last_error_at = now
                self._last_error = error_msg or f"HTTP {status_code}"
                if error_type is not None:
                    if len(self._error_type_window) == self._error_type_window.maxlen:
                        evicted = self._error_type_window[0]
                        self._error_type_counts[evicted] -= 1
                        if not self._error_type_counts[evicted]:
                            del self._error_type_counts[evicted]
                    self._error_type_window.append(error_type)
                    self._error_type_counts[error_type] = self._error_type_counts.get(error_type, 0) + 1
            else:
                self._consecutive_errors = 0
                self._last_success_at = now

            self._avg_rtt_ms = _update_ema(self._avg_rtt_ms, rtt_ms, self.ema_alpha)
            self._min_rtt_ms = rtt_ms if self._min_rtt_ms is None else min(self._min_rtt_ms, rtt_ms)
            self._max_rtt_ms = rtt_ms if self._max_rtt_ms is None else max(self._max_rtt_ms, rtt_ms)
            # slow call tracking — update before appending so we can read window[0]
            if self.slow_call_threshold_ms is not None:
                if len(self._rtt_window) == self._rtt_window.maxlen and self._rtt_window[0] > self.slow_call_threshold_ms:
                    self._slow_call_count -= 1
                if rtt_ms > self.slow_call_threshold_ms:
                    self._slow_call_count += 1
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

    def watch(self, func_or_label: Callable | str | None = None) -> Callable:
        """Decorator that instruments a route handler.

        Can be used with or without an optional string label:

        .. code-block:: python

            @route_probe.watch
            async def list_users(): ...

            @route_probe.watch("GET /users")
            async def list_users(): ...

        The label is included as ``"description"`` in the reported details,
        which is useful when multiple probes cover different methods on the
        same path (e.g. ``GET /users`` vs ``POST /users``).

        Works with both ``async def`` and ``def`` handlers.  Preserves the
        function signature so FastAPI dependency injection continues to work.

        ``HTTPException`` is caught to record its status code and then
        re-raised so FastAPI's normal exception handling is unaffected.
        Any other exception is recorded as a 500 and re-raised.
        """
        if isinstance(func_or_label, str):
            self._label: str | None = func_or_label
            return self._wrap
        if func_or_label is None:
            return self._wrap
        return self._wrap(func_or_label)

    def _wrap(self, func: Callable) -> Callable:
        if asyncio.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                start = time.perf_counter()
                try:
                    result = await func(*args, **kwargs)
                    rtt_ms = round((time.perf_counter() - start) * 1000, 2)
                    # Honour explicit status codes set on a Response return value.
                    status_code = getattr(result, "status_code", 200)
                    try:
                        self._record(status_code, rtt_ms)
                    except Exception:
                        pass
                    return result
                except Exception as exc:
                    rtt_ms = round((time.perf_counter() - start) * 1000, 2)
                    status_code = getattr(exc, "status_code", 500)
                    try:
                        self._record(
                            status_code,
                            rtt_ms,
                            error_msg=f"{type(exc).__name__}: {exc}",
                            error_type=type(exc).__name__,
                        )
                    except Exception:
                        pass
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
                    try:
                        self._record(status_code, rtt_ms)
                    except Exception:
                        pass
                    return result
                except Exception as exc:
                    rtt_ms = round((time.perf_counter() - start) * 1000, 2)
                    status_code = getattr(exc, "status_code", 500)
                    try:
                        self._record(
                            status_code,
                            rtt_ms,
                            error_msg=f"{type(exc).__name__}: {exc}",
                            error_type=type(exc).__name__,
                        )
                    except Exception:
                        pass
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
        p50, p95, p99 = _calc_percentiles(self._rtt_window, 0.5, 0.95, 0.99)

        details: dict[str, Any] = {
            **({"description": self._label} if self._label is not None else {}),
            "request_count": self._request_count,
            "error_count": self._error_count,
            "error_rate": round(error_rate, 4),
            "consecutive_errors": self._consecutive_errors,
            "last_status_code": self._last_status_code,
            "last_rtt_ms": self._last_rtt_ms,
            "avg_rtt_ms": round(avg_rtt, 2),
            "p50_rtt_ms": p50,
            "p95_rtt_ms": p95,
            "p99_rtt_ms": p99,
            "min_rtt_ms": self._min_rtt_ms,
            "max_rtt_ms": self._max_rtt_ms,
        }

        rpm = self._requests_per_minute
        if rpm is not None:
            details["requests_per_minute"] = rpm
        if self.slow_call_threshold_ms is not None and self._rtt_window:
            details["slow_calls"] = self._slow_call_count
        if self._last_error is not None:
            details["last_error"] = self._last_error
        if self._status_counts:
            details["status_distribution"] = dict(self._status_counts)
        if self._error_type_counts:
            details["error_types"] = dict(self._error_type_counts)
        if self._cache_window:
            details["cache_hits"] = self._cache_hit_count
            details["cache_misses"] = len(self._cache_window) - self._cache_hit_count
        if self._last_error_at is not None:
            details["last_error_at"] = self._last_error_at.isoformat()
        w = self._outcome_window
        if w and self._outcome_failure_count / len(w) >= 0.99 and self._last_success_at is not None:
            details["last_success_at"] = self._last_success_at.isoformat()

        return ProbeResult(
            name=self.name,
            status=status,
            latency_ms=round(avg_rtt, 2),
            error="; ".join(reasons) or None,
            details=details,
        )
