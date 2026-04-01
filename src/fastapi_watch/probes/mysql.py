from .base import PassiveProbe


class MySQLProbe(PassiveProbe):
    """Health probe that passively observes outgoing MySQL / MariaDB calls via the :meth:`watch` decorator.

    Instruments the functions in your code that query MySQL, recording
    latency and errors from real traffic rather than opening a synthetic
    connection and running queries on a poll timer.

    Install with: ``pip install fastapi-watch[mysql]``

    Args:
        name: Probe name shown in health reports (default ``"mysql"``).
        max_error_rate: Error-rate threshold above which the probe is UNHEALTHY (0–1).
        max_avg_rtt_ms: Average-RTT threshold in milliseconds. ``None`` disables it.
        window_size: Number of recent calls used for percentile calculations.
        ema_alpha: Smoothing factor for the exponential moving average (0–1).
        poll_interval_ms: Per-probe poll interval override.

    Example::

        mysql_probe = MySQLProbe(name="primary-db", max_error_rate=0.01)

        @mysql_probe.watch
        async def get_product(product_id: int) -> dict | None:
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT id, name, price FROM products WHERE id = %s",
                        (product_id,),
                    )
                    return await cur.fetchone()

        registry.add(mysql_probe)
    """

    def __init__(self, name: str = "mysql", **kwargs) -> None:
        super().__init__(name, **kwargs)