import asyncio
import smtplib
import time

from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe


class SMTPProbe(BaseProbe):
    """SMTP connectivity probe using :mod:`smtplib` (stdlib only).

    Connects to the mail server, sends ``EHLO``, and optionally upgrades to
    TLS via ``STARTTLS``.  No messages are sent.  Silent email failures are a
    common production blind spot — this probe catches them before users do.

    Args:
        host: SMTP server hostname.
        port: SMTP port (default ``25``; use ``587`` for STARTTLS, ``465`` for
            implicit TLS — note: implicit TLS requires ``smtplib.SMTP_SSL``
            which is not used here; set ``use_tls=True`` with port ``587``).
        timeout: Connection timeout in seconds (default ``5.0``).
        name: Probe name (default ``"smtp"``).
        use_tls: Upgrade the connection with ``STARTTLS`` after connecting
            (default ``False``).
        username: SMTP username for ``AUTH LOGIN`` / ``AUTH PLAIN``.
        password: SMTP password.
        poll_interval_ms: Per-probe poll interval override.

    Example::

        registry.add(SMTPProbe("smtp.sendgrid.net", port=587, use_tls=True,
                                username="apikey", password=os.environ["SENDGRID_API_KEY"]))
    """

    def __init__(
        self,
        host: str,
        port: int = 25,
        timeout: float = 5.0,
        name: str = "smtp",
        use_tls: bool = False,
        username: str | None = None,
        password: str | None = None,
        poll_interval_ms: int | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.name = name
        self.use_tls = use_tls
        self.username = username
        self.password = password
        self.poll_interval_ms = poll_interval_ms

    def _connect_sync(self) -> dict:
        """Blocking SMTP handshake — runs in an executor thread."""
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
            banner = smtp.getwelcome()
            banner_str = banner.decode(errors="replace") if isinstance(banner, bytes) else str(banner)
            if self.use_tls:
                smtp.starttls()
            if self.username is not None and self.password is not None:
                smtp.login(self.username, self.password)
            smtp.ehlo()
            # esmtp_features is populated after ehlo()
            extensions = sorted(smtp.esmtp_features.keys())
            return {
                "host": self.host,
                "port": self.port,
                "server_banner": banner_str.strip(),
                "extensions": extensions,
                "tls": self.use_tls,
            }

    async def check(self) -> ProbeResult:
        loop = asyncio.get_running_loop()
        start = time.perf_counter()
        try:
            details = await asyncio.wait_for(
                loop.run_in_executor(None, self._connect_sync),
                timeout=self.timeout + 1.0,  # executor timeout slightly wider than socket timeout
            )
            latency = round((time.perf_counter() - start) * 1000, 2)
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.HEALTHY,
                latency_ms=latency,
                details=details,
            )
        except Exception as exc:
            latency = round((time.perf_counter() - start) * 1000, 2)
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.UNHEALTHY,
                latency_ms=latency,
                error=f"{type(exc).__name__}: {exc}",
                details={"host": self.host, "port": self.port},
            )
