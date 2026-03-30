from typing import Callable

from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe


def _safe_eval(fn: Callable[[dict], bool], details: dict) -> bool:
    """Call *fn(details)*, returning False if it raises."""
    try:
        return bool(fn(details))
    except Exception:
        return False


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
        self.timeout = inner_probe.timeout
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

        if self.fail_if is not None and _safe_eval(self.fail_if, details):
            return result.model_copy(update={"name": self.name, "status": ProbeStatus.UNHEALTHY})

        if self.warn_if is not None and _safe_eval(self.warn_if, details):
            return result.model_copy(update={"name": self.name, "status": ProbeStatus.DEGRADED})

        return result.model_copy(update={"name": self.name})
