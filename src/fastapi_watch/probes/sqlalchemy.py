from .base import PassiveProbe


class SqlAlchemyProbe(PassiveProbe):
    """Health probe that passively observes outgoing SQLAlchemy calls via the :meth:`watch` decorator.

    Instruments the functions in your code that use a SQLAlchemy async engine,
    recording latency and errors from real traffic rather than running a
    synthetic ``SELECT 1`` on a poll timer.

    Works with any database SQLAlchemy supports (PostgreSQL, MySQL, SQLite, etc.).

    Install with: ``pip install fastapi-watch[sqlalchemy]``

    Args:
        name: Probe name shown in health reports (default ``"database"``).
        max_error_rate: Error-rate threshold above which the probe is UNHEALTHY (0–1).
        max_avg_rtt_ms: Average-RTT threshold in milliseconds. ``None`` disables it.
        window_size: Number of recent calls used for percentile calculations.
        ema_alpha: Smoothing factor for the exponential moving average (0–1).
        poll_interval_ms: Per-probe poll interval override.

    Example::

        db_probe = SqlAlchemyProbe(name="postgres", max_error_rate=0.01)

        @db_probe.watch
        async def get_user(user_id: int) -> User | None:
            async with async_session() as session:
                return await session.get(User, user_id)

        @db_probe.watch
        async def list_items(page: int) -> list[Item]:
            async with async_session() as session:
                result = await session.execute(
                    select(Item).offset(page * 20).limit(20)
                )
                return result.scalars().all()

        registry.add(db_probe)
    """

    def __init__(
        self,
        name: str = "database",
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
