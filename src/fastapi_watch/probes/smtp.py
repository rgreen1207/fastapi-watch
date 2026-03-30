import asyncio
import functools
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe, _calc_p95, _update_ema


class SMTPProbe(BaseProbe):
    """Health probe that passively observes outgoing email calls via the :meth:`watch` decorator.

    Rather than repeatedly authenticating against a third-party mail service on a
    timer (which risks rate limits and security alerts), ``SMTPProbe`` instruments
    the functions in your code that actually send mail. Every call is silently timed
    and any exception is counted as an error.

    Stats collected:

    * ``last_rtt_ms`` — execution time of the most recent send
    * ``avg_rtt_ms`` — exponential moving average RTT
    * ``p95_rtt_ms`` — 95th-percentile RTT over the last ``window_size`` calls
    * ``min_rtt_ms`` / ``max_rtt_ms`` — all-time bounds
    * ``call_count`` — total sends observed
    * ``error_count`` — sends that raised an exception
    * ``error_rate`` — ``error_count / call_count``
    * ``consecutive_errors`` — unbroken run of failures

    Health thresholds (probe reports ``UNHEALTHY`` when exceeded):

    * ``max_error_rate`` — default ``0.1`` (10 %)
    * ``max_avg_rtt_ms`` — default ``None`` (disabled)

    Usage::

        smtp_probe = SMTPProbe(name="sendgrid", max_error_rate=0.05)

        @smtp_probe.watch
        async def send_welcome_email(to: str) -> None:
            async with aiosmtplib.SMTP("smtp.sendgrid.net", port=587) as smtp:
                await smtp.login("apikey", os.environ["SENDGRID_API_KEY"])
                await smtp.sendmail(FROM, to, message.as_string())

        registry.add(smtp_probe)

    Args:
        name: Probe name shown in health reports.
        max_error_rate: Error-rate threshold above which the probe is UNHEALTHY (0–1).
        max_avg_rtt_ms: Average-RTT threshold in milliseconds. ``None`` disables it.
        window_size: Number of recent calls used for percentile calculations.
        ema_alpha: Smoothing factor for the exponential moving average (0–1).
        poll_interval_ms: Per-probe poll interval override.
    """

    def __init__(
        self,
        name: str = "smtp",
        *,
        max_error_rate: float = 0.1,
        max_avg_rtt_ms: float | None = None,
        window_size: int = 100,
        ema_alpha: float = 0.1,
        poll_interval_ms: int | None = None,
    ) -> None:
        self.name = name
        self.poll_interval_ms = poll_interval_ms
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

    # ------------------------------------------------------------------
    # Internal recording
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Computed properties
    # ------------------------------------------------------------------

    @property
    def _error_rate(self) -> float:
        if self._call_count == 0:
            return 0.0
        return self._error_count / self._call_count

    @property
    def _p95_rtt_ms(self) -> float | None:
        return _calc_p95(self._rtt_window)

    # ------------------------------------------------------------------
    # Decorator
    # ------------------------------------------------------------------

    def watch(self, func: Callable) -> Callable:
        """Decorator that instruments an outgoing email send.

        Works with both ``async def`` and ``def`` functions. Records latency
        and whether the call raised an exception. Re-raises all exceptions
        so normal error handling in your code is unaffected.

        Example::

            @smtp_probe.watch
            async def send_welcome_email(to: str) -> None:
                async with aiosmtplib.SMTP("smtp.sendgrid.net", port=587) as smtp:
                    await smtp.login("apikey", os.environ["SENDGRID_API_KEY"])
                    await smtp.sendmail(FROM, to, message.as_string())
        """
        if asyncio.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                start = time.perf_counter()
                try:
                    result = await func(*args, **kwargs)
                    rtt_ms = round((time.perf_counter() - start) * 1000, 2)
                    self._record(rtt_ms, error=False)
                    return result
                except Exception:
                    rtt_ms = round((time.perf_counter() - start) * 1000, 2)
                    self._record(rtt_ms, error=True)
                    raise

            return async_wrapper
        else:
            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                start = time.perf_counter()
                try:
                    result = func(*args, **kwargs)
                    rtt_ms = round((time.perf_counter() - start) * 1000, 2)
                    self._record(rtt_ms, error=False)
                    return result
                except Exception:
                    rtt_ms = round((time.perf_counter() - start) * 1000, 2)
                    self._record(rtt_ms, error=True)
                    raise

            return sync_wrapper

    # ------------------------------------------------------------------
    # BaseProbe interface
    # ------------------------------------------------------------------

    async def check(self) -> ProbeResult:
        if self._call_count == 0:
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.HEALTHY,
                latency_ms=0.0,
                details={"message": "no sends observed yet"},
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

        return ProbeResult(
            name=self.name,
            status=status,
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
