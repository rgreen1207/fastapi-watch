import logging
from typing import Callable

from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe

_LOGGER = logging.getLogger("fastapi_watch")


def _safe_eval(fn: Callable[[dict], bool], details: dict) -> tuple[bool, str | None]:
    """Call *fn(details)*; return ``(met, error_type)``.

    A threshold that raises counts as not met, but is reported (logged, and
    surfaced in the result's details by the caller) rather than silently
    ignored — a typo'd key in a lambda would otherwise disable it unnoticed.
    """
    try:
        return bool(fn(details)), None
    except Exception as exc:
        _LOGGER.warning("fastapi-watch: threshold %r raised %s", fn, type(exc).__name__, exc_info=True)
        return False, type(exc).__name__


class ThresholdProbe(BaseProbe):
    """Wraps another probe and applies callable thresholds to its details.

    The inner probe runs normally.  If the inner probe is already UNHEALTHY its
    result is passed through unchanged.  Otherwise *fail_if* is evaluated first
    (→ UNHEALTHY), then *warn_if* (→ DEGRADED).  Neither threshold fires means
    the inner probe's original status is preserved.

    Register *ThresholdProbe* with the registry, not the inner probe — the
    inner probe must not be registered separately or it will run twice.

    Args:
        inner_probe: The probe whose result is evaluated.
        warn_if: Callable ``(details: dict) -> bool``.  Returns ``True`` to
            promote the result to DEGRADED.
        fail_if: Callable ``(details: dict) -> bool``.  Returns ``True`` to
            promote the result to UNHEALTHY.  Evaluated before *warn_if*.
        name: Override the probe name.  Defaults to the inner probe's name.
        poll_interval_ms: Per-probe poll interval override.  Defaults to the
            inner probe's ``poll_interval_ms``.

    Example::

        from fastapi_watch.probes import EventLoopProbe, ThresholdProbe

        # Promote DEGRADED → UNHEALTHY based on a detail value
        registry.add(ThresholdProbe(
            EventLoopProbe(),
            warn_if=lambda d: d.get("lag_ms", 0) > 3.0,
            fail_if=lambda d: d.get("lag_ms", 0) > 15.0,
        ))

        # Wrap any passive probe to add custom error-rate bands
        from fastapi_watch.probes import RedisProbe
        redis = RedisProbe(name="session-cache")

        @redis.watch
        async def get_session(sid: str):
            return await cache.get(sid)

        registry.add(ThresholdProbe(
            redis,
            warn_if=lambda d: d.get("error_rate", 0) > 0.01,
            fail_if=lambda d: d.get("consecutive_errors", 0) >= 5,
        ))
    """

    def __init__(
        self,
        inner_probe: BaseProbe,
        warn_if: Callable[[dict], bool] | None = None,
        fail_if: Callable[[dict], bool] | None = None,
        name: str | None = None,
        poll_interval_ms: int | None = None,
    ) -> None:
        self._inner = inner_probe
        self.warn_if = warn_if
        self.fail_if = fail_if
        self.name = name if name is not None else inner_probe.name
        self.description = inner_probe.description
        self.tags = inner_probe.tags
        self.timeout = inner_probe.timeout
        # A passive inner probe keeps its breaker off; wrapping must not re-enable it.
        self.circuit_breaker_enabled = inner_probe.circuit_breaker_enabled
        self.poll_interval_ms = (
            poll_interval_ms
            if poll_interval_ms is not None
            else inner_probe.poll_interval_ms
        )

    async def check(self) -> ProbeResult:
        result = await self._inner.check()

        # Already UNHEALTHY — don't downgrade a genuine failure
        if result.status == ProbeStatus.UNHEALTHY:
            return result.model_copy(update={"name": self.name})

        details = result.details or {}
        errors: dict[str, str] = {}
        update: dict = {"name": self.name}

        for label, fn, status in (("fail_if", self.fail_if, ProbeStatus.UNHEALTHY), ("warn_if", self.warn_if, ProbeStatus.DEGRADED)):
            if fn is None:
                continue
            met, error = _safe_eval(fn, details)
            if error is not None:
                errors[label] = error
            if met:
                update["status"] = status
                break

        if errors:
            update["details"] = {**details, "threshold_errors": errors}
        return result.model_copy(update=update)
