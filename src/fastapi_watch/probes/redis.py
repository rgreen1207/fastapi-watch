from .base import PassiveProbe


class RedisProbe(PassiveProbe):
    """Health probe that passively observes outgoing Redis calls via the :meth:`watch` decorator.

    Instruments the functions in your code that interact with Redis, recording
    latency and errors from real traffic rather than making synthetic commands
    on a poll timer.

    Install with: ``pip install fastapi-watch[redis]``

    Args:
        name: Probe name shown in health reports (default ``"redis"``).
        max_error_rate: Error-rate threshold above which the probe is UNHEALTHY (0–1).
        max_avg_rtt_ms: Average-RTT threshold in milliseconds. ``None`` disables it.
        window_size: Number of recent calls used for percentile calculations.
        ema_alpha: Smoothing factor for the exponential moving average (0–1).
        poll_interval_ms: Per-probe poll interval override.

    Example::

        redis_probe = RedisProbe(name="session-cache", max_error_rate=0.05)

        @redis_probe.watch
        async def get_session(session_id: str) -> dict | None:
            return await redis.hgetall(f"session:{session_id}")

        @redis_probe.watch
        async def set_session(session_id: str, data: dict) -> None:
            await redis.hset(f"session:{session_id}", mapping=data)
            await redis.expire(f"session:{session_id}", 3600)

        registry.add(redis_probe)
    """

    def __init__(
        self,
        name: str = "redis",
        *,
        max_error_rate: float = 0.1,
        max_avg_rtt_ms: float | None = None,
        window_size: int = 100,
        ema_alpha: float = 0.1,
        circuit_breaker_enabled: bool = True,
        cache_window_size: int | None = None,
        slow_call_threshold_ms: float | None = None,
    ) -> None:
        super().__init__(
            name,
            max_error_rate=max_error_rate,
            max_avg_rtt_ms=max_avg_rtt_ms,
            window_size=window_size,
            ema_alpha=ema_alpha,
            circuit_breaker_enabled=circuit_breaker_enabled,
            cache_window_size=cache_window_size,
            slow_call_threshold_ms=slow_call_threshold_ms,
        )
