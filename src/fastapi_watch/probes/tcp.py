import asyncio
import socket
import time

from .._executor import SingleFlight, StillRunning
from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe, _failure_message


class TCPProbe(BaseProbe):
    """DNS resolve and TCP connection health check (stdlib only).

    Performs a DNS lookup for *host* and then opens a TCP connection to one
    of the resolved addresses on *port*, trying each in turn.  Both steps must
    succeed for the probe to be healthy.  Useful for diagnosing network
    partitions and firewall issues before attempting a full application-level
    check against a dependency.

    The lookup is a blocking call, so it runs in fastapi-watch's own small
    thread pool rather than the event loop's default executor.  If a DNS
    server hangs past the probe's deadline, the next check reports
    ``"previous check still running"`` instead of starting another lookup.

    Args:
        host: Hostname or IP address to connect to.
        port: TCP port number.
        name: Probe name (default ``"tcp:<host>:<port>"``).
        timeout: Connection timeout in seconds (default ``5.0``).
        poll_interval_ms: Per-probe poll interval override.

    Example::

        registry.add(TCPProbe("db.internal", 5432, name="pg-tcp"))
        registry.add(TCPProbe("cache.internal", 6379, name="redis-tcp"))
    """

    def __init__(
        self,
        host: str,
        port: int,
        name: str | None = None,
        timeout: float = 5.0,
        poll_interval_ms: int | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.name = name or f"tcp:{host}:{port}"
        self.timeout = timeout
        self.poll_interval_ms = poll_interval_ms
        self._resolve_call = SingleFlight()

    def _resolve(self) -> list[tuple]:
        return socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)

    async def _connect(self, infos: list[tuple]) -> None:
        """Connect to each resolved address in turn until one accepts.

        Connecting to an IP address (rather than *host*) keeps asyncio from
        resolving the name a second time in the default executor.
        """
        error: Exception | None = None
        for family, _, proto, _, sockaddr in infos:
            try:
                _, writer = await asyncio.open_connection(
                    sockaddr[0], sockaddr[1], family=family, proto=proto
                )
            except OSError as exc:
                error = exc
                continue
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return
        raise error if error is not None else OSError(f"no addresses resolved for {self.host!r}")

    async def check(self) -> ProbeResult:
        start = time.perf_counter()
        try:
            infos = await self._resolve_call.run(self._resolve)
            resolved_ips = sorted({info[4][0] for info in infos})

            # TCP connect with timeout
            await asyncio.wait_for(self._connect(infos), timeout=self.timeout)

            connect_ms = round((time.perf_counter() - start) * 1000, 2)
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.HEALTHY,
                latency_ms=connect_ms,
                details={
                    "host": self.host,
                    "port": self.port,
                    "resolved_ips": resolved_ips,
                    "connect_ms": connect_ms,
                },
            )
        except StillRunning:
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.UNHEALTHY,
                latency_ms=0.0,
                error="previous check still running",
                details={"host": self.host, "port": self.port},
            )
        except Exception as exc:
            latency = round((time.perf_counter() - start) * 1000, 2)
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.UNHEALTHY,
                latency_ms=latency,
                error=_failure_message(self.name, exc),
                details={"host": self.host, "port": self.port},
            )
