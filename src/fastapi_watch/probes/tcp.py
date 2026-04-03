import asyncio
import socket
import time

from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe


class TCPProbe(BaseProbe):
    """DNS resolve and TCP connection health check (stdlib only).

    Performs a DNS lookup for *host* and then opens a TCP connection to
    *host*:*port*.  Both steps must succeed for the probe to be healthy.
    Useful for diagnosing network partitions and firewall issues before
    attempting a full application-level check against a dependency.

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

    async def check(self) -> ProbeResult:
        loop = asyncio.get_running_loop()
        start = time.perf_counter()
        try:
            # DNS resolution runs in a thread pool (blocking syscall)
            infos = await loop.run_in_executor(
                None,
                lambda: socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM),
            )
            resolved_ips = sorted({info[4][0] for info in infos})

            # TCP connect with timeout
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.timeout,
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

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
        except Exception as exc:
            latency = round((time.perf_counter() - start) * 1000, 2)
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.UNHEALTHY,
                latency_ms=latency,
                error="probe check failed",
                details={"host": self.host, "port": self.port},
            )
