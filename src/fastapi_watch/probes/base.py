import asyncio
import functools
import math
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

from ..models import ProbeResult, ProbeStatus


def _update_ema(current: float | None, value: float, alpha: float) -> float:
    """Return an updated exponential moving average."""
    return value if current is None else alpha * value + (1 - alpha) * current


def _calc_percentile(window: deque, p: float) -> float | None:
    """Return a single percentile from *window*, or ``None`` if empty."""
    if not window:
        return None
    sorted_window = sorted(window)
    return round(sorted_window[max(0, math.ceil(len(sorted_window) * p) - 1)], 2)


def _calc_percentiles(window: deque, *percentiles: float) -> tuple[float | None, ...]:
    """Return multiple percentiles from *window* in a single sort pass."""
    if not window:
        return tuple(None for _ in percentiles)
    sorted_window = sorted(window)
    window_length = len(sorted_window)
    return tuple(round(sorted_window[max(0, math.ceil(window_length * p) - 1)], 2) for p in percentiles)


class BaseProbe(ABC):
    """Base class for custom **active** health probes.

    Active probes make their own check on every poll cycle (e.g. a TCP ping,
    a queue depth query, a process liveness check). Subclass this, implement
    :meth:`check`, and register the probe with the registry.

    Example::

        from fastapi_watch import BaseProbe
        from fastapi_watch.models import ProbeResult, ProbeStatus

        class QueueDepthProbe(BaseProbe):
            name = "job-queue"
            poll_interval_ms = 10_000  # check every 10 s

            def __init__(self, queue, warn_depth: int = 500, fail_depth: int = 1000):
                self.queue = queue
                self.warn_depth = warn_depth
                self.fail_depth = fail_depth

            async def check(self) -> ProbeResult:
                depth = await self.queue.depth()
                if depth >= self.fail_depth:
                    status = ProbeStatus.UNHEALTHY
                elif depth >= self.warn_depth:
                    status = ProbeStatus.DEGRADED
                else:
                    status = ProbeStatus.HEALTHY
                return ProbeResult(
                    name=self.name,
                    status=status,
                    latency_ms=0.0,
                    details={"depth": depth, "warn": self.warn_depth, "fail": self.fail_depth},
                )

        registry.add(QueueDepthProbe(queue, warn_depth=200, fail_depth=800))
    """

    name: str = "unnamed"
    timeout: float | None = None  # seconds; None means no timeout
    poll_interval_ms: int | None = None
    circuit_breaker_threshold: int | None = None
    circuit_breaker_cooldown_ms: int | None = None
    circuit_breaker_enabled: bool = True

    def disable_circuit_breaker(self) -> "BaseProbe":
        """Disable the circuit breaker for this probe. Returns ``self`` for chaining."""
        self.circuit_breaker_enabled = False
        return self

    def enable_circuit_breaker(self) -> "BaseProbe":
        """Re-enable the circuit breaker for this probe. Returns ``self`` for chaining."""
        self.circuit_breaker_enabled = True
        return self

    @abstractmethod
    async def check(self) -> ProbeResult:
        """Execute the health check and return a :class:`~fastapi_watch.models.ProbeResult`."""


class PassiveProbe(BaseProbe):
    """Base class for custom **passive** health probes.

    Passive probes never make their own external calls. Instead they instrument
    functions in your code via the inherited :meth:`watch` decorator. Every
    watched call is silently timed; exceptions are counted as errors and
    re-raised unchanged. :meth:`check` reports the accumulated stats.

    Subclass this, set a default ``name``, and optionally override the
    docstring. No ``check`` implementation is needed — it is provided.

    Example::

        from fastapi_watch import PassiveProbe

        class PaymentGatewayProbe(PassiveProbe):
            \\"\\"\\"Observes calls to the payment gateway.\\"\\"\\"

        payment_probe = PaymentGatewayProbe(name="stripe", max_error_rate=0.02)

        @payment_probe.watch
        async def create_charge(amount: int, token: str) -> dict:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.stripe.com/v1/charges",
                    json={"amount": amount, "source": token},
                ) as resp:
                    resp.raise_for_status()
                    return await resp.json()

        registry.add(payment_probe)

    Stats reported by :meth:`check`:

    * ``call_count`` — total calls observed
    * ``error_count`` — calls that raised an exception
    * ``error_rate`` — ``error_count / call_count``
    * ``consecutive_errors`` — unbroken run of failures; resets on any success
    * ``last_rtt_ms`` — execution time of the most recent call
    * ``avg_rtt_ms`` — exponential moving average RTT
    * ``p50_rtt_ms`` / ``p95_rtt_ms`` / ``p99_rtt_ms`` — percentile RTTs over the last ``window_size`` calls
    * ``min_rtt_ms`` / ``max_rtt_ms`` — all-time bounds
    * ``slow_calls`` — calls exceeding ``slow_call_threshold_ms`` in the last ``window_size`` calls (when threshold is set)
    * ``error_types`` — count per exception class in the last ``cache_window_size`` errors (when errors exist)

    Health thresholds (probe reports ``UNHEALTHY`` when exceeded):

    * ``max_error_rate`` — default ``0.1`` (10 %)
    * ``max_avg_rtt_ms`` — default ``None`` (disabled)
    """

    def __init__(
        self,
        name: str,
        *,
        max_error_rate: float = 0.1,
        max_avg_rtt_ms: float | None = None,
        window_size: int = 100,
        ema_alpha: float = 0.1,
        circuit_breaker_enabled: bool = True,
        cache_window_size: int | None = None,
        slow_call_threshold_ms: float | None = None,
        cache_reporting: bool = True,
        cache_time_window_s: float = 120.0,
    ) -> None:
        self.name = name
        self.max_error_rate = max_error_rate
        self.max_avg_rtt_ms = max_avg_rtt_ms
        self.ema_alpha = ema_alpha
        self.circuit_breaker_enabled = circuit_breaker_enabled
        self.slow_call_threshold_ms = slow_call_threshold_ms
        self.cache_reporting = cache_reporting
        self.cache_time_window_s = cache_time_window_s
        # None = not explicitly set; auto-detected from cache maxsize in watch()
        self._cache_window_size: int | None = cache_window_size
        self._cache_window_size_explicit: bool = cache_window_size is not None

        _stats_window = cache_window_size if cache_window_size is not None else window_size

        self._lock = threading.Lock()
        self._call_count: int = 0
        self._error_count: int = 0
        self._consecutive_errors: int = 0
        self._last_rtt_ms: float | None = None
        self._avg_rtt_ms: float | None = None
        self._min_rtt_ms: float | None = None
        self._max_rtt_ms: float | None = None
        self._rtt_window: deque[float] = deque(maxlen=window_size)
        self._request_timestamps: deque[float] = deque(maxlen=window_size)
        self._outcome_window: deque[bool] = deque(maxlen=window_size)  # True=success, False=failure
        self._outcome_failure_count: int = 0
        # Cache events stored as (monotonic_timestamp, is_hit); window capped to avoid unbounded growth.
        _cache_event_cap = max(cache_window_size or 0, 10000)
        self._cache_timed_events: deque[tuple[float, bool]] = deque(maxlen=_cache_event_cap)
        self._cache_maxsize: int | None = None
        self._cache_currsize: int | None = None
        self._error_type_window: deque[str] = deque(maxlen=_stats_window)
        self._error_type_counts: dict[str, int] = {}
        self._slow_call_count: int = 0
        self._last_error: str | None = None
        self._last_error_at: datetime | None = None
        self._last_success_at: datetime | None = None

    def record_cache_hit(self) -> None:
        """Record a cache hit manually.

        Use this when you are not using ``@lru_cache`` / ``@alru_cache`` but
        still want to track cache performance (e.g. Redis lookups).  When
        ``@probe.watch`` wraps a cached function, hits and misses are recorded
        automatically and you do not need to call this directly.
        """
        with self._lock:
            self._cache_timed_events.append((time.monotonic(), True))

    def record_cache_miss(self) -> None:
        """Record a cache miss manually.

        See :meth:`record_cache_hit` for when to use this method.
        """
        with self._lock:
            self._cache_timed_events.append((time.monotonic(), False))

    def _record(
        self,
        rtt_ms: float,
        *,
        error: bool,
        error_msg: str | None = None,
        error_type: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            self._call_count += 1
            self._last_rtt_ms = rtt_ms
            self._request_timestamps.append(now.timestamp())
            # outcome window — track failures incrementally
            if len(self._outcome_window) == self._outcome_window.maxlen:
                if not self._outcome_window[0]:
                    self._outcome_failure_count -= 1
            self._outcome_window.append(not error)
            if error:
                self._error_count += 1
                self._consecutive_errors += 1
                self._outcome_failure_count += 1
                self._last_error_at = now
                if error_msg is not None:
                    self._last_error = error_msg
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

    @property
    def _error_rate(self) -> float:
        return 0.0 if self._call_count == 0 else self._error_count / self._call_count

    def watch(self, func: Callable) -> Callable:
        """Decorator that silently instruments a function call.

        Works with both ``async def`` and ``def``. Records latency and whether
        the call raised an exception. Re-raises all exceptions unchanged.

        If the decorated function has a ``cache_info()`` method (i.e. it is
        wrapped with ``@lru_cache`` or ``@alru_cache``) and ``cache_reporting``
        is ``True`` (the default), cache hits and misses are recorded
        automatically.  ``cache_maxsize`` and ``cache_currsize`` are also
        included in the probe details so you can monitor cache saturation.
        Set ``cache_reporting=False`` on the probe to opt out.
        """
        _has_cache = self.cache_reporting and callable(getattr(func, "cache_info", None))

        # Auto-detect window size from cache maxsize on first wrap (if not explicitly set).
        if _has_cache and not self._cache_window_size_explicit:
            detected_maxsize = func.cache_info().maxsize
            if detected_maxsize is not None:
                self._cache_window_size = detected_maxsize

        if asyncio.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                hits_before = func.cache_info().hits if _has_cache else 0
                start = time.perf_counter()
                try:
                    result = await func(*args, **kwargs)
                    rtt_ms = round((time.perf_counter() - start) * 1000, 2)
                    try:
                        if _has_cache:
                            info = func.cache_info()
                            if info.hits > hits_before:
                                self.record_cache_hit()
                            else:
                                self.record_cache_miss()
                            self._cache_maxsize = info.maxsize
                            self._cache_currsize = info.currsize
                        self._record(rtt_ms, error=False)
                    except Exception:
                        pass
                    return result
                except Exception as exc:
                    try:
                        self._record(
                            round((time.perf_counter() - start) * 1000, 2),
                            error=True,
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
                hits_before = func.cache_info().hits if _has_cache else 0
                start = time.perf_counter()
                try:
                    result = func(*args, **kwargs)
                    rtt_ms = round((time.perf_counter() - start) * 1000, 2)
                    try:
                        if _has_cache:
                            info = func.cache_info()
                            if info.hits > hits_before:
                                self.record_cache_hit()
                            else:
                                self.record_cache_miss()
                            self._cache_maxsize = info.maxsize
                            self._cache_currsize = info.currsize
                        self._record(rtt_ms, error=False)
                    except Exception:
                        pass
                    return result
                except Exception as exc:
                    try:
                        self._record(
                            round((time.perf_counter() - start) * 1000, 2),
                            error=True,
                            error_msg=f"{type(exc).__name__}: {exc}",
                            error_type=type(exc).__name__,
                        )
                    except Exception:
                        pass
                    raise
            return sync_wrapper

    async def check(self) -> ProbeResult:
        if self._call_count == 0:
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.HEALTHY,
                latency_ms=0.0,
                details={"message": "no calls observed yet"},
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

        p50, p95, p99 = _calc_percentiles(self._rtt_window, 0.5, 0.95, 0.99)

        details: dict[str, Any] = {
            "call_count": self._call_count,
            "error_count": self._error_count,
            "error_rate": round(error_rate, 4),
            "consecutive_errors": self._consecutive_errors,
            "last_rtt_ms": self._last_rtt_ms,
            "avg_rtt_ms": round(avg_rtt, 2),
            "p50_rtt_ms": p50,
            "p95_rtt_ms": p95,
            "p99_rtt_ms": p99,
            "min_rtt_ms": self._min_rtt_ms,
            "max_rtt_ms": self._max_rtt_ms,
        }
        if self.slow_call_threshold_ms is not None and self._rtt_window:
            details["slow_calls"] = self._slow_call_count
        if self._last_error is not None:
            details["last_error"] = self._last_error
        if self._error_type_counts:
            details["error_types"] = dict(self._error_type_counts)
        if self._cache_timed_events:
            events = list(self._cache_timed_events)
            if self._cache_window_size is not None:
                window = events[-self._cache_window_size:]
            else:
                cutoff = time.monotonic() - self.cache_time_window_s
                window = [entry for entry in events if entry[0] >= cutoff]
            if window:
                cache_hits = sum(1 for _, is_hit in window if is_hit)
                details["cache_hits"] = cache_hits
                details["cache_misses"] = len(window) - cache_hits
                if self._cache_maxsize is not None:
                    details["cache_maxsize"] = self._cache_maxsize
                if self._cache_currsize is not None:
                    details["cache_currsize"] = self._cache_currsize
        if self._last_error_at is not None:
            details["last_error_at"] = self._last_error_at.isoformat()
        outcome_window = self._outcome_window
        if outcome_window and self._outcome_failure_count / len(outcome_window) >= 0.99 and self._last_success_at is not None:
            details["last_success_at"] = self._last_success_at.isoformat()

        return ProbeResult(
            name=self.name,
            status=ProbeStatus.UNHEALTHY if reasons else ProbeStatus.HEALTHY,
            latency_ms=round(avg_rtt, 2),
            error="; ".join(reasons) or None,
            details=details,
        )
