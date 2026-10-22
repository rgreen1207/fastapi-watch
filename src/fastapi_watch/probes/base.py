import functools
import inspect
import logging
import math
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from ..models import ProbeResult, ProbeStatus

_LOGGER = logging.getLogger("fastapi_watch")


def _failure_message(probe_name: str, exc: BaseException) -> str:
    """Error text for a failed check: the exception *type* only.

    Driver messages can embed credentials, DSNs, or internal hostnames, and
    health responses are often public, so the message itself is never exposed;
    it is logged at debug level (with the traceback) instead.  The type still
    says *what* failed.
    """
    _LOGGER.debug("fastapi-watch: probe %r check failed", probe_name, exc_info=exc)
    return f"probe check failed: {type(exc).__name__}"


def _validate_stats_params(
    *, window_size: int, ema_alpha: float, max_error_rate: float, cache_window_size: int | None = None
) -> None:
    """Raise ``ValueError`` for settings that would leave a probe unable to record.

    A zero-length window makes every recording fail; that failure is swallowed
    (recording must never break the call it observes), so the probe would
    silently report nothing forever.
    """
    if window_size <= 0:
        raise ValueError(f"window_size must be a positive integer, got {window_size!r}")
    if cache_window_size is not None and cache_window_size <= 0:
        raise ValueError(f"cache_window_size must be a positive integer or None, got {cache_window_size!r}")
    if not 0 < ema_alpha <= 1:
        raise ValueError(f"ema_alpha must be in (0, 1], got {ema_alpha!r}")
    if not 0 <= max_error_rate <= 1:
        raise ValueError(f"max_error_rate must be in [0, 1], got {max_error_rate!r}")


def _update_ema(current: float | None, value: float, alpha: float) -> float:
    """Return an updated exponential moving average."""
    return value if current is None else alpha * value + (1 - alpha) * current


def _status_family(status: int) -> str:
    """``"2xx"``-style family of an HTTP status code."""
    family = status // 100
    return _STATUS_FAMILIES[family] if 1 <= family <= 5 else f"{family}xx"


_STATUS_FAMILIES = {family: f"{family}xx" for family in range(1, 6)}


def _is_async_callable(func: Any) -> bool:
    """True if calling *func* returns an awaitable coroutine.

    Covers ``partial`` objects and callable instances with an ``async def
    __call__`` (``alru_cache`` wrappers, for one), which
    ``inspect.iscoroutinefunction`` alone reports as plain functions.
    """
    while isinstance(func, functools.partial):
        func = func.func
    return inspect.iscoroutinefunction(func) or inspect.iscoroutinefunction(getattr(type(func), "__call__", None))


def _round_ms(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _iso_utc(timestamp: float | None) -> str | None:
    """ISO-8601 UTC text for a ``time.time()`` value, or ``None``."""
    return None if timestamp is None else datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _calc_percentile(window: Sequence[float], p: float) -> float | None:
    """Return a single percentile from *window*, or ``None`` if empty."""
    if not window:
        return None
    sorted_window = sorted(window)
    return round(sorted_window[max(0, math.ceil(len(sorted_window) * p) - 1)], 2)


def _calc_percentiles(window: Sequence[float], *percentiles: float) -> tuple[float | None, ...]:
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
    description: str | None = None
    tags: list[str] | None = None
    timeout: float | None = None  # seconds; None means the registry's probe_timeout
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

    async def aclose(self) -> None:
        """Release anything the probe started (background tasks, connections).

        Called once when the registry shuts down.  The default does nothing;
        override it in probes that hold resources.
        """


class PassiveProbe(BaseProbe):
    """Base class for custom **passive** health probes.

    Passive probes never make their own external calls. Instead they monitor
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
    * ``error_rate`` — fraction of the last ``window_size`` calls that raised
    * ``consecutive_errors`` — unbroken run of failures; resets on any success
    * ``last_rtt_ms`` — execution time of the most recent call
    * ``avg_rtt_ms`` — exponential moving average RTT
    * ``p50_rtt_ms`` / ``p95_rtt_ms`` / ``p99_rtt_ms`` — percentile RTTs over the last ``window_size`` calls
    * ``min_rtt_ms`` / ``max_rtt_ms`` — all-time bounds
    * ``requests_per_minute`` — throughput over the last ``window_size`` calls
    * ``slow_calls`` — calls exceeding ``slow_call_threshold_ms`` in the last ``window_size`` calls (when threshold is set)
    * ``error_types`` — count per exception class in the last ``cache_window_size`` errors (when errors exist)

    Health thresholds (probe reports ``UNHEALTHY`` when exceeded):

    * ``max_error_rate`` — default ``0.1`` (10 %)
    * ``max_avg_rtt_ms`` — default ``None`` (disabled)

    The registry's circuit breaker is off by default (``circuit_breaker_enabled=False``):
    a passive probe only reads in-memory counters, so suspending it protects no
    dependency and would show a stale result while traffic recovers.

    ``last_error`` names only the exception type (``"IntegrityError"``), or
    ``"HTTP <status>"`` for a status-code-only error.  Exception messages often
    carry DSNs, credentials, or the bound parameters of a failed query, and
    health endpoints are often public, so the message is logged at debug level
    instead.  Pass ``expose_error_messages=True`` to report it as
    ``"<type>: <message>"`` — only where the health endpoints are protected.
    """

    # Overridden by FastAPIRouteProbe, which counts requests rather than calls.
    _count_key = "call_count"
    _empty_message = "no calls observed yet"

    def __init__(
        self,
        name: str,
        *,
        description: str | None = None,
        tags: list[str] | None = None,
        max_error_rate: float = 0.1,
        max_avg_rtt_ms: float | None = None,
        window_size: int = 100,
        ema_alpha: float = 0.1,
        circuit_breaker_enabled: bool = False,
        cache_window_size: int | None = None,
        slow_call_threshold_ms: float | None = None,
        cache_reporting: bool = True,
        cache_time_window_s: float = 120.0,
        timeout: float | None = None,
        poll_interval_ms: int | None = None,
        expose_error_messages: bool = False,
    ) -> None:
        _validate_stats_params(
            window_size=window_size,
            ema_alpha=ema_alpha,
            max_error_rate=max_error_rate,
            cache_window_size=cache_window_size,
        )
        self.name = name
        self.description = description
        self.tags = list(tags) if tags else []
        self.timeout = timeout
        self.poll_interval_ms = poll_interval_ms
        self.max_error_rate = max_error_rate
        self.max_avg_rtt_ms = max_avg_rtt_ms
        self.ema_alpha = ema_alpha
        self.circuit_breaker_enabled = circuit_breaker_enabled
        self.slow_call_threshold_ms = slow_call_threshold_ms
        self.cache_reporting = cache_reporting
        self.cache_time_window_s = cache_time_window_s
        self.expose_error_messages = expose_error_messages
        # None = not explicitly set; auto-detected from cache maxsize in watch()
        self._cache_window_size: int | None = cache_window_size
        self._cache_window_size_explicit: bool = cache_window_size is not None
        self._label: str | None = None

        stats_window = cache_window_size if cache_window_size is not None else window_size

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
        self._cache_timed_events: deque[tuple[float, bool]] = deque(maxlen=max(cache_window_size or 0, 10000))
        self._cache_maxsize: int | None = None
        self._cache_currsize: int | None = None
        # HTTP status tracking; only populated when outcomes carry a status code.
        self._last_status_code: int | None = None
        self._status_window: deque[int] = deque(maxlen=stats_window)
        self._status_counts: dict[str, int] = {}
        self._error_type_window: deque[str] = deque(maxlen=stats_window)
        self._error_type_counts: dict[str, int] = {}
        self._slow_call_count: int = 0
        self._last_error: str | None = None
        # time.time() values, formatted only when reported.
        self._last_error_at: float | None = None
        self._last_success_at: float | None = None

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

    # ------------------------------------------------------------------
    # Outcome classification — overridden by FastAPIRouteProbe
    # ------------------------------------------------------------------

    def _classify_result(self, result: Any) -> tuple[bool, int | None]:
        """Return ``(is_error, status_code)`` for a call that returned *result*."""
        return False, None

    def _classify_exception(self, exc: Exception) -> tuple[bool, int | None]:
        """Return ``(is_error, status_code)`` for a call that raised *exc*."""
        return True, None

    def _record(
        self,
        rtt_ms: float,
        *,
        error: bool,
        status: int | None = None,
        error_msg: str | None = None,
        error_type: str | None = None,
    ) -> None:
        # Throughput uses a monotonic clock, so a wall-clock step cannot skew it;
        # the wall clock is only kept for the last-error/last-success times.
        ticks = time.perf_counter()
        now = time.time()
        with self._lock:
            self._call_count += 1
            self._last_rtt_ms = rtt_ms
            self._request_timestamps.append(ticks)
            if status is not None:
                self._last_status_code = status
                if len(self._status_window) == self._status_window.maxlen:
                    evicted_family = _status_family(self._status_window[0])
                    self._status_counts[evicted_family] -= 1
                    if not self._status_counts[evicted_family]:
                        del self._status_counts[evicted_family]
                self._status_window.append(status)
                family = _status_family(status)
                self._status_counts[family] = self._status_counts.get(family, 0) + 1
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
                elif status is not None:
                    self._last_error = f"HTTP {status}"
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

    def _record_cache_delta(self, before: Any, after: Any) -> None:
        """Attribute one call's cache hit or miss from ``cache_info()`` before and after it.

        ``cache_info()`` counts every caller, so when concurrent calls interleave
        the deltas cover more than this call and cannot be attributed; those
        calls are skipped rather than miscounted (the reported hit/miss counts
        are then a sample, which keeps the ratio honest).
        """
        with self._lock:
            self._cache_maxsize = after.maxsize
            self._cache_currsize = after.currsize
        hits = after.hits - before.hits
        misses_before, misses_after = getattr(before, "misses", None), getattr(after, "misses", None)
        if misses_before is None or misses_after is None:
            # No miss counter to cross-check: fall back to the hits delta alone.
            self.record_cache_hit() if hits > 0 else self.record_cache_miss()
            return
        if hits + (misses_after - misses_before) != 1:
            return
        self.record_cache_hit() if hits else self.record_cache_miss()

    @property
    def _error_rate(self) -> float:
        """Failure fraction over the last ``window_size`` calls (read it under ``_lock``).

        Lifetime counts would let a long healthy history mask a fresh outage
        (and an old burst of errors linger forever), so health is judged on
        recent calls only.  ``error_count`` still reports the lifetime total.
        """
        window = self._outcome_window
        return self._outcome_failure_count / len(window) if window else 0.0

    @property
    def _requests_per_minute(self) -> float | None:
        """Throughput derived from the timestamp window; ``None`` until ≥2 calls (read it under ``_lock``)."""
        if len(self._request_timestamps) < 2:
            return None
        span = self._request_timestamps[-1] - self._request_timestamps[0]
        if span <= 0:
            return None
        return round((len(self._request_timestamps) - 1) / span * 60, 2)

    def watch(self, func: Callable) -> Callable:
        """Decorator that silently monitors a function call.

        Works with both ``async def`` and ``def``. Records latency and whether
        the call raised an exception. Re-raises all exceptions unchanged.

        If the decorated function has a ``cache_info()`` method (i.e. it is
        wrapped with ``@lru_cache`` or ``@alru_cache``) and ``cache_reporting``
        is ``True`` (the default), cache hits and misses are recorded
        automatically.  ``cache_maxsize`` and ``cache_currsize`` are also
        included in the probe details so you can monitor cache saturation.
        Set ``cache_reporting=False`` on the probe to opt out.
        """
        return self._wrap(func)

    def _error_message(self, exc: Exception) -> str:
        """``last_error`` text for *exc*: its type, plus its message only when opted in.

        The message (an ``HTTPException``'s ``detail`` included) is logged at
        debug level instead; see the class docstring.
        """
        if self.expose_error_messages:
            # str(HTTPException) is empty on older Starlette; its detail is the message.
            detail = getattr(exc, "detail", None)
            message = detail if isinstance(detail, str) else str(exc)
            return f"{type(exc).__name__}: {message}"
        _LOGGER.debug("fastapi-watch: probe %r observed a failed call", self.name, exc_info=exc)
        return type(exc).__name__

    def _wrap(self, func: Callable) -> Callable:
        has_cache = self.cache_reporting and callable(getattr(func, "cache_info", None))

        # Auto-detect window size from cache maxsize on first wrap (if not explicitly set).
        if has_cache and not self._cache_window_size_explicit:
            detected_maxsize = func.cache_info().maxsize
            if detected_maxsize is not None:
                self._cache_window_size = detected_maxsize

        def _start() -> tuple[Any, float]:
            return (func.cache_info() if has_cache else None), time.perf_counter()

        # Recording must never break the call it observes: failures are logged
        # and the original result or exception passes through unchanged.
        def _on_success(result: Any, started: tuple[Any, float]) -> None:
            cache_before, start = started
            # Unrounded: check() rounds what it reports, so recording need not.
            rtt_ms = (time.perf_counter() - start) * 1000
            try:
                if cache_before is not None:
                    self._record_cache_delta(cache_before, func.cache_info())
                is_error, status = self._classify_result(result)
                self._record(rtt_ms, error=is_error, status=status)
            except Exception:
                _LOGGER.debug("fastapi-watch: failed to record a call for probe %r", self.name, exc_info=True)

        def _on_error(exc: Exception, started: tuple[Any, float]) -> None:
            rtt_ms = (time.perf_counter() - started[1]) * 1000
            try:
                is_error, status = self._classify_exception(exc)
                self._record(
                    rtt_ms,
                    error=is_error,
                    status=status,
                    error_msg=self._error_message(exc),
                    error_type=type(exc).__name__,
                )
            except Exception:
                _LOGGER.debug("fastapi-watch: failed to record a call for probe %r", self.name, exc_info=True)

        if _is_async_callable(func):
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                started = _start()
                try:
                    result = await func(*args, **kwargs)
                except Exception as exc:
                    _on_error(exc, started)
                    raise
                _on_success(result, started)
                return result
            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            started = _start()
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                _on_error(exc, started)
                raise
            _on_success(result, started)
            return result
        return sync_wrapper

    async def check(self) -> ProbeResult:
        # Sync callables record from threadpool threads, so copy one consistent
        # snapshot under the lock and do the sorting and formatting outside it.
        with self._lock:
            call_count = self._call_count
            if call_count == 0:
                return ProbeResult(
                    name=self.name,
                    status=ProbeStatus.HEALTHY,
                    latency_ms=0.0,
                    details={"message": self._empty_message},
                )
            error_count = self._error_count
            error_rate = self._error_rate
            consecutive_errors = self._consecutive_errors
            last_status_code = self._last_status_code
            last_rtt_ms = self._last_rtt_ms
            avg_rtt = self._avg_rtt_ms or 0.0
            min_rtt_ms = self._min_rtt_ms
            max_rtt_ms = self._max_rtt_ms
            rtt_window = list(self._rtt_window)
            requests_per_minute = self._requests_per_minute
            slow_call_count = self._slow_call_count
            last_error = self._last_error
            status_counts = dict(self._status_counts)
            error_type_counts = dict(self._error_type_counts)
            cache_events = list(self._cache_timed_events)
            cache_maxsize = self._cache_maxsize
            cache_currsize = self._cache_currsize
            last_error_at = self._last_error_at
            last_success_at = self._last_success_at

        reasons: list[str] = []
        if error_rate > self.max_error_rate:
            reasons.append(
                f"error rate {error_rate:.1%} exceeds threshold {self.max_error_rate:.1%}"
            )
        if self.max_avg_rtt_ms is not None and avg_rtt > self.max_avg_rtt_ms:
            reasons.append(
                f"avg RTT {avg_rtt:.1f} ms exceeds threshold {self.max_avg_rtt_ms:.1f} ms"
            )

        p50, p95, p99 = _calc_percentiles(rtt_window, 0.5, 0.95, 0.99)

        details: dict[str, Any] = {}
        if self._label is not None:
            details["description"] = self._label
        details.update({
            self._count_key: call_count,
            "error_count": error_count,
            "error_rate": round(error_rate, 4),
            "consecutive_errors": consecutive_errors,
        })
        if last_status_code is not None:
            details["last_status_code"] = last_status_code
        details.update({
            "last_rtt_ms": _round_ms(last_rtt_ms),
            "avg_rtt_ms": round(avg_rtt, 2),
            "p50_rtt_ms": p50,
            "p95_rtt_ms": p95,
            "p99_rtt_ms": p99,
            "min_rtt_ms": _round_ms(min_rtt_ms),
            "max_rtt_ms": _round_ms(max_rtt_ms),
        })
        if requests_per_minute is not None:
            details["requests_per_minute"] = requests_per_minute
        if self.slow_call_threshold_ms is not None and rtt_window:
            details["slow_calls"] = slow_call_count
        if last_error is not None:
            details["last_error"] = last_error
        if status_counts:
            details["status_distribution"] = status_counts
        if error_type_counts:
            details["error_types"] = error_type_counts
        if cache_events:
            if self._cache_window_size is not None:
                window = cache_events[-self._cache_window_size:]
            else:
                cutoff = time.monotonic() - self.cache_time_window_s
                window = [entry for entry in cache_events if entry[0] >= cutoff]
            if window:
                cache_hits = sum(1 for _, is_hit in window if is_hit)
                details["cache_hits"] = cache_hits
                details["cache_misses"] = len(window) - cache_hits
                if cache_maxsize is not None:
                    details["cache_maxsize"] = cache_maxsize
                if cache_currsize is not None:
                    details["cache_currsize"] = cache_currsize
        if last_error_at is not None:
            details["last_error_at"] = _iso_utc(last_error_at)
        if error_rate >= 0.99 and last_success_at is not None:
            details["last_success_at"] = _iso_utc(last_success_at)

        return ProbeResult(
            name=self.name,
            status=ProbeStatus.UNHEALTHY if reasons else ProbeStatus.HEALTHY,
            latency_ms=round(avg_rtt, 2),
            error="; ".join(reasons) or None,
            details=details,
        )
