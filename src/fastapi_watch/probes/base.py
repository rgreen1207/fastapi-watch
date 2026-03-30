import asyncio
import functools
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


def _calc_p95(window: deque) -> float | None:
    """Return the 95th-percentile value from a deque, or None if empty."""
    if not window:
        return None
    s = sorted(window)
    return round(s[max(0, int(len(s) * 0.95) - 1)], 2)


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
    * ``p95_rtt_ms`` — 95th-percentile RTT over the last ``window_size`` calls
    * ``min_rtt_ms`` / ``max_rtt_ms`` — all-time bounds

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
    ) -> None:
        self.name = name
        self.max_error_rate = max_error_rate
        self.max_avg_rtt_ms = max_avg_rtt_ms
        self.ema_alpha = ema_alpha

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

    def _record(self, rtt_ms: float, *, error: bool) -> None:
        with self._lock:
            self._call_count += 1
            self._last_rtt_ms = rtt_ms
            self._request_timestamps.append(datetime.now(timezone.utc).timestamp())
            if error:
                self._error_count += 1
                self._consecutive_errors += 1
            else:
                self._consecutive_errors = 0
            self._avg_rtt_ms = _update_ema(self._avg_rtt_ms, rtt_ms, self.ema_alpha)
            self._min_rtt_ms = rtt_ms if self._min_rtt_ms is None else min(self._min_rtt_ms, rtt_ms)
            self._max_rtt_ms = rtt_ms if self._max_rtt_ms is None else max(self._max_rtt_ms, rtt_ms)
            self._rtt_window.append(rtt_ms)

    @property
    def _error_rate(self) -> float:
        return 0.0 if self._call_count == 0 else self._error_count / self._call_count

    @property
    def _p95_rtt_ms(self) -> float | None:
        return _calc_p95(self._rtt_window)

    def watch(self, func: Callable) -> Callable:
        """Decorator that silently instruments a function call.

        Works with both ``async def`` and ``def``. Records latency and whether
        the call raised an exception. Re-raises all exceptions unchanged.
        """
        if asyncio.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                start = time.perf_counter()
                try:
                    result = await func(*args, **kwargs)
                    self._record(round((time.perf_counter() - start) * 1000, 2), error=False)
                    return result
                except Exception:
                    self._record(round((time.perf_counter() - start) * 1000, 2), error=True)
                    raise
            return async_wrapper
        else:
            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                start = time.perf_counter()
                try:
                    result = func(*args, **kwargs)
                    self._record(round((time.perf_counter() - start) * 1000, 2), error=False)
                    return result
                except Exception:
                    self._record(round((time.perf_counter() - start) * 1000, 2), error=True)
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

        return ProbeResult(
            name=self.name,
            status=ProbeStatus.UNHEALTHY if reasons else ProbeStatus.HEALTHY,
            latency_ms=round(avg_rtt, 2),
            error="; ".join(reasons) or None,
            details={
                "call_count": self._call_count,
                "error_count": self._error_count,
                "error_rate": round(error_rate, 4),
                "consecutive_errors": self._consecutive_errors,
                "last_rtt_ms": self._last_rtt_ms,
                "avg_rtt_ms": round(avg_rtt, 2),
                "p95_rtt_ms": self._p95_rtt_ms,
                "min_rtt_ms": self._min_rtt_ms,
                "max_rtt_ms": self._max_rtt_ms,
            },
        )
