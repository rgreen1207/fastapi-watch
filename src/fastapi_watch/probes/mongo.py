from .base import PassiveProbe


class MongoProbe(PassiveProbe):
    """Health probe that passively observes outgoing MongoDB calls via the :meth:`watch` decorator.

    Instruments the functions in your code that query MongoDB, recording
    latency and errors from real traffic rather than issuing a synthetic
    ``serverStatus`` command on a poll timer.

    Install with: ``pip install fastapi-watch[mongo]``

    Args:
        name: Probe name shown in health reports (default ``"mongodb"``).
        max_error_rate: Error-rate threshold above which the probe is UNHEALTHY (0–1).
        max_avg_rtt_ms: Average-RTT threshold in milliseconds. ``None`` disables it.
        window_size: Number of recent calls used for percentile calculations.
        ema_alpha: Smoothing factor for the exponential moving average (0–1).
        poll_interval_ms: Per-probe poll interval override.

    Example::

        mongo_probe = MongoProbe(name="mongodb", max_error_rate=0.02)

        @mongo_probe.watch
        async def get_document(doc_id: str) -> dict | None:
            return await db.documents.find_one({"_id": doc_id})

        @mongo_probe.watch
        async def insert_event(event: dict) -> str:
            result = await db.events.insert_one(event)
            return str(result.inserted_id)

        registry.add(mongo_probe)
    """

    def __init__(
        self,
        name: str = "mongodb",
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
