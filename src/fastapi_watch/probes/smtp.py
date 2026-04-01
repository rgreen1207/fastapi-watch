from .base import PassiveProbe


class SMTPProbe(PassiveProbe):
    """Health probe that passively observes outgoing email calls via the :meth:`watch` decorator.

    Rather than repeatedly authenticating against a third-party mail service on
    a timer (which risks rate limits and security alerts), ``SMTPProbe``
    instruments the functions in your code that actually send mail. Every call
    is silently timed and any exception is counted as an error.

    Works with any SMTP library and both ``async def`` and ``def`` functions.

    Args:
        name: Probe name shown in health reports (default ``"smtp"``).
        max_error_rate: Error-rate threshold above which the probe is UNHEALTHY (0–1).
        max_avg_rtt_ms: Average-RTT threshold in milliseconds. ``None`` disables it.
        window_size: Number of recent calls used for percentile calculations.
        ema_alpha: Smoothing factor for the exponential moving average (0–1).
        poll_interval_ms: Per-probe poll interval override.

    Example::

        smtp_probe = SMTPProbe(name="sendgrid", max_error_rate=0.05)

        @smtp_probe.watch
        async def send_welcome_email(to: str) -> None:
            async with aiosmtplib.SMTP("smtp.sendgrid.net", port=587) as smtp:
                await smtp.login("apikey", os.environ["SENDGRID_API_KEY"])
                await smtp.sendmail(FROM, to, message.as_string())

        registry.add(smtp_probe)
    """

    def __init__(self, name: str = "smtp", **kwargs) -> None:
        super().__init__(name, **kwargs)