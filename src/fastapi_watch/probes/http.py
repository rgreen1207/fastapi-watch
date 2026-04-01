from .base import PassiveProbe


class HttpProbe(PassiveProbe):
    """Health probe that passively observes outgoing HTTP calls via the :meth:`watch` decorator.

    Rather than making its own synthetic requests (which would burn API credits
    or trip rate limits), ``HttpProbe`` instruments the functions in your code
    that call external services. Every call is silently timed and any exception
    is counted as an error.

    Works with any HTTP library and both ``async def`` and ``def`` functions.

    Args:
        name: Probe name shown in health reports (default ``"http"``).
        max_error_rate: Error-rate threshold above which the probe is UNHEALTHY (0–1).
        max_avg_rtt_ms: Average-RTT threshold in milliseconds. ``None`` disables it.
        window_size: Number of recent calls used for percentile calculations.
        ema_alpha: Smoothing factor for the exponential moving average (0–1).
        poll_interval_ms: Per-probe poll interval override.

    Example::

        stripe_probe = HttpProbe(name="stripe", max_error_rate=0.05, max_avg_rtt_ms=500)

        @stripe_probe.watch
        async def charge_customer(amount: int, currency: str) -> dict:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.stripe.com/v1/charges",
                    json={"amount": amount, "currency": currency},
                ) as response:
                    response.raise_for_status()
                    return await response.json()

        registry.add(stripe_probe)
    """

    def __init__(
        self,
        name: str = "http",
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
