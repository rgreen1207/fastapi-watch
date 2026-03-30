from .base import PassiveProbe


class PostgreSQLProbe(PassiveProbe):
    """Health probe that passively observes outgoing PostgreSQL calls via the :meth:`watch` decorator.

    Instruments the functions in your code that query PostgreSQL, recording
    latency and errors from real traffic rather than opening a synthetic
    connection and running queries on a poll timer.

    Install with: ``pip install fastapi-watch[postgres]``

    Args:
        name: Probe name shown in health reports (default ``"postgresql"``).
        max_error_rate: Error-rate threshold above which the probe is UNHEALTHY (0–1).
        max_avg_rtt_ms: Average-RTT threshold in milliseconds. ``None`` disables it.
        window_size: Number of recent calls used for percentile calculations.
        ema_alpha: Smoothing factor for the exponential moving average (0–1).
        poll_interval_ms: Per-probe poll interval override.

    Example::

        pg_probe = PostgreSQLProbe(name="primary-db", max_error_rate=0.01)

        @pg_probe.watch
        async def get_user(user_id: int) -> dict | None:
            async with pool.acquire() as conn:
                return await conn.fetchrow(
                    "SELECT id, email FROM users WHERE id = $1", user_id
                )

        @pg_probe.watch
        async def create_order(user_id: int, total: float) -> int:
            async with pool.acquire() as conn:
                return await conn.fetchval(
                    "INSERT INTO orders (user_id, total) VALUES ($1, $2) RETURNING id",
                    user_id, total,
                )

        registry.add(pg_probe)
    """

    def __init__(
        self,
        name: str = "postgresql",
        *,
        max_error_rate: float = 0.1,
        max_avg_rtt_ms: float | None = None,
        window_size: int = 100,
        ema_alpha: float = 0.1,
    ) -> None:
        super().__init__(
            name,
            max_error_rate=max_error_rate,
            max_avg_rtt_ms=max_avg_rtt_ms,
            window_size=window_size,
            ema_alpha=ema_alpha,
        )
