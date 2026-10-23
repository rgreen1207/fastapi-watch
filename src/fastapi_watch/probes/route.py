from typing import Any, Callable

from starlette.requests import ClientDisconnect
from starlette.responses import Response

from .base import PassiveProbe

# Recorded for a request whose client hung up before the response (nginx's
# 499): an aborted upload is not a server error.
CLIENT_CLOSED_REQUEST = 499


def _status_code(value: Any, default: int) -> int:
    """Return *value* as an HTTP status code, or *default* if it is not one.

    A route can return any object; one with a non-integer ``status_code``
    attribute (a domain model field, say) must not drop the request record.
    """
    return value if isinstance(value, int) and not isinstance(value, bool) else default


class FastAPIRouteProbe(PassiveProbe):
    """Health probe that monitors a FastAPI route handler via the :meth:`watch` decorator.

    Collects per-route traffic stats from real requests and reports them as a
    :class:`~fastapi_watch.models.ProbeResult`.  The probe is passive — it observes
    actual traffic rather than making synthetic requests.  It shares its
    recording and reporting with :class:`~fastapi_watch.PassiveProbe`, judging
    each request by its HTTP status instead of by whether it raised.

    Stats collected:

    * ``last_rtt_ms`` — handler execution time for the most recent request
    * ``avg_rtt_ms`` — exponential moving average RTT (smoothed by ``ema_alpha``)
    * ``p50_rtt_ms`` / ``p95_rtt_ms`` / ``p99_rtt_ms`` — percentile RTTs over the last ``window_size`` requests
    * ``min_rtt_ms`` / ``max_rtt_ms`` — all-time bounds
    * ``last_status_code`` — HTTP status of the most recent request
    * ``request_count`` — total requests observed
    * ``error_count`` — requests at or above ``min_error_status``
    * ``error_rate`` — fraction of the last ``window_size`` requests that were errors
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
        window_size: Rolling window for the error rate, RTT percentiles, throughput, and slow-call counts.
        ema_alpha: Smoothing factor for the exponential moving average (0–1).
            Higher values make the average react faster to changes.
        timeout: Passed to the registry; not used internally.
        min_error_status: HTTP status codes at or above this value are counted
            as errors (default ``500``).  4xx client errors such as 404 are not
            counted as errors by default; set to ``400`` to include them.
        circuit_breaker_enabled: Whether the registry's circuit breaker may
            suspend this probe (default ``False``: it only reads in-memory stats).
        cache_window_size: Rolling window for cache hit/miss counts, status
            distribution, and error-type breakdown. Defaults to ``window_size``.
        slow_call_threshold_ms: Requests taking longer than this (ms) are counted
            as slow calls. ``None`` disables the counter.
        expose_error_messages: When ``True``, ``last_error`` includes the
            exception message (an ``HTTPException``'s ``detail``, for example).
            Off by default: messages can carry credentials or personal data,
            and only the exception type (or ``"HTTP <status>"``) is reported.
    """

    _count_key = "request_count"
    _empty_message = "no requests observed yet"

    def __init__(
        self,
        name: str = "route",
        *,
        description: str | None = None,
        tags: list[str] | None = None,
        max_error_rate: float = 0.1,
        max_avg_rtt_ms: float | None = None,
        window_size: int = 100,
        ema_alpha: float = 0.1,
        timeout: float | None = None,
        poll_interval_ms: int | None = None,
        min_error_status: int = 500,
        circuit_breaker_enabled: bool = False,
        cache_window_size: int | None = None,
        slow_call_threshold_ms: float | None = None,
        cache_reporting: bool = True,
        cache_time_window_s: float = 120.0,
        expose_error_messages: bool = False,
    ) -> None:
        super().__init__(
            name,
            description=description,
            tags=tags,
            max_error_rate=max_error_rate,
            max_avg_rtt_ms=max_avg_rtt_ms,
            window_size=window_size,
            ema_alpha=ema_alpha,
            circuit_breaker_enabled=circuit_breaker_enabled,
            cache_window_size=cache_window_size,
            slow_call_threshold_ms=slow_call_threshold_ms,
            cache_reporting=cache_reporting,
            cache_time_window_s=cache_time_window_s,
            timeout=timeout,
            poll_interval_ms=poll_interval_ms,
            expose_error_messages=expose_error_messages,
        )
        self.min_error_status = min_error_status

    def watch(self, func_or_label: Callable | str | None = None) -> Callable:
        """Decorator that monitors a route handler.

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
        ``ClientDisconnect`` (the client hung up while the handler read its
        body) is recorded as a 499; any other exception as a 500.  Either
        way it is re-raised.
        """
        if isinstance(func_or_label, str):
            self._label = func_or_label
            return self._wrap
        if func_or_label is None:
            return self._wrap
        return self._wrap(func_or_label)

    def _wrap(self, func: Callable) -> Callable:
        wrapper = super()._wrap(func)
        # Markers read by discover_routes()/watch_router(): manual monitoring wins.
        wrapper._fastapi_watch = "manual"
        wrapper._fastapi_watch_probe = self
        return wrapper

    def _classify_result(self, result: Any) -> tuple[bool, int | None]:
        """Honour an explicit status code on a returned Response; default 200.

        Only a ``Response`` sets the status: FastAPI serialises anything else
        (a model with its own ``status_code`` field, say) with the route's.
        """
        status = _status_code(result.status_code, 200) if isinstance(result, Response) else 200
        return status >= self.min_error_status, status

    def _classify_exception(self, exc: Exception) -> tuple[bool, int | None]:
        """Use an ``HTTPException``'s status code; a client that hung up while
        the handler read its body is a 499; anything else counts as a 500."""
        if isinstance(exc, ClientDisconnect):
            status = CLIENT_CLOSED_REQUEST
        else:
            status = _status_code(getattr(exc, "status_code", 500), 500)
        return status >= self.min_error_status, status
